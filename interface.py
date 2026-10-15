"""Inline-first Telegram interface for stories, character sheets and image references."""
from __future__ import annotations

import logging
import os
import sqlite3
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, BufferedInputFile,
)
from vision import MAX_IMAGE_BYTES, describe_image, image_info

log = logging.getLogger(__name__)


class Editing(StatesGroup):
    story_title = State()
    field = State()
    character_name = State()
    character_rename = State()
    character_description = State()
    character_appearance = State()
    photo = State()
    pinned_fact = State()


DRAFT_KEYS = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text='📨 Отправить пост'), KeyboardButton(text='❌ Отменить черновик')],
    [KeyboardButton(text='🗂 Меню')],
], resize_keyboard=True, input_field_placeholder='Часть длинного ролевого поста')


MAIN_KEYS = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text='🗂 Меню'), KeyboardButton(text='📖 Истории')],
        [KeyboardButton(text='👤 Персонажи'), KeyboardButton(text='✒️ Начать')],
    ], resize_keyboard=True, input_field_placeholder='Ролевой пост или кнопка меню',
)


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
        for row in rows
    ])


def main_menu():
    return keyboard([
        [('📖 Истории', 'ui:sessions'), ('👤 Персонажи', 'ui:characters')],
        [('🌍 Мир и сцена', 'ui:scene'), ('🧠 Память', 'ui:memory')],
        [('✒️ Первый пост', 'ui:begin'), ('📝 Длинный пост', 'ui:draft')],
        [('📦 Экспорт', 'ui:export'), ('📊 Статус', 'ui:status')],
    ])


def scene_menu():
    return keyboard([
        [('📜 Описание мира', 'ui:field:world'), ('🎭 Распределение ролей', 'ui:field:cast')],
        [('🕰 Время', 'ui:field:scene_time'), ('📍 Локация', 'ui:field:current_location')],
        [('← Главное меню', 'ui:home')],
    ])


def character_menu(cid: int):
    return keyboard([
        [('🖼 Фотографии', f'ui:photos:{cid}'), ('➕ Фото', f'ui:upload:{cid}')],
        [('📝 Характер и биография', f'ui:editbio:{cid}')],
        [('✏️ Переименовать', f'ui:rename:{cid}')],
        [('✏️ Уточнить внешность', f'ui:editlook:{cid}')],
        [('🎭 Кто отыгрывает', f'ui:roles:{cid}')],
        [('🗑 Удалить персонажа', f'ui:delchar:{cid}')],
        [('← Персонажи', 'ui:characters')],
    ])


async def register_interface(db, engine, owner: int, begin_story, play_story):
    """Build router with injected engine and story operations; no circular imports."""
    router = Router(name='roleplay_ui')
    media_dir = Path(os.getenv('MEDIA_DIR') or str(Path(db.path).parent / 'portraits'))
    media_dir.mkdir(parents=True, exist_ok=True)
    vision_model = os.getenv('VISION_MODEL', 'gpt-6-luna')

    def authorized_id(user, chat) -> bool:
        return bool(user and user.id == owner and chat and chat.type == 'private')

    def active(user_id):
        session = db.current_session(user_id)
        if session is None:
            sid = db.new_session(user_id, 'Свободная история')
            return db.get_session(sid)
        return session

    def active_char(cid, user_id):
        char = db.get_character(cid)
        return char if char is not None and char['session_id'] == active(user_id)['id'] else None

    async def show_menu(message: Message, user_id: int):
        session = active(user_id)
        await message.answer(f'🗂 Чернильник · «{session["title"]}»\n\n'
                             'Выбирай раздел кнопками. Обычные сообщения по-прежнему считаются ролевыми постами.',
                             reply_markup=main_menu())

    async def show_sessions(message: Message, user_id: int):
        active_id = active(user_id)['id']
        rows = [[('➕ Новая история', 'ui:new')]]
        for s in db.list_sessions()[:25]:
            title = s['title'][:33]
            rows.append([(('✅ ' if s['id'] == active_id else '📖 ') + title, f'ui:session:{s["id"]}')])
        rows.append([('← Меню', 'ui:home')])
        await message.answer('📖 Истории · выбери существующую или создай новую.', reply_markup=keyboard(rows))

    async def show_characters(message: Message, user_id: int):
        sid = active(user_id)['id']
        rows = [[('➕ Новый персонаж', 'ui:newchar')]]
        for char in db.list_characters(sid)[:35]:
            who = {'user': '👤', 'bot': '✒️', 'npc': '🎭'}[char['role']]
            rows.append([(f'{who} {char["name"][:38]}', f'ui:char:{char["id"]}')])
        rows.append([('← Меню', 'ui:home')])
        await message.answer('👤 Персонажи текущей истории.\n'
                             '👤 — твой герой; ✒️ — герой бота; 🎭 — NPC.\n'
                             'Фотографии и описания не переходят в другие истории.',
                             reply_markup=keyboard(rows))

    async def show_character(message: Message, cid: int, user_id: int):
        ch = active_char(cid, user_id)
        if ch is None:
            await message.answer('Карточка не принадлежит этой истории.')
            return
        photos = db.photos(cid)
        active_pic = next((p for p in photos if p['is_primary']), None)
        role = {'user': 'Твой персонаж', 'bot': 'Персонаж соавтора', 'npc': 'NPC соавтора'}[ch['role']]
        bio = ch['description'][:1300] or 'Пока без описания'
        author_look = ch['appearance_override'][:700] or 'Нет'
        ai_look = (active_pic['visual_description'][:1100] if active_pic and active_pic['visual_description'] else 'Нет')
        text = (f'👤 {ch["name"]}\nРоль: {role}\n\n'
                f'Характер/биография:\n{bio}\n\n'
                f'Уточнение внешности от тебя:\n{author_look}\n\n'
                f'По основному фото:\n{ai_look}\n\n'
                f'Фото: {len(photos)}/5. Эти сведения доступны модели, но не автоматически всем героям мира.')
        await message.answer(text, reply_markup=character_menu(cid))

    async def show_photos(message: Message, cid: int, user_id: int):
        ch = active_char(cid, user_id)
        if ch is None:
            await message.answer('Сначала выбери персонажа в текущей истории.')
            return
        rows = [[('➕ Добавить фото', f'ui:upload:{cid}')]]
        photos = db.photos(cid)
        for photo in photos:
            label = ('⭐ ' if photo['is_primary'] else '🖼 ') + f'Референс #{photo["id"]}'
            rows.append([(label, f'ui:photo:{photo["id"]}')])
        rows.append([('← Карточка', f'ui:char:{cid}')])
        await message.answer(f'🖼 Референсы «{ch["name"]}»: {len(photos)}/5\n'
                             'Основное фото ⭐ используется как приоритетный визуальный ориентир.',
                             reply_markup=keyboard(rows))

    async def show_photo(message: Message, pid: int, user_id: int):
        photo = db.get_photo(pid)
        if photo is None or active_char(photo['character_id'], user_id) is None:
            await message.answer('Фото не найдено в текущей истории.')
            return
        desc = (photo['visual_description'] or 'Анализ не готов. Можно запустить повторно.')[:850]
        try:
            await message.answer_photo(photo['telegram_file_id'], caption=f'🖼 Референс #{pid}')
        except Exception:
            try:
                # A document file_id cannot be sent as a Telegram photo.
                await message.answer_document(photo['telegram_file_id'], caption=f'🖼 Референс #{pid}')
            except Exception:
                log.exception('Telegram portrait preview failed')
                await message.answer('Не удалось показать фото из Telegram; его копия сохранена на хостинге.')
        rows = [
            [('⭐ Сделать основным', f'ui:primary:{pid}')],
            [('🔎 Повторить анализ', f'ui:analyze:{pid}')],
            [('🗑 Удалить фото', f'ui:delete:{pid}')],
            [('← Галерея', f'ui:photos:{photo["character_id"]}')],
        ]
        await message.answer(f'Описание по изображению:\n{desc}', reply_markup=keyboard(rows))

    async def analyze_photo(pid: int, message: Message, user_id: int):
        photo = db.get_photo(pid)
        if not photo or not active_char(photo['character_id'], user_id):
            await message.answer('Фото не найдено.')
            return
        ch = db.get_character(photo['character_id'])
        try:
            raw = Path(photo['local_path']).read_bytes()
            await message.bot.send_chat_action(message.chat.id, 'typing')
            description, inp, out = await describe_image(engine.client, vision_model, raw, ch['name'])
            db.update_photo_analysis(pid, description, 'ok')
            db.add_usage(ch['session_id'], 'vision', vision_model, inp, out)
            await message.answer(f'✅ Внешность сохранена для «{ch["name"]}».\n\n{description}',
                                 reply_markup=character_menu(ch['id']))
        except Exception:
            log.exception('Vision analysis failed, photo %d', pid)
            db.update_photo_analysis(pid, photo['visual_description'],
                                     'ok' if photo['visual_description'] else 'failed')
            await message.answer('Фото сохранено, но анализ OpenAI не удался. '
                                 'Проверь доступ к VISION_MODEL и API-баланс. '
                                 'Можно повторить анализ из карточки фото.',
                                 reply_markup=keyboard([[('🔎 Повторить анализ', f'ui:analyze:{pid}')],
                                                        [('← Карточка', f'ui:char:{ch["id"]}')]]))

    @router.message(F.text == '🗂 Меню')
    async def reply_menu(message: Message, state: FSMContext):
        if not authorized_id(message.from_user, message.chat):
            return
        await state.clear()  # convenient cancel of unfinished input
        await show_menu(message, message.from_user.id)

    @router.message(F.text == '📖 Истории')
    async def reply_sessions(message: Message, state: FSMContext):
        if authorized_id(message.from_user, message.chat):
            await state.clear()
            await show_sessions(message, message.from_user.id)

    @router.message(F.text == '👤 Персонажи')
    async def reply_chars(message: Message, state: FSMContext):
        if authorized_id(message.from_user, message.chat):
            await state.clear()
            await show_characters(message, message.from_user.id)

    @router.message(F.text == '✒️ Начать')
    async def reply_begin(message: Message, state: FSMContext):
        if authorized_id(message.from_user, message.chat):
            await state.clear()
            await begin_story(message, message.from_user.id)

    @router.message(F.text == '📨 Отправить пост')
    async def reply_draft_send(message: Message):
        if not authorized_id(message.from_user, message.chat):
            return
        uid = message.from_user.id
        draft = db.draft_active(uid)
        if not draft or not draft['text'].strip() or draft['session_id'] != active(uid)['id']:
            await message.answer('Нет черновика, который можно отправить.')
            return
        if await play_story(message, draft['text'], uid):
            db.clear_draft(uid)
            await message.answer('✅ Пост отправлен.', reply_markup=MAIN_KEYS)

    @router.message(F.text == '❌ Отменить черновик')
    async def reply_draft_cancel(message: Message):
        if authorized_id(message.from_user, message.chat):
            db.clear_draft(message.from_user.id)
            await message.answer('Черновик отменён; история не изменилась.', reply_markup=MAIN_KEYS)

    @router.callback_query(F.data.startswith('ui:'))
    async def callback_ui(callback: CallbackQuery, state: FSMContext):
        if not authorized_id(callback.from_user, callback.message.chat if isinstance(callback.message, Message) else None):
            await callback.answer('Недоступно.', show_alert=True)
            return
        await callback.answer()
        if not isinstance(callback.message, Message):
            return
        message = callback.message
        uid = callback.from_user.id
        parts = (callback.data or '').split(':')
        action = parts[1] if len(parts) > 1 else ''
        arg = parts[2] if len(parts) > 2 else ''
        if action not in ('begin', 'draftsend'):
            await state.clear()
        if action == 'home':
            await show_menu(message, uid)
        elif action == 'sessions':
            await show_sessions(message, uid)
        elif action == 'new':
            await state.set_state(Editing.story_title)
            await message.answer('📖 Как назовём историю? Пришли название одним сообщением.\n'
                                 'Для отмены нажми «🗂 Меню».')
        elif action == 'session' and arg.isdigit():
            if db.switch_session(uid, int(arg)):
                await message.answer(f'✅ Активна история «{active(uid)["title"]}».',
                                     reply_markup=main_menu())
            else:
                await message.answer('История не найдена.')
        elif action == 'characters':
            await show_characters(message, uid)
        elif action == 'newchar':
            await state.set_state(Editing.character_name)
            await message.answer('👤 Имя нового персонажа? Напиши его одним сообщением.')
        elif action == 'char' and arg.isdigit():
            await show_character(message, int(arg), uid)
        elif action == 'rename' and arg.isdigit():
            ch = active_char(int(arg), uid)
            if ch:
                await state.set_state(Editing.character_rename)
                await state.update_data(char_id=ch['id'])
                await message.answer(f'✏️ Как переименовать «{ch["name"]}»?')
        elif action == 'delchar' and arg.isdigit():
            ch = active_char(int(arg), uid)
            if ch:
                await message.answer(f'Точно удалить «{ch["name"]}» и все его референсы? '
                                     'Ролевые посты останутся в истории.', reply_markup=keyboard([
                    [('🗑 Да, удалить', f'ui:delcharyes:{arg}')],
                    [('← Не удалять', f'ui:char:{arg}')],
                ]))
        elif action == 'delcharyes' and arg.isdigit():
            ch = active_char(int(arg), uid)
            if ch:
                paths = db.delete_character(ch['session_id'], ch['id'])
                for item in paths:
                    p = Path(item)
                    if p.parent.resolve() == media_dir.resolve():
                        p.unlink(missing_ok=True)
                await message.answer('🗑 Персонаж и его фото удалены. Сыгранные посты не изменены.')
                await show_characters(message, uid)
        elif action == 'roles' and arg.isdigit():
            ch = active_char(int(arg), uid)
            if ch:
                await message.answer(f'Кто отыгрывает «{ch["name"]}»?', reply_markup=keyboard([
                    [('👤 Я', f'ui:setrole:{arg}:user'), ('✒️ Бот', f'ui:setrole:{arg}:bot')],
                    [('🎭 NPC бота', f'ui:setrole:{arg}:npc')],
                    [('← Карточка', f'ui:char:{arg}')],
                ]))
        elif action == 'setrole' and arg.isdigit() and len(parts) == 4:
            ch = active_char(int(arg), uid)
            if ch and parts[3] in ('user', 'bot', 'npc'):
                db.set_character_role(ch['id'], parts[3])
                await show_character(message, ch['id'], uid)
        elif action in ('editbio', 'editlook') and arg.isdigit():
            ch = active_char(int(arg), uid)
            if ch:
                await state.set_state(Editing.character_description if action == 'editbio'
                                      else Editing.character_appearance)
                await state.update_data(char_id=ch['id'], session_id=ch['session_id'])
                await message.answer(('📝 Напиши характер и биографию «' if action == 'editbio'
                                      else '✏️ Опиши или поправь внешность «') + ch['name'] +
                                     '». Одним сообщением, текст заменит предыдущий.')
        elif action == 'photos' and arg.isdigit():
            await show_photos(message, int(arg), uid)
        elif action == 'photo' and arg.isdigit():
            await show_photo(message, int(arg), uid)
        elif action == 'upload' and arg.isdigit():
            ch = active_char(int(arg), uid)
            if ch:
                if len(db.photos(ch['id'])) >= 5:
                    await message.answer('Максимум 5 фото. Удали ненужный референс из галереи.')
                else:
                    await state.set_state(Editing.photo)
                    await state.update_data(char_id=ch['id'], session_id=ch['session_id'])
                    await message.answer(f'🖼 Отправь фото персонажа «{ch["name"]}» обычной фотографией '
                                         'или документом JPG/PNG/WEBP (до 12 МБ).\n'
                                         'Бот сохранит файл и попробует описать внешность. '
                                         'Для отмены нажми «🗂 Меню».')
        elif action == 'primary' and arg.isdigit():
            photo = db.get_photo(int(arg))
            if photo and active_char(photo['character_id'], uid):
                db.make_primary_photo(photo['character_id'], photo['id'])
                await message.answer('⭐ Основной референс изменён.')
                await show_photos(message, photo['character_id'], uid)
        elif action == 'analyze' and arg.isdigit():
            await analyze_photo(int(arg), message, uid)
        elif action == 'delete' and arg.isdigit():
            photo = db.get_photo(int(arg))
            if photo and active_char(photo['character_id'], uid):
                await message.answer('Удалить этот референс навсегда?', reply_markup=keyboard([
                    [('Да, удалить', f'ui:deleteyes:{arg}'), ('Нет', f'ui:photo:{arg}')]
                ]))
        elif action == 'deleteyes' and arg.isdigit():
            photo = db.get_photo(int(arg))
            if photo and active_char(photo['character_id'], uid):
                removed = db.delete_photo(photo['character_id'], photo['id'])
                if removed:
                    path = Path(removed['local_path'])
                    if path.parent.resolve() == media_dir.resolve():
                        path.unlink(missing_ok=True)
                await message.answer('🗑 Референс удалён.')
                await show_photos(message, photo['character_id'], uid)
        elif action == 'scene':
            session = active(uid)
            await message.answer(f'🌍 Мир и сцена · {session["title"]}\n\n'
                                 f'Мир: {(session["world"] or "не задан")[:200]}\n'
                                 f'Роли: {(session["cast"] or "не заданы")[:220]}\n'
                                 f'Время: {session["scene_time"] or "не задано"}\n'
                                 f'Локация: {session["current_location"] or "не задана"}',
                                 reply_markup=scene_menu())
        elif action == 'field' and arg in ('world', 'cast', 'scene_time', 'current_location'):
            await state.set_state(Editing.field)
            await state.update_data(field=arg, session_id=active(uid)['id'])
            label = {'world': 'описание мира', 'cast': 'распределение ролей',
                     'scene_time': 'время и сезон', 'current_location': 'место действия'}[arg]
            await message.answer(f'✏️ Пришли {label} одним сообщением. Старое значение будет заменено.\n'
                                 'Для отмены нажми «🗂 Меню».')
        elif action == 'memory':
            sid = active(uid)['id']
            facts = db.list_facts(sid, 15)
            fact_text = '\n'.join((('★ ' if f['pinned'] else '• ') + f['text'][:180]) for f in facts) or 'Пока пусто.'
            await message.answer('🧠 Память истории:\n\n' + fact_text[:3200], reply_markup=keyboard([
                [('📌 Закрепить важный факт', 'ui:remember')],
                [('← Меню', 'ui:home')],
            ]))
        elif action == 'remember':
            await state.set_state(Editing.pinned_fact)
            await state.update_data(session_id=active(uid)['id'])
            await message.answer('📌 Напиши важный факт, который нельзя забывать.')
        elif action == 'begin':
            await begin_story(message, uid)
        elif action == 'draft':
            sid = active(uid)['id']
            existing = db.draft_active(uid)
            if not existing or existing['session_id'] != sid:
                db.start_draft(uid, sid)
            await message.answer('📝 Режим длинного поста. Присылай части обычными сообщениями. '
                                 'Когда закончишь — нажми «📨 Отправить пост» внизу.',
                                 reply_markup=DRAFT_KEYS)
        elif action == 'draftsend':
            draft = db.draft_active(uid)
            if not draft or not draft['text'].strip() or draft['session_id'] != active(uid)['id']:
                await message.answer('Нет подходящего черновика.')
            elif await play_story(message, draft['text'], uid):
                db.clear_draft(uid)
                await message.answer('✅ Пост отправлен.', reply_markup=MAIN_KEYS)
        elif action == 'draftcancel':
            db.clear_draft(uid)
            await message.answer('Черновик удалён. История не изменилась.', reply_markup=MAIN_KEYS)
        elif action == 'export':
            sid = active(uid)['id']
            await message.answer_document(BufferedInputFile(db.export(sid).encode('utf-8'),
                                                           filename=f'roleplay_{sid}.md'))
        elif action == 'status':
            sid = active(uid)['id']
            turns = db.recent_turns(sid, 999999)
            opening = db.get_opening(sid)
            uses = db.usage_summary(sid)
            await message.answer(f'📊 «{active(uid)["title"]}»\n'
                                 f'Ходов: {len(turns)}\n'
                                 f'Персонажей: {len(db.list_characters(sid))}\n'
                                 f'Фактов: {len(db.list_facts(sid))}\n'
                                 f'Первый пост: {"есть" if opening else "нет"}\n'
                                 f'Дополнительных вызовов памяти/зрения: '
                                 f'{sum(r["input_tokens"] + r["output_tokens"] for r in uses)} токенов')

    @router.message(Editing.story_title, F.text)
    async def receive_title(message: Message, state: FSMContext):
        if not authorized_id(message.from_user, message.chat):
            return
        title = message.text.strip()[:100]
        if not title:
            await message.answer('Имя не должно быть пустым.')
            return
        sid = db.new_session(message.from_user.id, title)
        await state.clear()
        await message.answer(f'✅ История #{sid} «{title}» создана.\n'
                             'Настрой мир и персонажей или сразу предложи боту начать.',
                             reply_markup=main_menu())

    @router.message(Editing.field, F.text)
    async def receive_field(message: Message, state: FSMContext):
        if not authorized_id(message.from_user, message.chat):
            return
        data = await state.get_data()
        if data.get('session_id') != active(message.from_user.id)['id']:
            await state.clear()
            await message.answer('История изменилась. Открой настройку заново.')
            return
        value = message.text.strip()
        if not value or len(value) > 12000:
            await message.answer('Пришли непустой текст до 12 000 символов.')
            return
        db.set_session_field(data['session_id'], data['field'], value)
        await state.clear()
        await message.answer('✅ Сохранено для этой истории.', reply_markup=scene_menu())

    @router.message(Editing.character_name, F.text)
    async def receive_name(message: Message, state: FSMContext):
        if not authorized_id(message.from_user, message.chat):
            return
        name = message.text.strip()
        if not (1 <= len(name) <= 80):
            await message.answer('Имя должно содержать от 1 до 80 символов.')
            return
        try:
            cid = db.add_character(active(message.from_user.id)['id'], name)
        except sqlite3.IntegrityError:
            await message.answer('Персонаж с таким именем уже существует в этой истории. Дай другое имя.')
            return
        await state.clear()
        await message.answer('✅ Персонаж создан. Выбери, кто его отыгрывает:', reply_markup=keyboard([
            [('👤 Я', f'ui:setrole:{cid}:user'), ('✒️ Бот', f'ui:setrole:{cid}:bot')],
            [('🎭 NPC', f'ui:setrole:{cid}:npc')],
        ]))

    @router.message(Editing.character_rename, F.text)
    async def receive_rename(message: Message, state: FSMContext):
        if not authorized_id(message.from_user, message.chat):
            return
        data = await state.get_data()
        cid = data.get('char_id')
        if not isinstance(cid, int) or not active_char(cid, message.from_user.id):
            await state.clear()
            await message.answer('Персонаж не найден.')
            return
        try:
            db.rename_character(cid, message.text)
        except (sqlite3.IntegrityError, ValueError):
            await message.answer('Такое имя уже есть или оно слишком длинное (максимум 80 символов).')
            return
        await state.clear()
        await show_character(message, cid, message.from_user.id)

    async def receive_char_text(message: Message, state: FSMContext, field: str):
        if not authorized_id(message.from_user, message.chat):
            return
        data = await state.get_data()
        cid = data.get('char_id')
        if not isinstance(cid, int) or not active_char(cid, message.from_user.id):
            await state.clear()
            await message.answer('Карточка устарела, выбери персонажа заново.')
            return
        value = message.text.strip()
        if not value or len(value) > 6000:
            await message.answer('Пришли текст от 1 до 6000 символов.')
            return
        db.set_character_text(cid, field, value)
        await state.clear()
        await show_character(message, cid, message.from_user.id)

    @router.message(Editing.character_description, F.text)
    async def receive_bio(message: Message, state: FSMContext):
        await receive_char_text(message, state, 'description')

    @router.message(Editing.character_appearance, F.text)
    async def receive_look(message: Message, state: FSMContext):
        await receive_char_text(message, state, 'appearance_override')

    @router.message(Editing.pinned_fact, F.text)
    async def receive_fact(message: Message, state: FSMContext):
        if not authorized_id(message.from_user, message.chat):
            return
        data = await state.get_data()
        fact = message.text.strip()
        if data.get('session_id') != active(message.from_user.id)['id']:
            await state.clear()
            await message.answer('История изменилась, факт не сохранён.')
            return
        if not (9 <= len(fact) <= 600):
            await message.answer('От 9 до 600 символов, пожалуйста.')
            return
        db.add_fact(data['session_id'], fact, category='canon', importance=5, pinned=True)
        await state.clear()
        await message.answer('📌 Факт сохранён как важный.', reply_markup=main_menu())

    @router.message(Editing.photo, F.photo | F.document)
    async def receive_photo(message: Message, state: FSMContext):
        if not authorized_id(message.from_user, message.chat):
            return
        data = await state.get_data()
        cid = data.get('char_id')
        ch = active_char(cid, message.from_user.id) if isinstance(cid, int) else None
        if ch is None:
            await state.clear()
            await message.answer('Персонаж больше не найден в активной истории.')
            return
        if len(db.photos(cid)) >= 5:
            await state.clear()
            await message.answer('Максимум пять фото на персонажа.')
            return
        if message.photo:
            media = message.photo[-1]
        elif message.document and message.document.mime_type in ('image/jpeg', 'image/png', 'image/webp'):
            media = message.document
        else:
            await message.answer('Отправь изображение JPG, PNG или WEBP, не другой документ.')
            return
        if media.file_size and media.file_size > MAX_IMAGE_BYTES:
            await message.answer('Файл больше 12 МБ. Пришли изображение поменьше.')
            return
        await message.answer('🔎 Получил фото. Сохраняю и анализирую внешность...')
        try:
            stream = BytesIO()
            await message.bot.download(media, destination=stream)
            raw = stream.getvalue()
            suffix = image_info(raw)
            final_path = media_dir / (uuid4().hex + suffix)
            final_path.write_bytes(raw)
            try:
                pid = db.add_photo(cid, media.file_id, str(final_path), status='pending')
            except Exception:
                final_path.unlink(missing_ok=True)
                raise
        except ValueError as exc:
            await message.answer(str(exc))
            return
        except Exception:
            log.exception('Photo download/save failed')
            await message.answer('Не удалось сохранить фото. Проверь хранилище Bothost и попробуй снова.')
            return
        await state.clear()
        await analyze_photo(pid, message, message.from_user.id)

    @router.message(Editing.photo)
    async def photo_instructions(message: Message):
        if authorized_id(message.from_user, message.chat):
            await message.answer('Жду именно фото или изображение-документ. Чтобы выйти, нажми «🗂 Меню».')

    @router.message(F.photo | F.document)
    async def photo_outside_flow(message: Message):
        if authorized_id(message.from_user, message.chat):
            await message.answer('Чтобы добавить внешность, открой 👤 Персонажи → выбери героя → ➕ Фото.\n'
                                 'Так я не перепутаю, кому принадлежит референс.', reply_markup=main_menu())

    return router
