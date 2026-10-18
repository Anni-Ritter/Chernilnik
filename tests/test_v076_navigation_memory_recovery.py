"""v0.7.6: compact menus and reliably inspectable, recoverable RP memory."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS

from storage import Store
from engine import RoleplayEngine
from memory_pipeline import parse_payload, important_turn


def test_memory_job_tracks_missed_turns_and_persists(tmp_path):
    path = tmp_path/'test.sqlite3'
    db = Store(str(path))
    sid = db.new_session(4, 'Граница Эльдоры')
    a = db.add_turn(sid,'Кирай скрылась','Авинель пошла за ней','g',1,2)
    b = db.add_turn(sid,'Удар химеры','Они сбежали в Севтос','g',1,2)
    assert db.memory_health(sid) == {'total':2,'done':0,'failed':0,'unprocessed':2}
    db.record_memory_job(sid,a,success=True,count=2)
    db.record_memory_job(sid,b,success=False,error='Invalid JSON')
    assert [r['id'] for r in db.turns_for_memory_sync(sid)] == [b]
    db.record_memory_job(sid,b,success=True,count=1)
    assert db.memory_health(sid) == {'total':2,'done':2,'failed':0,'unprocessed':0}
    assert Store(str(path)).memory_job(sid,b)['attempts'] == 2
    assert db.recent_turns(sid,2)[-1]['bot_text'] == 'Они сбежали в Севтос'


def test_parser_rejects_incomplete_json_and_recovers_fences():
    assert parse_payload('```json\n{"facts":[],"world_changes":[],"character_insights":[]}\n```')['facts'] == []
    import pytest
    with pytest.raises(ValueError):
        parse_payload('{"facts": []}')
    with pytest.raises(ValueError):
        parse_payload('```json\n{"facts":')
    assert important_turn('Авинель пала, они сбежали от химер в Севтос.')


def test_failed_luna_falls_back_to_gemini_without_mangling_story(tmp_path):
    db=Store(str(tmp_path/'test.sqlite3'))
    sid=db.new_session(1,'Авинель и Кирай')
    user=db.add_character(sid,'Кирай','user')
    bot=db.add_character(sid,'Авинель','bot')
    post='Кирай произнесла, что не доверяет инквизиторам.'
    reply='Авинель увидела химер и приказала отступать в Севтос.'
    tid=db.add_turn(sid,post,reply,'gemini',23,21)

    class OpenAI:
        async def create(self,**kw):
            return NS(output_text='', usage=NS(input_tokens=44,output_tokens=3200))

    result={
      'facts':[{'text':'Авинель приказала отступать в Севтос при появлении химер.',
               'importance':4,'category':'event','certainty':'confirmed','known_by':'',
               'evidence':'Авинель увидела химер и приказала отступать в Севтос.'}],
      'world_changes':[],
      'character_insights':[{'name':'Кирай','field':'values',
                             'text':'Кирай не доверяет инквизиторам.',
                             'certainty':'confirmed','evidence':post},
                            {'name':'Авинель','field':'goals',
                             'text':'Авинель решила отступать в Севтос.',
                             'certainty':'confirmed', 'evidence':reply}]
    }
    class Completions:
        async def create(self, **kwargs):
            assert kwargs['model']=='google/gemini-3.7-flash'
            assert 'character_insights' in kwargs['messages'][0]['content']
            return NS(choices=[NS(message=NS(content=json.dumps(result)))],
                      usage=NS(prompt_tokens=2200,completion_tokens=450))

    engine=RoleplayEngine(db,NS(responses=OpenAI()),'test','luna')
    engine.polza_client=NS(chat=NS(completions=Completions()))
    assert asyncio.run(engine.memorize(sid,tid,post,reply)) == 1
    assert len(db.list_facts(sid)) == 1
    assert db.memory_job(sid,tid)['status']=='done'
    assert len(db.development(bot,sid))==2  # action + major plot event
    assert len(db.development(user,sid))==1
    assert {r['field'] for r in db.profile_fields(user,sid)} == {'values'}
    assert db.recent_turns(sid,1)[0]['bot_text']==reply


def test_memory_failure_is_visible_and_retriable(tmp_path,monkeypatch):
    db=Store(str(tmp_path/'test.sqlite3'))
    sid=db.new_session(1,'Начало')
    tid=db.add_turn(sid,'Кирай пошла','Авинель устала','g',1,1)
    class Fail:
        async def create(self,**kwargs):
            raise ValueError('test API down')
    engine=RoleplayEngine(db,NS(responses=Fail()),'gen','mem')
    monkeypatch.setenv('MEMORY_FALLBACK_POLZA','false')
    import pytest
    with pytest.raises(RuntimeError):
        asyncio.run(engine.memorize(sid,tid,'Кирай пошла','Авинель устала'))
    assert db.memory_health(sid)['failed']==1
    assert db.turns_for_memory_sync(sid)[0]['id']==tid


def test_navigational_messages_use_edit_instead_of_per_click_send():
    base=Path(__file__).parents[1]
    assert (base/'menu_navigation.py').exists()
    for module in ('world_ui.py','interface.py'):
        src=(base/module).read_text()
        assert 'InlineMenuMessage(callback.message)' in src
    assert "('⚙️ Ещё настройки', 'ui:tools')" in (base/'interface.py').read_text()
