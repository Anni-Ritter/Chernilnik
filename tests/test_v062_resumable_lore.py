import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from lore_import import (ImportDraft, analyze_bible, draft_root, plan_from_draft,
                         split_source, apply_bible, _decode)
from storage import Store


def api_result(raw, name):
    quoted=raw[:min(70,len(raw))].strip()
    return NS(choices=[NS(finish_reason='stop',message=NS(content=json.dumps({
        'summary':[], 'articles':[{'title':name,'body':'Установленная подробная статья мира',
                                   'kind':'place','certainty':'canon','evidence':quoted}],
        'people':[]},ensure_ascii=False)))])


def source_with_many_parts():
    return ('# Мир\n'+('Фантастический мир с множеством городов и исторических событий.\n'*38)
            +'# Раздел два\n'+('Кариат — милитаризированная империя с армией и гильдиями.\n'*38)
            +'# Раздел три\n'+('Эльдора — страна Светоносцев и церковной власти.\n'*38))


def test_draft_recovers_after_restart_without_rebilling_first_part(tmp_path):
    src=source_with_many_parts()
    parts=split_source(src)
    assert len(parts)>2
    d=ImportDraft.create(tmp_path,12,src,'google/gemini-3.7-flash')
    class Forbidden(Exception):
        status_code=403
    class Client:
        def __init__(self):self.calls=[];self.forbid=True
        async def create(self,**kw):
            chunk=kw['messages'][1]['content'].split(':\n\n',1)[1]
            idx=next((i for i,p in enumerate(parts) if p==chunk),-1)
            self.calls.append(idx)
            if idx==1 and self.forbid: raise Forbidden('forbidden')
            return api_result(chunk,f'Запись {idx}')
    fake=Client()
    client=NS(chat=NS(completions=fake))
    with pytest.raises(Forbidden):
        asyncio.run(analyze_bible(client,d.model,src,draft=d))
    assert d.completed_root_parts(parts)==1
    fake.forbid=False
    reloaded=ImportDraft(tmp_path,12,d.digest)
    result=asyncio.run(analyze_bible(client,reloaded.model,src,draft=reloaded))
    assert fake.calls.count(0)==1, 'The first completed part must never be charged again.'
    assert result['articles']
    assert reloaded.complete
    assert reloaded.completed_root_parts(parts)==len(parts)
    assert plan_from_draft(reloaded)['parts']==len(parts)
    assert ImportDraft.latest(tmp_path,12).digest==d.digest
    reloaded.cancel()
    assert ImportDraft.latest(tmp_path,12) is None
    restored=ImportDraft.create(tmp_path,12,src,reloaded.model)
    assert restored.completed_root_parts(parts)==0


def test_invalid_json_gets_retry_and_smaller_split_if_necessary(tmp_path,monkeypatch):
    src='# Эльдора\n'+('Светоносцы защищают Верею, стоящую около вечной зимы. '*28)
    d=ImportDraft.create(tmp_path,1,src,'google/gemini-3.7-flash')
    calls=[]
    class Client:
        async def create(self,**kwargs):
            raw=kwargs['messages'][1]['content'].split(':\n\n',1)[1]
            calls.append(raw)
            # Fail for any chunk larger than 800 chars; recover on both halves.
            if len(raw)>800:
                return NS(choices=[NS(finish_reason='length',message=NS(content='{'))])
            return api_result(raw,'Верея')
    async def no_wait(_):return None
    monkeypatch.setattr('lore_import.asyncio.sleep',no_wait)
    result=asyncio.run(analyze_bible(NS(chat=NS(completions=Client())),d.model,src,draft=d))
    assert len(calls)>3
    assert d.data['splits']
    assert result['articles'][0]['title']=='Верея'
    assert d.complete
    before=len(calls)
    asyncio.run(analyze_bible(NS(chat=NS(completions=Client())),d.model,src,draft=ImportDraft(tmp_path,1,d.digest)))
    assert len(calls)==before


def test_response_format_unsupported_falls_back(tmp_path):
    src='# Эльдора\nСветоносцы охраняют границу, но судьба Солемира неизвестна.'
    modes=[]
    class BadFormat(Exception):
        status_code=400
    class Client:
        async def create(self,**kwargs):
            mode=kwargs.get('response_format',{}).get('type','plain')
            modes.append(mode)
            if mode!='plain':raise BadFormat('response_format unsupported')
            raw=kwargs['messages'][1]['content'].split(':\n\n',1)[1]
            return api_result(raw,'Эльдора')
    result=asyncio.run(analyze_bible(NS(chat=NS(completions=Client())), 'google/gemini-3.7-flash',src))
    assert modes==['json_schema','json_object','plain']
    assert result['articles'][0]['title']=='Эльдора'


def test_commit_only_after_confirmation(tmp_path):
    db=Store(str(tmp_path/'world.sqlite3'))
    wid=db.add_world('Сквозь миры')
    source='# Кариат\nКариат — военная империя.'
    draft=ImportDraft.create(draft_root(db),wid,source,'google/gemini-3.7-flash')
    assert db.list_world_lore(wid)==[]
    class Client:
        async def create(self,**kwargs):
            raw=kwargs['messages'][1]['content'].split(':\n\n',1)[1]
            return api_result(raw,'Кариат')
    asyncio.run(analyze_bible(NS(chat=NS(completions=Client())),draft.model,draft.source,draft=draft))
    assert db.list_world_lore(wid)==[]
    result=apply_bible(db,wid,draft.source,plan_from_draft(draft))
    draft.mark_applied()
    assert result['articles']==1
    assert len(db.list_world_lore(wid))==1
    assert ImportDraft.latest(draft_root(db),wid) is None


def test_decode_rejects_truncated_json_and_accepts_fences():
    with pytest.raises(ValueError):_decode('{"summary":[}')
    assert _decode('```json\n{"summary":[],"articles":[],"people":[]}\n```')['summary']==[]
