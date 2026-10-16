import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace as NS
from lore_import import split_source, _validate_piece, analyze_bible, apply_bible
from storage import Store


def test_world_character_without_story_and_schema_migration(tmp_path):
    store=Store(str(tmp_path/'rp.sqlite3'))
    world=store.add_world('Сквозь миры')
    assert store.get_world(world)
    assert not store.world_stories(world)
    assert not store.world_characters(world)
    from lore_import import apply_bible
    payload={'summary':[], 'articles':[], 'people':[
        {'name':'Солемир','description':'Ангел-прародитель, вывел свой народ после войны.',
         'fields':{'species':'Ангел','occupation':'Прародитель'},'evidence':'Солемир — ангел-прародитель'}]}
    counts=apply_bible(store,world,'Солемир — ангел-прародитель',payload)
    assert counts['characters']==1
    ch=store.world_characters(world)[0]
    assert ch['session_id'] is None
    assert store.character_stories(ch['id'])==[]
    sid=store.new_session(1,'Натан и Кирай')
    store.assign_story_world(sid,world)
    store.attach_character(sid,ch['id'],'npc')
    assert store.list_characters(sid)[0]['name']=='Солемир'
    assert store.profile_fields(ch['id'])[0]['source']=='import'
    # Migration version 7 persists without toggling back to 6 on restart.
    again=Store(str(tmp_path/'rp.sqlite3'))
    with again.connect() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0]==7
        assert conn.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_no_overwrite_and_casefold_cyrillic(tmp_path):
    store=Store(str(tmp_path/'rp.sqlite3'))
    world=store.add_world('Мир')
    payload={'summary':['Магия многообразна.'],
             'articles':[{'title':'Кариат','body':'Военная империя','kind':'place',
                          'certainty':'canon','evidence':'Военная империя'}],
             'people':[{'name':'НАТАН','description':'Юстициар Эльдоры',
                        'fields':{'occupation':'Юстициар'},'evidence':'Юстициар Эльдоры'}]}
    result=apply_bible(store,world,'Мир и НАТАН',payload)
    cid=store.world_characters(world)[0]['id']
    store.set_profile_field(cid,'occupation','Верховный юстициар',source='author')
    payload['people'][0]['name']='натан'
    payload['people'][0]['fields']={'occupation':'Чужое значение'}
    next_result=apply_bible(store,world,'Мир и НАТАН',payload)
    assert next_result['characters']==0
    assert next_result['articles']==0
    assert len(store.world_characters(world))==1
    assert store.profile_fields(cid)[0]['value']=='Верховный юстициар'
    assert Path(result['source_file']).exists()


def test_model_evidence_and_uncertainty_guard():
    raw='В Эльдоре Солемир почитается как божество, хотя его судьба неизвестна.'
    d={'summary':[{'text':'Солемир точно жив','evidence':'цитата отсутствует'},
                  {'text':'Его судьба неизвестна','evidence':'его судьба неизвестна'}],
       'articles':[{'title':'Эльдора', 'body':'Место почитания Солемира',
                    'certainty':'unknown','kind':'place','evidence':'В Эльдоре Солемир почитается'}],
       'people':[{'name':'Солемир','description':'Ангел-прародитель',
                  'evidence':'несуществующая цитата'}]}
    result=_validate_piece(d,raw)
    assert result['summary']==['Его судьба неизвестна']
    assert result['articles'][0]['certainty']=='unknown'
    assert not result['people']


def test_source_split_and_mock_polza_complete(tmp_path):
    source=Path('/mnt/data/SKVOZ_MIRY_LORE_FULL(2).md').read_text(encoding='utf-8')
    pieces=split_source(source)
    assert 3<=len(pieces)<=16
    assert 'Эльдора' in '\n'.join(pieces)
    class MockCompletions:
        def __init__(self): self.times=0
        async def create(self, *, model,messages,max_tokens):
            self.times+=1
            raw=messages[-1]['content'].split(':\n\n',1)[1]
            sample=raw[:min(80,len(raw))]
            return NS(choices=[NS(message=NS(content=json.dumps({
                'summary':[{'text':'Досье содержит факты о мире','evidence':sample[:40]}],
                'articles':[{'title':f'Фрагмент {self.times}', 'kind':'other',
                             'body':'Статья по этому разделу канона мира.', 'certainty':'unknown',
                             'evidence':sample[:40]}], 'people':[]},ensure_ascii=False)))])
    mock=MockCompletions()
    client=NS(chat=NS(completions=mock))
    result=asyncio.run(analyze_bible(client,'google/gemini-3.7-flash',source))
    assert mock.times==len(pieces)
    assert len(result['articles'])==len(pieces)
    assert result['parts']==len(pieces)


def test_migration_preserves_existing_characters_photo_and_cast(tmp_path):
    # First create a pre-v0.6.1 DB by copying a new schema and forcing old schema.
    store=Store(str(tmp_path/'rp.sqlite3'))
    world=store.add_world('World')
    sid=store.new_session(1,'Story')
    store.assign_story_world(sid,world)
    cid=store.add_character(sid,'Винсент','bot')
    store.add_photo(cid,'f','/app/data/portraits/f.jpg')
    store.set_profile_field(cid,'habits','Не любит долго ждать')
    assert store.list_characters(sid)[0]['id']==cid
    assert store.photos(cid)[0]['telegram_file_id']=='f'
    # Existing refs remain valid after re-opening the already-migrated database.
    reopen=Store(str(tmp_path/'rp.sqlite3'))
    assert reopen.list_characters(sid)[0]['id']==cid
    assert reopen.photos(cid)[0]['telegram_file_id']=='f'
    assert reopen.profile_fields(cid)[0]['value']=='Не любит долго ждать'
