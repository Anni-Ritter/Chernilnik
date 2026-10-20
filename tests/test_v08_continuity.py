"""v0.8: latest story state, not duplicated or superseded historical memories."""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from continuity import validated_scene_updates
from engine import RoleplayEngine
from storage import Store
from prompting import PromptComposer


def case(tmp_path):
    db = Store(str(tmp_path/'roleplay.sqlite3'))
    sid = db.new_session(17, 'Кирай и Авинель')
    db.add_character(sid, 'Авинель', 'bot')
    db.add_character(sid, 'Кирай', 'user')
    return db, sid


def item(scope, name, aspect, value, evidence):
    return {'scope':scope, 'name':name, 'aspect':aspect,
            'value':value, 'evidence':evidence}


def test_eyepain_and_location_are_replaced_without_erasing_history(tmp_path):
    db,sid=case(tmp_path)
    cast=db.list_characters(sid)
    first='В Севтосе Авинель видела фиолетовое марево, а глаз причинял невыносимую боль.'
    old=db.add_turn(sid,'Кирай наблюдала за ней.',first,'gemini',1,2)
    before=validated_scene_updates({'scene_updates':[
        item('character','Авинель','pain','глаз сильно болит','глаз причинял невыносимую боль'),
        item('character','Авинель','vision','искажённое фиолетовое зрение','видела фиолетовое марево')
    ]},'',first,cast)
    assert db.update_scene(sid,old,before)==2
    author=('Кирай поцеловала Авинель, и магия устранила боль. '
            'Зрение Авинель вернулось в норму, а корабль уже вышел в открытое море.')
    new=db.add_turn(sid,author,'Авинель молчала, чувствуя перемену.','gemini',1,2)
    changes=validated_scene_updates({'scene_updates':[
        item('character','Авинель','pain','болезненных ощущений больше нет','магия устранила боль'),
        item('character','Авинель','vision','обычное зрение','Зрение Авинель вернулось в норму'),
        item('scene','','location','на корабле в открытом море','корабль уже вышел в открытое море')
    ]},author,'Авинель молчала, чувствуя перемену.',cast)
    assert db.update_scene(sid,new,changes)==3
    snapshot=db.scene_snapshot(sid)
    assert 'обычное зрение' in snapshot and 'больше нет' in snapshot
    assert 'на корабле в открытом море' in snapshot
    assert 'искажённое фиолетовое зрение' not in snapshot
    assert db.recent_turns(sid,2)[0]['bot_text']==first
    assert len(db.scene_state(sid))==3
    with db.connect() as con:
        assert con.execute('SELECT COUNT(*) FROM scene_state_history').fetchone()[0]==5


def test_old_sync_cannot_revert_newer_milestone(tmp_path):
    db,sid=case(tmp_path)
    new=db.add_turn(sid, 'Корабль отплыл.', 'Мы в море.','x',0,0)
    db.update_scene(sid,new,[dict(scope='scene',name='',aspect='location',
                                  value='в море',source='author',evidence='Корабль отплыл.')])
    db.update_scene(sid,new-1,[dict(scope='scene',name='',aspect='location',
                                  value='в порту',source='bot',evidence='причал')])
    assert 'в море' in db.scene_snapshot(sid)


def test_author_controls_her_hero_and_npc_does_not_auto_canonize(tmp_path):
    db,sid=case(tmp_path)
    a='Кирай с облегчением выдохнула: её шея больше не болела.'
    b='Авинель решила, что у Кирай начался приступ паники.'
    updates=validated_scene_updates({'scene_updates':[
       item('character','Кирай','condition','паническая атака','у Кирай начался приступ паники'),
       item('character','Кирай','pain','боли в шее нет','её шея больше не болела'),
       item('scene','','location','в Эльдоре','Авинель решила'),
    ]},a,b,db.list_characters(sid))
    assert len(updates)==1 and updates[0]['aspect']=='pain'


def test_author_dialogue_does_not_become_objective_spatial_state(tmp_path):
    db,sid=case(tmp_path)
    a='— Мы в Эльдоре, — уверенно сказала Кирай.\nНо впереди было море.'
    updates=validated_scene_updates({'scene_updates':[
       item('scene','','location','Эльдора','Мы в Эльдоре'),
       item('scene','','travel','идут по суше','впереди было море')
    ]},a,'',db.list_characters(sid))
    assert all(x['aspect'] != 'location' for x in updates)


def test_manual_fix_is_pinned_until_explicitly_released(tmp_path):
    db,sid=case(tmp_path)
    db.set_scene_override(sid,'scene','','location','На корабле за пределами Псеумы')
    t=db.add_turn(sid,'','', 'g',0,0)
    db.update_scene(sid,t,[dict(scope='scene',name='',aspect='location',
                                value='Порт Псеумы',source='bot',evidence='порт')])
    assert 'На корабле' in db.scene_snapshot(sid)
    assert Store(db.path).scene_state(sid)[0]['source']=='manual'
    db.clear_scene_override(sid,'scene','','location')
    db.update_scene(sid,t,[dict(scope='scene',name='',aspect='location',
                                value='Порт Псеумы',source='bot',evidence='порт')])
    assert 'Порт Псеумы' in db.scene_snapshot(sid)


def test_prompt_marks_old_session_location_as_historical(tmp_path):
    db,sid=case(tmp_path)
    db.set_session_field(sid,'current_location','порт отправления Псеумы')
    db.add_fact(sid,'Глаз Авинель болел в Севтосе.',importance=5)
    t=db.add_turn(sid,'Корабль вышел в море.', 'Авинель молчала.','g',1,2)
    db.update_scene(sid,t,[dict(scope='scene',name='',aspect='location',
                                value='корабль в море',source='author',evidence='Корабль вышел')])
    result=PromptComposer(db,lambda x:'Авинель и Кирай').assemble(sid,'Кирай спросила о боли.')
    assert 'АКТУАЛЬНОЕ СОСТОЯНИЕ' in result.user
    assert 'корабль в море' in result.user
    assert 'ИЗНАЧАЛЬНАЯ / РУЧНАЯ ЛОКАЦИЯ' in result.user
    assert 'порт отправления Псеумы' in result.user
    assert 'НЕПРЕРЫВНОСТЬ СЦЕНЫ' in result.system
    assert result.user.index('АКТУАЛЬНОЕ СОСТОЯНИЕ') < result.user.index('ПОСЛЕДНИЕ ХОДЫ')


def test_one_memory_api_call_updates_scene_and_facts(tmp_path):
    db,sid=case(tmp_path)
    user='Кирай поцеловала Авинель, и боль в глазу полностью прошла.'
    bot='Авинель впервые за долгие дни видела мир без искажений.'
    tid=db.add_turn(sid,user,bot,'gemini',1,2)
    payload={'facts':[],'world_changes':[],'character_insights':[],
             'scene_updates':[
               item('character','Авинель','pain','глаз больше не болит','боль в глазу полностью прошла'),
               item('character','Авинель','vision','нормальное зрение','видела мир без искажений')
             ]}
    calls=[]
    class FakeResponse:
        async def create(self, **kw):
            calls.append(kw)
            assert 'scene_updates' in kw['instructions']
            return NS(output_text=json.dumps(payload),usage=NS(input_tokens=500,output_tokens=150))
    engine=RoleplayEngine(db,NS(responses=FakeResponse()),'gemini','luna')
    asyncio.run(engine.memorize(sid,tid,user,bot))
    assert len(calls)==1
    assert 'нормальное зрение' in db.scene_snapshot(sid)
    assert 'больше не болит' in db.scene_snapshot(sid)
    assert db.memory_job(sid,tid)['status']=='done'
    assert db.scene_jobs(sid)[0]['status']=='done'


def test_sync_scene_one_call_no_prose_or_world_changes(tmp_path):
    db,sid=case(tmp_path)
    user='Корабль уже покинул гавань и вышел в открытое море.'
    bot='Авинель смотрела на горизонт.'
    tid=db.add_turn(sid,user,bot,'gemini',1,2)
    result={'facts':[],'world_changes':[], 'character_insights':[], 'scene_updates':[
        item('scene','','location','на корабле в море','Корабль уже покинул гавань')
    ]}
    counter=[]
    class Fake:
        async def create(self,**kw):
            counter.append(kw)
            return NS(output_text=json.dumps(result),usage=NS(input_tokens=100,output_tokens=100))
    engine=RoleplayEngine(db,NS(responses=Fake()),'gemini','luna')
    assert asyncio.run(engine.rebuild_scene_snapshot(sid,12))==1
    assert len(counter)==1
    assert db.recent_turns(sid,1)[0]['bot_text']==bot
    assert 'на корабле в море' in db.scene_snapshot(sid)


def test_v08_existing_database_migrates_with_snapshot(tmp_path):
    path=tmp_path/'roleplay.sqlite3'
    db,sid=case(tmp_path)
    tid=db.add_turn(sid,'Кирай пошла.','Авинель за ней.','g',1,1)
    # emulate pre-v0.8 DB without state tables
    with db.connect() as con:
        con.execute('DROP TABLE scene_state_history')
        con.execute('DROP TABLE scene_jobs')
        con.execute('DROP TABLE scene_state')
    new_store=Store(str(path))
    assert new_store.get_session(sid)['title']=='Кирай и Авинель'
    assert new_store.recent_turns(sid,1)[0]['id']==tid
    assert new_store.scene_state(sid)==[]
    assert list((tmp_path/'backups').glob('*.sqlite3'))
    with new_store.connect() as con:
        assert con.execute('PRAGMA integrity_check').fetchone()[0]=='ok'


def test_story_snapshots_never_leak_between_stories(tmp_path):
    db,sid=case(tmp_path)
    other=db.new_session(17,'Другая история того же мира')
    db.update_scene(sid,1,[dict(scope='scene',name='',aspect='location',
                                 value='в море',source='author',evidence='в море')])
    assert not db.scene_state(other)
    assert 'в море' not in db.scene_snapshot(other)
