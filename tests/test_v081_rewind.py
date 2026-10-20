"""v0.8.1 rewind: data preservation, stale UI and story isolation."""
from __future__ import annotations

import sqlite3
import pytest

from storage import Store


def story(tmp_path):
    db = Store(str(tmp_path / 'story.sqlite3'))
    sid = db.new_session(42, 'Кирай — Авинель')
    cid = db.add_character(sid, 'Авинель', 'bot')
    wid = db.add_world('Мир 1')
    db.assign_story_world(sid,wid)
    return db,sid,cid,wid


def scene(scope, name, aspect, value, tid):
    return {'scope':scope,'name':name,'aspect':aspect,'value':value,
            'source':'author','evidence':f'Текст хода {tid}'}


def test_rollback_after_kiss_preserves_that_turn_and_reverts_memory(tmp_path):
    db,sid,cid,wid = story(tmp_path)
    before=db.add_turn(sid,'Вошли в порт.','Порт Псеумы.', 'gemini',10,20)
    db.update_scene(sid,before,[scene('scene','','location','В порту Псеумы',before)])
    kiss=db.add_turn(sid,'Кирай поцеловала Авинель. Боль исчезла.','Авинель обнажила меч.','gemini',10,20)
    db.update_scene(sid,kiss,[scene('character','Авинель','pain','Боли больше нет',kiss)])
    db.add_fact(sid,'Авинель обнажила меч возле Кирай.',source_turn_id=kiss)
    db.record_memory_job(sid,kiss,success=True)
    later=db.add_turn(sid,'Обвиняет гильдию.','Авинель тащит Кирай в Эльдору.','gemini',10,20)
    db.update_scene(sid,later,[scene('scene','','location','Снова в Эльдоре',later),
                                scene('character','Авинель','pain','Боль вернулась',later)])
    db.add_fact(sid,'Гильдия полностью распалась после угроз.',source_turn_id=later)
    db.record_memory_job(sid,later,success=True)
    db.record_scene_job(sid,later,success=True)
    db.add_development(cid,sid,later,'Авинель стала презирать гильдию ещё сильнее.','Слова Авинель','confirmed')
    db.set_profile_field(cid,'values','Новый ложный вывод после конфликта',sid=sid,source='model')
    db.add_world_event(wid,'Авинель устроила кризис после поцелуя',sid,later)
    db.add_usage(sid,'generation','gemini',10,20)
    # Second story must not be touched by rewinding the first.
    other=db.new_session(42,'Побочная ветка')
    other_id=db.add_turn(other,'другая героиня','её сцена','gemini',1,1)
    preview=db.rewind_preview(sid,kiss,True,later)
    assert preview['removed']==1
    result=db.rewind_to(sid,kiss,True,later)
    assert result.removed == 1 and result.last_kept_id == kiss
    assert [r['id'] for r in db.recent_turns(sid)]==[before,kiss]
    assert [r['id'] for r in db.recent_turns(other)]==[other_id]
    assert 'Боли больше нет' in db.scene_snapshot(sid)
    assert 'В порту Псеумы' in db.scene_snapshot(sid)
    assert 'Боль вернулась' not in db.scene_snapshot(sid)
    assert 'Эльдоре' not in db.scene_snapshot(sid)
    with db.connect() as con:
        assert con.execute('SELECT COUNT(*) FROM facts WHERE session_id=?',(sid,)).fetchone()[0]==1
        assert con.execute('SELECT COUNT(*) FROM world_events WHERE session_id=?',(sid,)).fetchone()[0]==0
        assert con.execute('SELECT COUNT(*) FROM character_development WHERE session_id=?',(sid,)).fetchone()[0]==0
        assert con.execute('SELECT COUNT(*) FROM memory_jobs WHERE session_id=?',(sid,)).fetchone()[0]==1
        assert con.execute('SELECT COUNT(*) FROM scene_jobs WHERE session_id=?',(sid,)).fetchone()[0]==0
        assert con.execute('SELECT COUNT(*) FROM character_fields WHERE session_id=?',(sid,)).fetchone()[0]==0
        assert con.execute('SELECT COUNT(*) FROM usage_log WHERE session_id=?',(sid,)).fetchone()[0]==1
        assert not con.execute('PRAGMA foreign_key_check').fetchall()
        assert con.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        # FTS delete trigger must not retain discarded narrative content.
        hits=db.search_turns(sid, 'гильдию', limit=3)
        assert all(r['id']!=later for r in hits)
    with sqlite3.connect(result.backup_path) as original:
        assert original.execute('SELECT COUNT(*) FROM turns WHERE session_id=?',(sid,)).fetchone()[0]==3
        assert original.execute('SELECT COUNT(*) FROM facts WHERE session_id=?',(sid,)).fetchone()[0]==2


def test_rollback_before_kiss_removes_kiss_and_its_derived_state(tmp_path):
    db,sid,cid,wid=story(tmp_path)
    first=db.add_turn(sid,'В порту','Тишина','gemini',1,1)
    db.update_scene(sid,first,[scene('scene','','location','Порт',first)])
    kiss=db.add_turn(sid,'Поцелуй Авинель','Ответ','gemini',1,1)
    db.update_scene(sid,kiss,[scene('character','Авинель','pain','исцелилась',kiss)])
    after=db.add_turn(sid,'Следующий пост','Ответ','gemini',1,1)
    result=db.rewind_to(sid,kiss,False,after)
    assert result.removed==2
    assert len(db.recent_turns(sid))==1
    assert 'исцелилась' not in db.scene_snapshot(sid)
    assert 'Порт' in db.scene_snapshot(sid)


def test_wrong_story_stale_preview_or_noop_cannot_mutate(tmp_path):
    db,sid,cid,wid=story(tmp_path)
    a=db.add_turn(sid,'текст','ответ','model',1,1)
    b=db.add_turn(sid,'текст2','ответ2','model',1,1)
    backups_before = set((tmp_path / 'backups').glob('*.sqlite3'))
    with pytest.raises(ValueError):
        db.rewind_to(sid,a,True,b+123)
    with pytest.raises(ValueError):
        db.rewind_to(sid,b,True,b)
    with pytest.raises(ValueError):
        db.rewind_to(sid,9999,False,b)
    assert len(db.recent_turns(sid))==2
    assert set((tmp_path / 'backups').glob('*.sqlite3')) == backups_before


def test_no_backup_means_no_rewind(tmp_path,monkeypatch):
    db,sid,cid,wid=story(tmp_path)
    a=db.add_turn(sid,'текст','ответ','model',1,1)
    b=db.add_turn(sid,'текст2','ответ2','model',1,1)
    def broken(): raise OSError('No storage space')
    monkeypatch.setattr(db,'make_backup',broken)
    with pytest.raises(OSError):
        db.rewind_to(sid,a,True,b)
    assert len(db.recent_turns(sid))==2


def test_cyrillic_search_finds_kiss_and_ignores_old_story(tmp_path):
    db,sid,cid,wid=story(tmp_path)
    db.add_turn(sid,'КИРАЙ ПОЦЕЛОВАЛА Авинель', 'ответ','gemini',1,1)
    db.add_turn(sid,'Она молчит', 'ответ','gemini',1,1)
    other=db.new_session(42,'Иное')
    db.add_turn(other,'Кирай поцеловала','второе','gemini',1,1)
    hits=db.rewind_candidates(sid,'поцелуй')
    assert len(hits)==1 and 'ПОЦЕЛОВАЛА' in hits[0]['user_text']


def test_preserve_proven_early_profile_and_manual_world_lore(tmp_path):
    db,sid,cid,wid=story(tmp_path)
    first=db.add_turn(sid,'Авинель молча сняла шлем.','Она выглядела усталой.', 'g',1,1)
    db.set_profile_field(cid,'habits','Авинель редко говорит',sid=sid,
                         source='model',evidence='Авинель молча сняла шлем.')
    db.set_profile_field(cid,'values','Обожает Священный Капитул',sid=sid,
                         source='author',evidence='')
    later=db.add_turn(sid,'Новая сцена','Авинель испугалась', 'g',1,1)
    db.set_profile_field(cid,'fears','Боится кораблей',sid=sid,
                         source='model',evidence='Авинель испугалась')
    db.add_world_lore(wid,'Государства','Севтос и Псеума существуют.')
    result=db.rewind_to(sid,later,False,later)
    fields={r['field']:r for r in db.profile_fields(cid,sid)}
    assert 'habits' in fields
    assert 'fears' not in fields
    assert fields['values']['source']=='author'
    assert any('Севтос' in r['body'] for r in db.list_world_lore(wid))
