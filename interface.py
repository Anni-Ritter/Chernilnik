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
    ReplyKeyboardRemove, BufferedInputFile,
)
from vision import MAX_IMAGE_BYTES, describe_image, image_info
from menu_navigation import InlineMenuMessage

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
    style_example = State()
    custom_polza_model = State()


# Never create a persistent reply keyboard, including in long-post mode.
# Telegram commands and one-off inline buttons live in chat messages instead.
MAIN_KEYS = ReplyKeyboardRemove()
DRAFT_KEYS = ReplyKeyboardRemove()


def draft_menu():
    return keyboard([
        [('📨 Отправить пост', 'ui:draftsend'), ('❌ Отменить', 'ui:draftcancel')],
        [('← Дополнительные настройки', 'ui:home')],
    ])


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
        for row in rows
    ])


def main_menu():
    """Small entry screen; all original tools remain one level deeper."""
    return keyboard([
        [('📖 Истории', 'ui:sessions'), ('🌍 Миры', 'w:home')],
        [('👤 Герои', 'ui:characters'), ('🧠 Память', 'ui:memory')],
        [('⚙️ Ещё настройки', 'ui:tools')],
    ])


def tools_menu():
    return keyboard([
        [('🎭 Сцена и роли', 'ui:scene'), ('🤖 Модель текста', 'ui:model')],
        [('🎨 Примеры стиля', 'ui:style'), ('📝 Длинный пост', 'ui:draft')],
        [('✒️ Первый пост', 'ui:begin'), ('📊 Статус', 'ui:status')],
        [('📦 Экспорт', 'ui:export'), ('💾 Резервная копия', 'ui:backup')],
        [('🔎 Промпт', 'ui:prompt'), ('📋 Анкеты', 'w:profiles')],
        [('← Меню', 'ui:home')],
    ])

def model_menu_text(engine):
    choice = engine.generation_choice()
    key_ok = engine.polza_client is not None
    return (f'🤖 Модель литературных постов\n\nСейчас: {choice.display()}\n'
            f'Polza API: {"ключ подключён" if key_ok else "нет POLZA_API_KEY"}\n\n'
            'Память и анализ фото по-прежнему работают через OpenAI. '
            'Переключение не меняет историю, SQLite или уже написанные посты. '
            'Модель меняется только для следующих генераций.\n'
            'Стоимость и ограничения зависят от Polza и выбранной модели.')


def model_menu(engine):
    choice = engine.generation_choice()
    presets = [
        ('gemini37', 'Polza · Gemini 3.7 Flash', 'polza', 'google/gemini-3.7-flash'),
        ('gemini38', 'Polza · Gemini 3.8 Flash', 'polza', 'google/gemini-3.8-flash'),
        ('openai', 'OpenAI · ' + engine.model, 'openai', engine.model),
    ]
    rows = []
    for code, label, provider, model in presets:
        selected = '✅ ' if (choice.provider, choice.model) == (provider, model) else ''
        rows.append([(selected + label, f'ui:modelpick:{code}')])
    rows.append([('✏️ Ввести другой ID модели Polza', 'ui:modelcustom')])
    rows.append([('← Меню', 'ui:home')])
    return keyboard(rows)


def scene_menu():
    return keyboard([
        [('📜 Уточнения сюжета', 'ui:field:world'), ('🎭 Распределение ролей', 'ui:field:cast')],
        [('🕰 Время', 'ui:field:scene_time'), ('📍 Локация', 'ui:field:current_location')],
        [('← Главное меню', 'ui:home')],
    ])


def character_menu(cid: int):
    return keyboard([
        [('🖼 Фотографии', f'ui:photos:{cid}'), ('➕ Фото', f'ui:upload:{cid}')],
        [('📋 Полная анкета', f'w:sheet:{cid}')],
        [('📝 Характер и биография', f'ui:editbio:{cid}')],
        [('✏️ Переименовать', f'ui:rename:{cid}')],
        [('✏️ Уточнить внешность', f'ui:editlook:{cid}')],
        [('🎭 Кто отыгрывает', f'ui:roles:{cid}')],
        [('🔗 Убрать из этой истории', f'ui:delchar:{cid}')],
        [('← Персонажи', 'ui:characters')],
    ])


async def register_interface(db, engine, owner: int, begin_story, play_story, sync_memory=None):
    """Build router with injected engine and story operations; no circular imports."""
    router = Router(name='roleplay_ui')
    media_dir = Path(os.getenv('MEDIA_DIR') or str(Path(db.path).parent / 'portraits'))
    media_dir.mkdir(parents=True, exist_ok=True)
    vision_model = os.getenv('VISION_MODEL', 'gpt-6-luna')

    def authorized_id(user, chat) -> bool:
        return bool(user and user.id == owner and chat and chat.type == 'private')

    def active(user_id):
        return db.ensure_current_session(user_id)

    def active_char(cid, user_id):
        char = db.get_character(cid)
        return char if char is not None and db.story_has_character(active(user_id)['id'], cid) else None

    async def show_menu(message: Message, user_id: int):
        session = active(user_id)
        await message.answer(f'🗂 Чернильник · «{session["title"]}»\n\n'
                             'Выбирай раздел кнопками. Обычные сообщения по-прежнему считаются ролевыми постами.',
                             reply_markup=main_menu())

    async def show_sessions(message: Message, user_id: int):
        active_id = active(user_id)['id']
        rows = [[('✨ Новая история с идеи', 'qs:new')]]
        for s in db.list_sessions()[:25]:
            title = s['title'][:33]
            rows.append([(('✅ ' if s['id'] == active_id else '📖 ') + title, f'ui:session:{s["id"]}')])
        rows.append([('← Меню', 'ui:home')])
        await message.answer('📖 Истории · выбери существующую или создай новую.', reply_markup=keyboard(rows))

    async def show_style(message: Message, user_id: int):
        examples = db.list_style_examples(user_id)
        rows = [[('➕ Добавить образец поста', 'ui:styleadd')]]
        for record in examples[:15]:
            label = '📝 #' + str(record['id']) + ' ' + record['text'][:26].replace('\n', ' ')
            rows.append([(label, f'ui:styleitem:{record["id"]}')])
        rows.append([('← Меню', 'ui:home')])
        await message.answer('🎨 Литературный стиль\n\n'
                             'Образцы действуют во ВСЕХ твоих историях, но не являются их каноном.\n'
                             f'Сохранено твоих примеров: {len(examples)}/15.\n'
                             'Чернильник подставит до двух подходящих примеров при генерации.\n'
                             'Их можно добавлять прямо сюда, без новых деплоев.',
                             reply_markup=keyboard(rows))

    async def show_characters(message: Message, user_id: int):
        sid = active(user_id)['id']
        rows = [[('➕ Новый персонаж', 'ui:newchar')]]
        for char in db.list_characters(sid)[:35]:
            who = {'user': '👤', 'bot': '✒️', 'npc': '🎭'}[char['role']]
            rows.append([(f'{who} {char["name"][:38]}', f'ui:char:{char["id"]}')])
        rows.append([('← Меню', 'ui:home')])
        await message.answer('👤 Персонажи текущей истории.\n'
                             '👤 — твой герой; ✒️ — герой бота; 🎭 — NPC.\n'
                             'Анкеты и внешность живут в библиотеке мира; сюжетные воспоминания разделены.',
                             reply_markup=keyboard(rows))

    async def show_character(message: Message, cid: int, user_id: int):
        ch = active_char(cid, user_id)
        if ch is None:
            await message.answer('Карточка не принадлежит этой истории.')
            return
        photos = db.photos(cid)
        active_pic = next((p for p in photos if p['is_primary']), None)
        story_char = next((c for c in db.list_characters(active(user_id)['id']) if c['id'] == cid), ch)
        role = {'user': 'Твой персонаж', 'bot': 'Персонаж соавтора', 'npc': 'NPC соавтора'}[story_char['role']]
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
        uid = callback.from_user.id
        parts = (callback.data or '').split(':')
        action = parts[1] if len(parts) > 1 else ''
        # Story text stays in the chat as its own message; only navigation edits.
        message = (callback.message if action in ('begin','draftsend')
                   else InlineMenuMessage(callback.message))
        arg = parts[2] if len(parts) > 2 else ''
        if action not in ('begin', 'draftsend'):
            await state.clear()
        if action == 'home':
            await show_menu(message, uid)
        elif action == 'tools':
            await message.answer('⚙️ Дополнительные возможности. Обычная игра не требует этих настроек.',
                                 reply_markup=tools_menu())
        elif action == 'model':
            await message.answer(model_menu_text(engine), reply_markup=model_menu(engine))
        elif action == 'modelpick':
            choices = {
                'gemini37': ('polza', 'google/gemini-3.7-flash'),
                'gemini38': ('polza', 'google/gemini-3.8-flash'),
                'openai': ('openai', engine.model),
            }
            if arg in choices:
                try:
                    choice = engine.select_generation_model(*choices[arg])
                except (ValueError, OSError) as exc:
                    await message.answer(str(exc), reply_markup=model_menu(engine))
                else:
                    await message.answer(f'✅ Выбрано: {choice.display()}. Следующий пост будет на этой модели.',
                                         reply_markup=model_menu(engine))
        elif action == 'modelcustom':
            if engine.polza_client is None:
                await message.answer('Сначала добавь POLZA_API_KEY в переменные BotHost и перезапусти бот.')
            else:
                await state.set_state(Editing.custom_polza_model)
                await message.answer('Пришли точный ID модели из каталога Polza, например: '
                                     'google/gemini-3.7-flash\n'
                                     'Для отмены используй /menu.')
        elif action == 'style':
            await show_style(message, uid)
        elif action == 'styleadd':
            if len(db.list_style_examples(uid)) >= 15:
                await message.answer('Уже 15 примеров. Удали ненужный, прежде чем добавить новый.')
            else:
                await state.set_state(Editing.style_example)
                await message.answer('🎨 Отправь одним сообщением удачный литературный пост '
                                     '(50–5500 символов). Это НЕ ролевой ход, он не изменит сюжет. '
                                     'Для отмены используй /menu.')
        elif action == 'styleitem' and arg.isdigit():
            item = next((x for x in db.list_style_examples(uid) if x['id'] == int(arg)), None)
            if item:
                await message.answer(f'🎨 Образец #{item["id"]}:\n\n{item["text"][:3000]}',
                                     reply_markup=keyboard([
                                         [('🗑 Удалить образец', f'ui:styledelask:{arg}')],
                                         [('← Стиль', 'ui:style')],
                                     ]))
        elif action == 'styledelask' and arg.isdigit():
            await message.answer('Удалить этот образец стиля?', reply_markup=keyboard([
                [('Да, удалить', f'ui:styledelyes:{arg}')],
                [('Нет', 'ui:style')],
            ]))
        elif action == 'styledelyes' and arg.isdigit():
            db.delete_style_example(uid, int(arg))
            await show_style(message, uid)
        elif action == 'sessions':
            await show_sessions(message, uid)
        elif action == 'new':
            from world_ui import kb
            rows = [[('🌿 Свободная', 'w:storynew:0')]]
            rows += [[('🌍 ' + w['name'][:40], f'w:storynew:{w["id"]}')] for w in db.list_worlds()[:30]]
            rows += [[('➕ Создать мир', 'w:new')]]
            await message.answer('В каком мире будет история?', reply_markup=kb(rows))
        elif action == 'session' and arg.isdigit():
            if db.switch_session(uid, int(arg)):
                from quickstart_core import QuickDrafts
                QuickDrafts(Path(db.path).parent/'quickstarts').clear(uid)
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
                await message.answer(f'Убрать «{ch["name"]}» из этой истории? '
                                     'В общем мире его анкета, биография и фотографии останутся.', reply_markup=keyboard([
                    [('🔗 Да, убрать', f'ui:delcharyes:{arg}')],
                    [('← Не удалять', f'ui:char:{arg}')],
                ]))
        elif action == 'delcharyes' and arg.isdigit():
            ch = active_char(int(arg), uid)
            if ch:
                paths = db.delete_character(active(uid)['id'], ch['id'])
                for item in paths:
                    p = Path(item)
                    if p.parent.resolve() == media_dir.resolve():
                        p.unlink(missing_ok=True)
                await message.answer('✅ Участие в истории убрано. Общие анкеты и фото не удалены, если герой принадлежит миру.')
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
                db.set_story_character_role(active(uid)['id'], ch['id'], parts[3])
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
                                         'Для отмены используй /menu.')
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
            shared_world = db.get_world(session['world_id']) if session['world_id'] else None
            await message.answer(f'🌍 Мир и сцена · {session["title"]}\n\n'
                                 f'Общий мир: {shared_world["name"] if shared_world else "не привязан"}\n'
                                 f'Уточнения сюжета: {(session["world"] or "нет")[:200]}\n'
                                 f'Роли: {(session["cast"] or "не заданы")[:220]}\n'
                                 f'Время: {session["scene_time"] or "не задано"}\n'
                                 f'Локация: {session["current_location"] or "не задана"}',
                                 reply_markup=scene_menu())
        elif action == 'field' and arg in ('world', 'cast', 'scene_time', 'current_location'):
            await state.set_state(Editing.field)
            await state.update_data(field=arg, session_id=active(uid)['id'])
            label = {'world': 'уточнения только этого сюжета', 'cast': 'распределение ролей',
                     'scene_time': 'время и сезон', 'current_location': 'место действия'}[arg]
            await message.answer(f'✏️ Пришли {label} одним сообщением. Старое значение будет заменено.\n'
                                 'Для отмены используй /menu.')
        elif action == 'memory':
            sid = active(uid)['id']
            facts = db.list_facts(sid, 15)
            fact_text = '\n'.join((('★ ' if f['pinned'] else '• ') + f['text'][:180]) for f in facts) or 'Пока пусто.'
            health=db.memory_health(sid)
            line=(f'\n\nИндексация: {health["done"]}/{health["total"]} ходов; '
                  f'ошибок: {health["failed"]}; не обработано: {health["unprocessed"]}.')
            await message.answer('🧠 Память истории:\n\n' + fact_text[:3000] + line, reply_markup=keyboard([
                [('🔄 Восстановить пропущенные ходы', 'ui:memorysync')],
                [('📌 Закрепить важный факт', 'ui:remember')],
                [('← Меню', 'ui:home')],
            ]))
        elif action == 'memorysync':
            if sync_memory is not None:
                await sync_memory(message, uid)
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
                                 'Когда закончишь — нажми «📨 Отправить пост» здесь или используй /play.',
                                 reply_markup=draft_menu())
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
        elif action == 'prompt':
            sid = active(uid)['id']
            trace = db.get_prompt_audit(sid)
            if trace is None:
                await message.answer('Промпт ещё не записан. Проведи один ход; PROMPT_DEBUG должен быть true.')
            else:
                await message.answer_document(
                    BufferedInputFile(trace['prompt_text'].encode('utf-8'),
                                      filename=f'prompt_story_{sid}.txt'),
                    caption='Последний реальный запрос к модели. Содержит секреты персонажей.')
        elif action == 'backup':
            try:
                path = db.make_backup()
                from aiogram.types import FSInputFile
                await message.answer_document(FSInputFile(str(path)),
                    caption='Резервная копия SQLite. Фото остаются отдельно в /app/data/portraits.')
            except Exception:
                log.exception('Manual backup failed')
                await message.answer('Не удалось сделать копию базы. Проверь логи и место на диске.')
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

    @router.message(Editing.custom_polza_model, F.text)
    async def receive_custom_model(message: Message, state: FSMContext):
        if not authorized_id(message.from_user, message.chat):
            return
        try:
            choice = engine.select_generation_model('polza', (message.text or '').strip())
        except (ValueError, OSError) as exc:
            await message.answer(str(exc) + '\nПришли правильный ID или открой /menu.')
            return
        await state.clear()
        await message.answer(f'✅ Выбрано: {choice.display()}.', reply_markup=model_menu(engine))

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
        except (sqlite3.IntegrityError, ValueError) as exc:
            await message.answer(str(exc) if isinstance(exc, ValueError) else
                                 'Персонаж с таким именем уже существует в этой истории. Дай другое имя.')
            return
        await state.clear()
        await message.answer('✅ Персонаж создан. Выбери, кто его отыгрывает.\n'
                             'Анкету и фото можно заполнить сейчас или позже.', reply_markup=keyboard([
            [('👤 Я', f'ui:setrole:{cid}:user'), ('✒️ Бот', f'ui:setrole:{cid}:bot')],
            [('🎭 NPC', f'ui:setrole:{cid}:npc')],
            [('📋 Открыть анкету', f'w:sheet:{cid}')],
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

    @router.message(Editing.style_example, F.text)
    async def receive_style_example(message: Message, state: FSMContext):
        if not authorized_id(message.from_user, message.chat):
            return
        try:
            db.add_style_example(message.from_user.id, message.text or '')
        except ValueError as exc:
            await message.answer('Не удалось сохранить пример: ' + str(exc))
            return
        await state.clear()
        await message.answer('✅ Литературный образец сохранён, не как ролевое событие.',
                             reply_markup=keyboard([[('🎨 Вернуться к стилю', 'ui:style')]]))

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
            await message.answer('Жду именно фото или изображение-документ. Чтобы выйти, открой /menu.')

    @router.message(F.photo | F.document)
    async def photo_outside_flow(message: Message):
        if authorized_id(message.from_user, message.chat):
            await message.answer('Чтобы добавить внешность, открой 👤 Персонажи → выбери героя → ➕ Фото.\n'
                                 'Так я не перепутаю, кому принадлежит референс.', reply_markup=main_menu())

    return router
