"""v0.7.1: quiet Telegram input and evidence-backed player profiles."""
import asyncio
import json
from types import SimpleNamespace as NS
from pathlib import Path

from storage import Store
from engine import RoleplayEngine


def run_memory(tmp_path, user_post, bot_post, insights, *, author_field=False):
    store = Store(str(tmp_path / 'story.sqlite3'))
    world = store.add_world('Сквозь миры')
    sid = store.new_session(123, 'Кирай и Авинель')
    store.assign_story_world(sid, world)
    player = store.add_character(sid, 'Кирай', 'user')
    bot_character = store.add_character(sid, 'Авинель', 'bot')
    if author_field:
        store.set_profile_field(player, 'goals', 'Защитить младшую сестру',
                                sid=0, source='author')
    turn_id = store.add_turn(sid, user_post, bot_post, 'gemini', 5, 5)
    payload = {'facts': [], 'world_changes': [], 'character_insights': insights}

    class Replies:
        async def create(self, **kwargs):
            assert 'ПОЛЬЗОВАТЕЛЯ' in kwargs['instructions']
            return NS(output_text=json.dumps(payload), usage=NS(input_tokens=5, output_tokens=9))

    engine = RoleplayEngine(store, NS(responses=Replies()), 'gpt', 'luna', auto_memory=True)
    asyncio.run(engine.memorize(sid, turn_id, user_post, bot_post))
    return store, sid, player, bot_character


def item(name, field, text, evidence, certainty='confirmed'):
    return {'name': name, 'field': field, 'text': text,
            'evidence': evidence, 'certainty': certainty}


def test_player_character_automatically_populates_sheet_and_development(tmp_path):
    phrase = 'Кирай с детства боялась замкнутых помещений.'
    store, sid, player, _ = run_memory(tmp_path, phrase, 'Авинель закрыла дверь.', [
        item('Кирай', 'fears', 'Кирай боится замкнутых помещений с детства.', phrase)])
    fields = store.profile_fields(player, sid)
    assert len(fields) == 1
    assert fields[0]['source'] == 'story'
    assert fields[0]['certainty'] == 'confirmed'
    assert fields[0]['evidence'] == phrase
    assert store.development(player, sid)[0]['insight'] == 'Кирай боится замкнутых помещений с детства.'
    assert store.profile_fields(player) == []  # only this story knows, not all story variants


def test_player_is_not_defined_by_bot_or_speculation(tmp_path):
    store, sid, player, botchar = run_memory(
        tmp_path, 'Кирай опустила глаза.', 'Авинель решила, что Кирай боится огня.', [
            item('Кирай', 'fears', 'Боится огня с детства.', 'Кирай опустила глаза.', 'interpretation'),
            item('Кирай', 'fears', 'Боится огня с детства.', 'Авинель решила, что Кирай боится огня.'),
            item('Авинель', 'habits', 'Авинель наблюдала за реакцией Кирай.',
                 'Авинель решила, что Кирай боится огня.'),
        ])
    assert not store.profile_fields(player, sid)
    assert not store.development(player, sid)
    assert store.profile_fields(botchar, sid)


def test_explicit_player_goals_allowed_but_author_field_protected(tmp_path):
    phrase = 'Кирай решила выследить правителя Эльдоры.'
    store, sid, player, _ = run_memory(tmp_path, phrase, 'Авинель смотрела в окно.', [
        item('Кирай', 'goals', 'Кирай хочет выследить правителя Эльдоры.', phrase)
    ], author_field=True)
    fields = store.profile_fields(player, sid)
    assert len(fields) == 1
    assert fields[0]['value'] == 'Защитить младшую сестру'
    assert fields[0]['source'] == 'author'
    assert store.development(player, sid)  # narrative evidence not dropped


def test_no_persistent_telegram_keyboard_anywhere_in_project():
    project = Path(__file__).parents[1]
    for filename in ('bot.py', 'interface.py', 'quickstart_ui.py', 'world_ui.py'):
        assert 'ReplyKeyboardMarkup(' not in (project / filename).read_text()
    bot = (project / 'bot.py').read_text()
    assert 'ReplyKeyboardRemove()' in bot
    assert "Command('hidekeyboard')" in bot
    assert '.reply_keyboard_removed_v071' in bot
