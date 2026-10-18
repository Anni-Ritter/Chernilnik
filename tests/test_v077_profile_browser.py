"""v0.7.7: read-before-edit profile browser and safe cancellation."""
from pathlib import Path

from profile_view import (selected_profile_fields, group_text, detail_text,
                          edit_text, group_for_field, field_marker)
from worlds import PROFILE_GROUPS
from storage import Store


def _profile_fixture(tmp_path):
    db = Store(str(tmp_path/'test.sqlite3'))
    sid = db.new_session(1,'Кирай и Авинель')
    cid = db.add_character(sid,'Авинель','bot')
    db.set_profile_field(cid,'occupation','Светоносная, командир инквизиторского отряда',sid=0)
    db.set_profile_field(cid,'values','Предана законам Эльдоры и служению Солемиру.',sid=0)
    db.set_profile_field(cid,'fears','Считает падение недопустимым.',sid=sid,
                         source='model',certainty='interpretation',evidence='Авинель не хотела отступать.')
    return db,sid,cid


def test_profile_group_shows_actual_saved_text_and_empty_fields(tmp_path):
    db,sid,cid = _profile_fixture(tmp_path)
    records=selected_profile_fields(db.profile_fields(cid,sid))
    base=group_text('Авинель','base',records)
    assert 'Светоносная, командир инквизиторского отряда' in base
    assert 'Пока не указано' in base
    mind=group_text('Авинель','mind',records)
    assert 'Предана законам Эльдоры' in mind
    assert 'Считает падение недопустимым.' in mind
    assert '≈' in mind
    assert field_marker(records['occupation']) == '✅'
    assert group_for_field('fears')=='mind'


def test_full_field_is_read_only_and_renders_complete_value(tmp_path):
    db,sid,cid=_profile_fixture(tmp_path)
    long_value='Фраза про мораль. ' * 70
    db.set_profile_field(cid,'values',long_value,sid=0)
    records=selected_profile_fields(db.profile_fields(cid,sid))
    section=group_text('Авинель','mind',records)
    assert len(section)<4096
    assert section.count('…')>=1  # only an excerpt in section
    details=detail_text('Авинель','values',records['values'])
    assert long_value.strip() in details
    assert 'Заполнено автором' in details
    assert '«-» — очистить поле' in edit_text('Авинель','values',records['values'])
    assert '«отмена»' in edit_text('Авинель','values',records['values'])


def test_author_value_has_priority_over_story_specific_guess(tmp_path):
    db,sid,cid=_profile_fixture(tmp_path)
    db.set_profile_field(cid,'occupation','Новое предположение',sid=sid,source='model')
    records=selected_profile_fields(db.profile_fields(cid,sid))
    assert records['occupation']['value']=='Светоносная, командир инквизиторского отряда'
    assert records['occupation']['source']=='author'


def test_editing_routes_are_read_first_and_cancel_resets_state():
    src=(Path(__file__).parents[1]/'world_ui.py').read_text()
    assert "f'w:field:{cid}:{key}'" in src
    assert "elif action == 'field'" in src
    assert "elif action in ('set','edit')" in src
    assert "f'w:group:{arg}:{group_for_field(key)}'" in src
    assert "if value.casefold() in ('отмена', '/cancel')" in src
    assert 'await state.clear()' in src
    assert 'db.set_profile_field(cid,key' in src


def test_clearing_story_value_requires_removing_local_record_too(tmp_path):
    db,sid,cid=_profile_fixture(tmp_path)
    assert 'fears' in selected_profile_fields(db.profile_fields(cid,sid))
    db.set_profile_field(cid,'fears','',sid=0)
    db.set_profile_field(cid,'fears','',sid=sid)
    assert 'fears' not in selected_profile_fields(db.profile_fields(cid,sid))
    assert "if value=='-' and db.story_has_character" in (Path(__file__).parents[1]/'world_ui.py').read_text()
