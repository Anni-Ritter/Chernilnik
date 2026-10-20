"""Private Telegram roleplay bot. Run: python bot.py"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BufferedInputFile, FSInputFile, Message, CallbackQuery,
                           InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ReplyKeyboardRemove)
from dotenv import load_dotenv
from openai import AsyncOpenAI

from typing_status import typing_during
from engine import RoleplayEngine
from storage import Store
from db_safety import resolve_database_path
from generation_models import GenerationSettings, POLZA_BASE_URL, DEFAULT_POLZA_MODEL

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

TOKEN = os.getenv('BOT_TOKEN', '').strip()
API_KEY = os.getenv('OPENAI_API_KEY', '').strip()
POLZA_KEY = os.getenv('POLZA_API_KEY', '').strip()
OWNER = int(os.getenv('OWNER_TELEGRAM_ID') or 0)
MODEL = os.getenv('OPENAI_MODEL', 'gpt-6-luna')
MEMORY_MODEL = os.getenv('MEMORY_MODEL', 'gpt-6-luna')
DB_PATH = resolve_database_path(allow_new=os.getenv('ALLOW_NEW_DATABASE', '').lower() in ('1', 'true', 'yes'))
log.info('Using SQLite database: %s', DB_PATH)
AUTO_MEMORY = os.getenv('AUTO_MEMORY', 'true').lower() in ('1', 'true', 'yes', 'on')
MAX_OUTPUT = int(os.getenv('MAX_OUTPUT_TOKENS', '8192'))

router = Router()
db = Store(DB_PATH)
openai_client = AsyncOpenAI(api_key=API_KEY or 'missing-api-key')
engine = RoleplayEngine(db, openai_client, MODEL, MEMORY_MODEL, AUTO_MEMORY, MAX_OUTPUT)
model_settings = GenerationSettings(
    os.getenv('GENERATION_MODEL_CONFIG') or os.path.join(os.path.dirname(DB_PATH), 'generation_model.json'),
    openai_model=MODEL,
    default_provider=os.getenv('GENERATION_PROVIDER', 'polza').strip().lower(),
    default_model=os.getenv('GENERATION_MODEL', DEFAULT_POLZA_MODEL).strip(),
)
polza_client = (AsyncOpenAI(api_key=POLZA_KEY,
                           base_url=os.getenv('POLZA_BASE_URL', POLZA_BASE_URL))
                if POLZA_KEY else None)
engine.configure_generation(model_settings, polza_client)
story_lock = asyncio.Lock()

HELP = (
    '✒️ Чернильник — литературный соавтор.\n\n'
    'Напиши «хочу новую историю» или /new. Я спрошу мир, выслушаю твою идею, '
    'приму описание героев и портреты. Скажешь «погнали» — начну писать.\n\n'
    '/new — начать новую историю разговором\n'
    '/worlds — миры и энциклопедия\n'
    '/sessions — выбрать существующую историю\n'
    '/menu — все дополнительные настройки\n'
    '/model — модель генерации\n'
    '/backup — копия SQLite\n'
    '/memoryhealth — проверить автоматическую память\n'
    '/syncmemory — восстановить память по прошлым ходам\n'
    '/cancelsetup — отменить подготовку\n'
    '/help — эта справка\n\n'
    'В уже начатой истории обычные сообщения считаются ролевыми ходами. '
    'Никаких перегенераций или выбора следующего события.'
)


def author(message: Message) -> bool:
    return bool(message.from_user and message.chat.type == ChatType.PRIVATE and OWNER and message.from_user.id == OWNER)


async def ensure_access(message: Message) -> bool:
    if not author(message):
        await message.answer('Это личный бот. Если ты владелец, проверь OWNER_TELEGRAM_ID в .env. /id покажет ID.')
        return False
    return True


def current(user_id: int):
    return db.ensure_current_session(user_id)


def begin_keyboard(sid: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='✒️ Начать историю — первый пост бота', callback_data=f'begin:{sid}')
    ]])


async def send_long(message: Message, content: str):
    """Respect Telegram's 4096 character limit without changing story text."""
    max_size = 3900
    while content:
        if len(content) <= max_size:
            await message.answer(content)
            break
        cut = content.rfind('\n', 0, max_size)
        if cut < max_size // 2:
            cut = content.rfind(' ', 0, max_size)
        if cut < max_size // 2:
            cut = max_size
        await message.answer(content[:cut])
        content = content[cut:].lstrip('\n')


@router.message(Command('id'))
async def cmd_id(message: Message):
    if message.from_user and message.chat.type == ChatType.PRIVATE:
        await message.answer(f'Твой Telegram ID: {message.from_user.id}')


@router.message(CommandStart())
async def cmd_start(message: Message):
    if not await ensure_access(message):
        return
    session = db.current_session(message.from_user.id)
    active_title = ('Сейчас открыта история «'+session['title']+'».\n') if session else ''
    await message.answer('✒️ Чернильник\n\n'+active_title+
                         'Хочешь новую историю? Просто расскажи идею или напиши /new. '
                         'Описание и картинки можно присылать несколькими сообщениями. '
                         '«Погнали» — и я начну.\n\n'
                         'Все настройки — в команде /menu.',
                         reply_markup=ReplyKeyboardRemove())


@router.message(Command('help'))
async def cmd_help(message: Message):
    if await ensure_access(message):
        await message.answer(HELP)


@router.message(Command('menu'))
async def cmd_menu(message: Message):
    if not await ensure_access(message):
        return
    from interface import main_menu
    await message.answer('⚙️ Меню Чернильника · дополнительные возможности',
                         reply_markup=main_menu())


@router.message(Command('hidekeyboard'))
async def cmd_hide_keyboard(message: Message):
    if await ensure_access(message):
        await message.answer('Нижнюю клавиатуру скрыла. Все функции — через команды /menu.',
                             reply_markup=ReplyKeyboardRemove())


@router.message(Command('new'))
async def cmd_new(message: Message, command: CommandObject):
    if not await ensure_access(message):
        return
    # quick_welcome is bound during main() before polling starts.
    if quick_welcome is None:
        await message.answer('Подготовка ещё запускается. Попробуй ещё раз.')
        return
    await quick_welcome(message, message.from_user.id, title=(command.args or '').strip())


quick_welcome = None


@router.message(Command('sessions'))
async def cmd_sessions(message: Message):
    if not await ensure_access(message):
        return
    from interface import keyboard
    active = current(message.from_user.id)['id']
    rows = [[('✨ Новая история', 'qs:new')]]
    for story in db.list_sessions()[:30]:
        label = ('✅ ' if story['id'] == active else '📖 ') + story['title'][:44]
        rows.append([(label, f'ui:session:{story["id"]}')])
    rows.append([('← Меню','ui:home')])
    await message.answer('📖 Выбери историю или начни новую.',reply_markup=keyboard(rows))


@router.message(Command('switch'))
async def cmd_switch(message: Message, command: CommandObject):
    if not await ensure_access(message):
        return
    try:
        sid = int((command.args or '').strip())
    except ValueError:
        await message.answer('Формат: /switch 2')
        return
    ok = db.switch_session(message.from_user.id, sid)
    if ok:
        from quickstart_core import QuickDrafts
        from pathlib import Path
        QuickDrafts(Path(db.path).parent/'quickstarts').clear(message.from_user.id)
    await message.answer('История переключена.' if ok else 'Такой истории нет.')


async def set_field(message: Message, command: CommandObject, field: str, syntax: str):
    if not await ensure_access(message):
        return
    value = (command.args or '').strip()
    if not value:
        await message.answer(syntax)
        return
    sid = current(message.from_user.id)['id']
    db.set_session_field(sid, field, value)
    if not db.get_opening(sid) and not db.recent_turns(sid, 1):
        await message.answer('Записано для этой истории. Когда будешь готова, начинаю первым.',
                             reply_markup=begin_keyboard(sid))
    else:
        await message.answer('Записано для текущей истории. Это не меняет другие ролевые.')


@router.message(Command('world'))
async def cmd_world(message: Message, command: CommandObject):
    await set_field(message, command, 'world', '/world краткий канон мира')


@router.message(Command('cast'))
async def cmd_cast(message: Message, command: CommandObject):
    await set_field(message, command, 'cast', '/cast Я играю за N, бот за M; бот управляет NPC')


@router.message(Command('time'))
async def cmd_time(message: Message, command: CommandObject):
    await set_field(message, command, 'scene_time', '/time Третий день зимы; сегодня вечер')


@router.message(Command('location'))
async def cmd_location(message: Message, command: CommandObject):
    await set_field(message, command, 'current_location', '/location покинутая башня')


@router.message(Command('remember'))
async def cmd_remember(message: Message, command: CommandObject):
    if not await ensure_access(message):
        return
    value = (command.args or '').strip()
    if len(value) < 9:
        await message.answer('Пример: /remember Ивар похитил Мирру и скрывает это от остальных.')
        return
    session = current(message.from_user.id)
    db.add_fact(session['id'], value, category='canon', importance=5, pinned=True)
    await message.answer('Закреплено в долговременной памяти этой истории (приоритет 5/5).')


@router.message(Command('memory'))
async def cmd_memory(message: Message):
    if not await ensure_access(message):
        return
    rows = db.list_facts(current(message.from_user.id)['id'])
    if not rows:
        await message.answer('Пока нет структурированных фактов. Тексты ходов сохраняются отдельно и полностью.')
        return
    lines = [f'{"★ " if row["pinned"] else ""}[{row["importance"]}/5 {row["certainty"]}] {row["text"]}'
             for row in rows[:22]]
    await send_long(message, '\n\n'.join(lines) + (f'\n\n…ещё {len(rows) - 22} записей' if len(rows) > 22 else ''))



async def sync_memory(message: Message, uid: int, limit: int = 12):
    """Recover past memory without touching immutable RP turns or generating new prose."""
    if not AUTO_MEMORY:
        await message.answer('Автоматическая память выключена. Установи AUTO_MEMORY=true на BotHost.')
        return
    if story_lock.locked():
        await message.answer('Сейчас обрабатывается игровой пост. Повтори команду позже.')
        return
    async with story_lock:
        sid = current(uid)['id']
        rows = db.turns_for_memory_sync(sid, limit)
        opening = db.get_opening(sid)
        process_opening = opening is not None and db.memory_job(sid, None) is None
        if not rows and not process_opening:
            await message.answer('✅ Пропущенных или неудачных записей памяти не найдено.')
            return
        total = len(rows) + int(process_opening)
        status = await message.answer(f'🧠 Восстанавливаю память: 0/{total} ходов. '
                                      'Игровые сообщения не изменяются.')
        ok = 0
        failures = []
        async with typing_during(message.bot, message.chat.id):
            if process_opening:
                try:
                    await engine.memorize(sid, None, '', opening['bot_text'])
                    ok += 1
                except Exception:
                    log.exception('Memory sync failed for story opening')
                    failures.append('начальный пост')
            for idx, row in enumerate(rows, start=1):
                try:
                    await engine.memorize(sid, row['id'], row['user_text'], row['bot_text'])
                    ok += 1
                except Exception:
                    log.exception('Memory sync failed for turn %s', row['id'])
                    failures.append('#'+str(row['id']))
                if idx % 3 == 0:
                    try:
                        await status.edit_text(f'🧠 Восстанавливаю память: {idx + int(process_opening)}/{total}.')
                    except Exception:
                        pass
        health = db.memory_health(sid)
        tail = ('\nНе удалось обработать: '+', '.join(failures[:8])+'. Подробнее — в логах BotHost.'
                if failures else '')
        try:
            await status.edit_text(f'🧠 Восстановление завершено: {ok}/{total}.\n'
                                   f'Успешно проиндексировано: {health["done"]} из {health["total"]} ходов.\n'
                                   f'Осталось без памяти: {health["unprocessed"]}; ошибок: {health["failed"]}.'
                                   + tail)
        except Exception:
            await message.answer(f'🧠 Обработано {ok}/{total}.' + tail)


@router.message(Command('memoryhealth'))
async def cmd_memory_health(message: Message):
    if not await ensure_access(message):
        return
    sid = current(message.from_user.id)['id']
    health = db.memory_health(sid)
    chars = db.list_characters(sid)
    details = ', '.join(c['name'] for c in chars[:8]) or 'нет привязанных персонажей'
    await message.answer('🧠 Состояние памяти текущей истории\n'
                         f'Автоматическая память: {"включена" if AUTO_MEMORY else "ВЫКЛЮЧЕНА"}\n'
                         f'Ходов всего: {health["total"]}\n'
                         f'Обработано: {health["done"]}\n'
                         f'Не обработано: {health["unprocessed"]}; ошибок: {health["failed"]}\n'
                         f'Участники для анкет: {details}\n\n'
                         'Чтобы дополнить память по прежним ходам — /syncmemory. '
                         'Подробности ошибок — в логах BotHost.')


@router.message(Command('syncmemory'))
async def cmd_sync_memory(message: Message, command: CommandObject):
    if not await ensure_access(message):
        return
    try:
        count = max(1,min(25,int((command.args or '12').strip())))
    except ValueError:
        await message.answer('Использование: /syncmemory 12 (от 1 до 25 ходов)')
        return
    await sync_memory(message, message.from_user.id, count)

@router.message(Command('model'))
async def cmd_model(message: Message):
    if not await ensure_access(message):
        return
    from interface import model_menu_text, model_menu
    await message.answer(model_menu_text(engine), reply_markup=model_menu(engine))


@router.message(Command('status'))
async def cmd_status(message: Message):
    if not await ensure_access(message):
        return
    session = current(message.from_user.id)
    turns = db.recent_turns(session['id'], 999999)
    opening = db.get_opening(session['id'])
    usage_in = sum(row['input_tokens'] for row in turns) + (opening['input_tokens'] if opening else 0)
    usage_out = sum(row['output_tokens'] for row in turns) + (opening['output_tokens'] if opening else 0)
    memory_uses = db.usage_summary(session['id'])
    memory_in = sum(r['input_tokens'] for r in memory_uses)
    memory_out = sum(r['output_tokens'] for r in memory_uses)
    await message.answer(f'История #{session["id"]}: {session["title"]}\n' 
                         f'Ходов: {len(turns)}; первый пост: {"есть" if opening else "нет"}\n'
                         f'Фактов: {len(db.list_facts(session["id"], 999999))}\n'
                         f'Время: {session["scene_time"] or "не задано"}\n'
                         f'Модель текста: {engine.generation_choice().display()}; память: OpenAI/{MEMORY_MODEL}\n'
                         f'Токены генерации: вход {usage_in}, выход {usage_out}.\n'
                         f'Токены памяти: вход {memory_in}, выход {memory_out}.')


@router.message(Command('prompt'))
async def cmd_prompt(message: Message):
    if not await ensure_access(message):
        return
    sid = current(message.from_user.id)['id']
    record = db.get_prompt_audit(sid)
    if record is None:
        await message.answer('Отладочный запрос ещё не записан. После следующей генерации '
                             'он появится здесь, если PROMPT_DEBUG=true.')
        return
    content = record['prompt_text'].encode('utf-8')
    await message.answer_document(
        BufferedInputFile(content, filename=f'prompt_story_{sid}.txt'),
        caption='Последний запрос к модели для этой истории. Содержит приватный канон и тайны героев.'
    )


@router.message(Command('backup'))
async def cmd_backup(message: Message):
    if not await ensure_access(message):
        return
    try:
        path = db.make_backup()
        await message.answer_document(FSInputFile(str(path)),
            caption='Резервная копия SQLite. Фото-референсы нужно сохранять отдельно из /app/data/portraits.')
    except Exception:
        log.exception('Manual backup failed')
        await message.answer('Не удалось сделать или отправить резервную копию. Проверь логи и свободное место BotHost.')


@router.message(Command('last'))
async def cmd_last(message: Message):
    if not await ensure_access(message):
        return
    rows = db.recent_turns(current(message.from_user.id)['id'], 1)
    if rows:
        await send_long(message, rows[-1]['bot_text'])
    else:
        opening = db.get_opening(current(message.from_user.id)['id'])
        if opening:
            await send_long(message, opening['bot_text'])
        else:
            await message.answer('В этой истории ещё нет ходов.')


@router.message(Command('opening'))
async def cmd_opening(message: Message):
    if not await ensure_access(message):
        return
    opening = db.get_opening(current(message.from_user.id)['id'])
    if opening:
        await send_long(message, opening['bot_text'])
    else:
        await message.answer('Начального поста пока нет. Используй /begin.')


async def start_story(message: Message, user_id: int, requested_sid=None):
    async with story_lock:
        session = current(user_id)
        sid = session['id']
        if requested_sid is not None and sid != requested_sid:
            await message.answer('Эта кнопка относится к другой истории. Переключись через /switch ID.')
            return
        if db.get_opening(sid):
            await message.answer('В этой истории я уже написал первый пост. /opening покажет его повторно.')
            return
        if db.recent_turns(sid, 1):
            await message.answer('Эта история уже началась. Нельзя создавать новый первый пост поверх сыгранных ходов.')
            return
        try:
            async with typing_during(message.bot, message.chat.id):
                opening, inp, out = await engine.generate_opening(sid)
            db.add_opening(sid, opening, engine.last_generation_model, inp, out)
        except Exception:
            log.exception('Opening generation or persistence failed')
            await message.answer('Не удалось начать историю. Начальный пост не сохранён; '
                                 'проверь API и логи Bothost, затем снова /begin.')
            return
        try:
            await send_long(message, opening)
        except Exception:
            log.exception('Opening saved but Telegram delivery failed. Recover with /opening')
        try:
            await engine.memorize(sid, None, '', opening)
        except Exception:
            log.exception('Opening memory extraction failed. The opening is safely stored.')


@router.message(Command('begin'))
async def cmd_begin(message: Message):
    if await ensure_access(message):
        await start_story(message, message.from_user.id)


@router.callback_query(F.data.startswith('begin:'))
async def callback_begin(callback: CallbackQuery):
    if not callback.from_user or callback.from_user.id != OWNER:
        await callback.answer('Недоступно.', show_alert=True)
        return
    try:
        sid = int(callback.data.split(':', 1)[1])
    except (ValueError, IndexError, AttributeError):
        await callback.answer('Неверная кнопка.', show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        await start_story(callback.message, callback.from_user.id, requested_sid=sid)


@router.message(Command('export'))
async def cmd_export(message: Message):
    if not await ensure_access(message):
        return
    sid = current(message.from_user.id)['id']
    content = db.export(sid).encode('utf-8')
    await message.answer_document(BufferedInputFile(content, filename=f'roleplay_{sid}.md'))


@router.message(Command('draft'))
async def cmd_draft(message: Message):
    if not await ensure_access(message):
        return
    sid = current(message.from_user.id)['id']
    db.start_draft(message.from_user.id, sid)
    from interface import draft_menu
    await message.answer('Режим длинного поста. Присылай части отдельными сообщениями. '
                         'Для отправки используй /play или кнопки под этим сообщением.',
                         reply_markup=draft_menu())


@router.message(Command('cancel'))
async def cmd_cancel(message: Message):
    if not await ensure_access(message):
        return
    db.clear_draft(message.from_user.id)
    from interface import MAIN_KEYS
    await message.answer('Черновик удалён. История не менялась.', reply_markup=MAIN_KEYS)


async def play(message: Message, post: str, user_id: int | None = None):
    async with story_lock:
        session = current(user_id if user_id is not None else message.from_user.id)
        if not db.get_opening(session['id']) and not db.recent_turns(session['id'], 1):
            await message.answer('Сначала я должен написать первый пост. Используй /begin или открой /menu. '
                                 'Твой текст не потерян: отправь его после начала истории.')
            return False
        try:
            async with typing_during(message.bot, message.chat.id):
                answer, inp, out = await engine.generate(session['id'], post)
        except Exception:
            log.exception('Text generation failed')
            await message.answer('Не удалось получить ответ от API. Ход не сохранён; '
                                 'сообщение можно отправить повторно. Проверь лог сервера и баланс API.')
            return False
        turn_id = db.add_turn(session['id'], post, answer, engine.last_generation_model, inp, out)
        try:
            await send_long(message, answer)
        except Exception:
            log.exception('Telegram send failed; use /last to recover (no regeneration)')
            # The turn is already committed; memory still needs updating.
        try:
            await engine.memorize(session['id'], turn_id, post, answer)
        except Exception:
            log.exception('Memory extraction failed for turn %s; story is intact', turn_id)
            # A memory failure must not rewrite or regenerate the story.
        return True


@router.message(Command('play'))
async def cmd_play(message: Message):
    if not await ensure_access(message):
        return
    draft = db.draft_active(message.from_user.id)
    if not draft or not draft['text'].strip():
        await message.answer('Нет текста. Сначала /draft, затем отправь части поста.')
        return
    if draft['session_id'] != current(message.from_user.id)['id']:
        await message.answer('Черновик относится к другой истории. /cancel и создай его заново.')
        return
    if await play(message, draft['text']):
        db.clear_draft(message.from_user.id)
        from interface import MAIN_KEYS
        await message.answer('✅ Пост отправлен.', reply_markup=MAIN_KEYS)


@router.message(F.text)
async def on_text(message: Message):
    if not await ensure_access(message):
        return
    text = (message.text or '').strip()
    if text.startswith('/'):
        await message.answer('Неизвестная команда. /help — список доступных.')
        return
    draft = db.draft_active(message.from_user.id)
    if draft:
        db.append_draft(message.from_user.id, text)
        from interface import draft_menu
        await message.answer('Часть добавлена в черновик. Продолжай писать или нажми /play.',
                             reply_markup=draft_menu())
        return
    await play(message, text)


async def main():
    if not TOKEN or not API_KEY or not OWNER:
        raise SystemExit('Укажи BOT_TOKEN, OPENAI_API_KEY и OWNER_TELEGRAM_ID в переменных окружения или .env.')
    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    from interface import register_interface
    from world_ui import register_world_ui
    from quickstart_ui import register_quickstart
    global quick_welcome
    conversation_router, _drafts, quick_welcome, _world_picker = await register_quickstart(
        db, engine, OWNER, start_story)
    dp.include_router(await register_world_ui(db, OWNER, engine))
    dp.include_router(conversation_router)
    dp.include_router(await register_interface(db, engine, OWNER, start_story, play, sync_memory))
    dp.include_router(router)
    # Telegram's slash-command menu replaces the permanently expanded keyboard.
    try:
        await bot.set_my_commands([
            BotCommand(command='new', description='Новая история — просто расскажи идею'),
            BotCommand(command='worlds', description='Миры и общий канон'),
            BotCommand(command='sessions', description='Открыть другую историю'),
            BotCommand(command='menu', description='Дополнительные настройки'),
            BotCommand(command='draft', description='Длинный пост: собрать по частям'),
            BotCommand(command='play', description='Отправить собранный пост'),
            BotCommand(command='model', description='Модель художественного текста'),
            BotCommand(command='memoryhealth', description='Проверить память истории'),
            BotCommand(command='syncmemory', description='Восстановить память по ходам'),
            BotCommand(command='backup', description='Скачать копию базы'),
            BotCommand(command='cancelsetup', description='Отменить подготовку истории'),
            BotCommand(command='help', description='Справка'),
        ])
    except Exception:
        log.exception('Telegram commands menu setup failed; regular slash commands still work')
    # Telegram retains custom reply keyboards until the bot explicitly removes them.
    # Send one cleanup message on first deployment of this hotfix, not on every restart.
    marker = Path(DB_PATH).parent / '.reply_keyboard_removed_v071'
    if not marker.exists():
        try:
            await bot.send_message(OWNER, 'Убрала нижнюю панель кнопок. Теперь просто пиши мне; '
                                   'дополнительные функции — в /menu.',
                                   reply_markup=ReplyKeyboardRemove())
            marker.touch()
        except Exception:
            log.exception('Cannot hide old Telegram reply keyboard on startup; /hidekeyboard is available')
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())
