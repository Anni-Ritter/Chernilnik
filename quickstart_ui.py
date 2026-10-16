"""Natural-language story onboarding, keeping the full admin UI optional.

Telegram is a conversation first; SQLite story creation happens only after a
validated setup and an explicit 'погнали'. Drafts/images survive redeploys.
"""
from __future__ import annotations

import asyncio
import os
import logging
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from quickstart_core import (QuickDrafts, EXTRACT_SYSTEM, setup_prompt,
                             decode_json, validate_plan, need_clarification,
                             find_world, wants_start, wants_new_story, install_plan)
from typing_status import typing_during
from vision import image_info, MAX_IMAGE_BYTES, describe_image

log = logging.getLogger(__name__)


def choices(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=code) for text, code in row]
        for row in rows
    ])


def world_picker(db):
    rows = [[('🌿 Без общего мира', 'qs:world:0')]]
    for world in db.list_worlds()[:30]:
        rows.append([(f'🌍 {world["name"][:48]}', f'qs:world:{world["id"]}')])
    rows.append([('⚙️ Полное меню миров', 'w:home')])
    return choices(rows)


async def register_quickstart(db, engine, owner: int, begin_story):
    router = Router(name='conversational_quickstart')
    root = Path(db.path).parent / 'quickstarts'
    drafts = QuickDrafts(root)
    photo_dir = root / 'photos'
    photo_dir.mkdir(parents=True, exist_ok=True)
    finish_lock = asyncio.Lock()

    def allowed(user, chat):
        return bool(user and user.id == owner and chat and chat.type == 'private')

    async def welcome(message: Message, uid: int, *, title: str = '', seed: str = ''):
        old = drafts.get(uid)
        if old and old.get('session_id'):
            await message.answer('Уже есть подготовленная история. Отправь «погнали» ещё раз, '
                                 'чтобы начать её без повторного создания. Или /cancelsetup.')
            return
        wid = find_world(seed, db.list_worlds()) if seed else None
        draft = drafts.start(uid, wid, title=title, seed=seed)
        if draft['world_id'] is None:
            await message.answer('Затеваем новую историю! В каком мире играем?\n'
                                 'Выбери мир ниже или напиши его название. Можно и без общего мира.',
                                 reply_markup=world_picker(db))
        else:
            await message.answer('Мир поняла. Рассказывай идею, кто твои герои, кто мои. '
                                 'Можно несколькими сообщениями и картинками.\n'
                                 'Когда всё расскажешь — просто напиши «погнали».')

    async def select_world(message: Message, uid: int, wid: int):
        if wid > 0 and not db.get_world(wid):
            await message.answer('Этот мир не найден. Выбери другой.', reply_markup=world_picker(db))
            return
        draft = drafts.get(uid)
        if draft is not None and draft.get('session_id'):
            if db.get_opening(draft['session_id']):
                drafts.clear(uid)
                draft = None
            else:
                await message.answer('Сначала закончим уже созданную историю: напиши «погнали». '
                                     'Если хочешь отказаться — /cancelsetup.')
                return
        if draft is None:
            draft = drafts.start(uid, wid)
        draft['world_id'] = wid
        draft['phase'] = 'collect'
        drafts.save(uid, draft)
        world = db.get_world(wid) if wid else None
        await message.answer(f'Отлично, {world["name"] if world else "играем без общего мира"}.\n\n'
                             'Рассказывай свободно: «Авинель — твоя, она такая-то. '
                             'Кирай — моя. Хочу начать с...». '
                             'Портреты можно присылать следом или с подписью.\n\n'
                             'Я всё соберу. Когда будешь готова — «погнали».')

    async def extract(draft):
        # Setup uses the same selected literary provider, separate from OpenAI memory.
        choice = engine.generation_choice()
        client = engine.polza_client if choice.provider == 'polza' else engine.client
        if client is None:
            raise RuntimeError('Не настроен ключ литературной модели.')
        # Small bounded JSON request, unlike the entire lore-bible importer.
        last_error = None
        for attempt in range(2):
            try:
                response = await client.chat.completions.create(
                    model=choice.model,
                    messages=[{'role':'system','content':EXTRACT_SYSTEM},
                              {'role':'user','content':setup_prompt(draft) +
                               ('\nПредыдущий ответ был невалидным. Ответь СТРОГО JSON.' if attempt else '')}],
                    max_tokens=2500,
                )
                raw = response.choices[0].message.content or ''
                return validate_plan(decode_json(raw), draft['photos'])
            except (ValueError, IndexError, AttributeError, TypeError) as error:
                last_error = error
        raise RuntimeError('Модель дважды вернула некорректный JSON.') from last_error

    async def finish(message: Message, uid: int):
        if finish_lock.locked():
            await message.answer('Секунду, я ещё собираю предыдущую историю.')
            return
        async with finish_lock:
            draft = drafts.get(uid)
            if not draft:
                await message.answer('Сейчас нет подготовки. Напиши «хочу новую историю» или /new.')
                return
            if draft['phase'] == 'world' or draft['world_id'] is None:
                await message.answer('Сначала уточни, в каком мире играем.', reply_markup=world_picker(db))
                return
            if not draft['notes'] and not draft.get('session_id'):
                await message.answer('Расскажи хоть немного об идее и персонажах — тогда смогу начать.')
                return
            if draft.get('session_id'):
                plan = draft.get('plan')
                if not plan:
                    await message.answer('У истории нет сохранённой схемы. '
                                         'Открой /menu и /sessions или /cancelsetup.')
                    return
            else:
                try:
                    async with typing_during(message.bot,message.chat.id):
                        plan = await extract(draft)
                except Exception:
                    log.exception('Quickstart setup extraction failed')
                    await message.answer('Не получилось разобрать описание. Ничего не потеряно. '
                                         'Попробуй ещё раз «погнали» или дополни идею.')
                    return
                question = need_clarification(plan,draft)
                if question:
                    draft['phase'] = 'clarify'
                    drafts.save(uid,draft)
                    await message.answer(question)
                    return
                # Before any SQLite writes persist the plan for crash recovery.
                draft['plan'] = plan
                drafts.save(uid,draft)
            try:
                sid = install_plan(db,drafts,uid,draft,plan)
            except Exception:
                log.exception('Quickstart story creation failed; recoverable draft is retained')
                await message.answer('Не удалось записать историю в базу. Черновик остался; '
                                     'проверь логи и повтори «погнали».')
                return
            # Visual analysis enriches only stable physical traits; appearance failures
            # do not prevent starting the story or erase the saved photo reference.
            for row in db.list_characters(sid):
                for photo in db.photos(row['id']):
                    if photo['analysis_status'] != 'pending':
                        continue
                    try:
                        raw = Path(photo['local_path']).read_bytes()
                        text, in_tok, out_tok = await describe_image(
                            engine.client, os.getenv('VISION_MODEL', 'gpt-6-luna'), raw, row['name'])
                        db.update_photo_analysis(photo['id'],text,'ok')
                        db.add_usage(sid,'vision',os.getenv('VISION_MODEL', 'gpt-6-luna'),in_tok,out_tok)
                    except Exception:
                        log.exception('Quickstart reference analysis failed for character %d',row['id'])
                        db.update_photo_analysis(photo['id'],'','failed')
            await message.answer('Героев и завязку записала. Начинаю первый пост!')
            await begin_story(message,uid)
            if db.get_opening(sid):
                drafts.clear(uid)

    @router.message(Command('setup'))
    async def cmd_setup(message: Message, state: FSMContext):
        if allowed(message.from_user,message.chat):
            await state.clear()
            await welcome(message,message.from_user.id)

    @router.message(Command('cancelsetup'))
    async def cmd_cancel(message: Message, state: FSMContext):
        if allowed(message.from_user,message.chat):
            await state.clear()
            draft = drafts.get(message.from_user.id)
            if draft and draft.get('session_id'):
                await message.answer('История уже создана. Она останется в базе; текущую подготовку закрываю.')
            else:
                await message.answer('Подготовку отменили. Старые истории и канон не затронуты.')
            drafts.clear(message.from_user.id)

    @router.callback_query(F.data.startswith('qs:'))
    async def callback(callback: CallbackQuery, state: FSMContext):
        if not allowed(callback.from_user, callback.message.chat if isinstance(callback.message,Message) else None):
            await callback.answer('Недоступно.',show_alert=True)
            return
        await callback.answer()
        await state.clear()
        if not isinstance(callback.message,Message):
            return
        data = (callback.data or '').split(':')
        if len(data) == 3 and data[1] == 'world' and data[2].isdigit():
            await select_world(callback.message,callback.from_user.id,int(data[2]))
        elif len(data) == 2 and data[1] == 'new':
            await welcome(callback.message,callback.from_user.id)

    def fresh_story(message: Message) -> bool:
        if not message.from_user or not message.text or message.text.startswith('/'):
            return False
        if drafts.get(message.from_user.id) or wants_new_story(message.text):
            return True
        session = db.current_session(message.from_user.id)
        # On first launch, the legacy auto-created empty "Свободная история" should
        # not require /begin, /cast or /world before the user can describe an idea.
        return (session is None or (session['title'] == 'Свободная история' and
                not db.get_opening(session['id']) and
                not db.recent_turns(session['id'],1) and
                not db.list_characters(session['id'])))

    @router.message(StateFilter(None), F.text, fresh_story)
    async def natural_message(message: Message):
        if not allowed(message.from_user,message.chat):
            return
        uid = message.from_user.id
        text = (message.text or '').strip()
        draft = drafts.get(uid)
        if draft is None:
            await welcome(message,uid,seed=text)
            return
        if draft.get('session_id') and not wants_start(text):
            await message.answer('История уже подготовлена. «Погнали» повторит запуск, '
                                 'а /cancelsetup закроет подготовку.')
            return
        if draft['phase'] == 'world':
            wid = find_world(text, db.list_worlds())
            if text.casefold() in ('без мира','свободная история','свой мир','без общего мира'):
                wid = 0
            if wid is None:
                await message.answer('Не уверена, какой мир ты имеешь в виду. Выбери существующий '
                                     'или «Без общего мира».',reply_markup=world_picker(db))
            else:
                matched = db.get_world(wid) if wid else None
                if len(text) > len(matched['name'] if matched else 'без мира') + 15:
                    try:
                        drafts.add_note(uid,text)
                    except ValueError as err:
                        await message.answer(str(err))
                        return
                await select_world(message,uid,wid)
            return
        if wants_start(text):
            await finish(message,uid)
            return
        try:
            drafts.add_note(uid,text)
        except ValueError as err:
            await message.answer(str(err))
            return
        if draft['phase'] == 'clarify':
            await finish(message,uid)
        else:
            await message.answer('Слушаю, продолжай. Можно присылать картинки и уточнения. '
                                 'Когда всё — «погнали».')

    @router.message(StateFilter(None), F.photo | F.document)
    async def natural_photo(message: Message):
        if not allowed(message.from_user,message.chat):
            return
        uid = message.from_user.id
        draft = drafts.get(uid)
        if not draft or draft['phase'] not in ('collect','clarify'):
            await message.answer('Для референса персонажа начни подготовку через /new. '
                                 'В старой истории фото можно добавить через /menu → Персонажи.')
            return
        media = message.photo[-1] if message.photo else message.document
        if media.file_size and media.file_size > MAX_IMAGE_BYTES:
            await message.answer('Картинка больше 12 МБ. Пришли поменьше.')
            return
        if message.document and message.document.mime_type not in ('image/jpeg','image/png','image/webp'):
            await message.answer('Нужно изображение JPG, PNG или WEBP.')
            return
        path = None
        try:
            stream = BytesIO()
            await message.bot.download(media,destination=stream)
            raw = stream.getvalue()
            suffix = image_info(raw)
            path = photo_dir / (uuid4().hex + suffix)
            path.write_bytes(raw)
            draft = drafts.add_photo(uid,media.file_id,str(path),message.caption or '')
        except (ValueError,OSError) as exc:
            if path: path.unlink(missing_ok=True)
            await message.answer('Не смогла сохранить изображение: '+str(exc))
            return
        n=len(draft['photos'])
        await message.answer(f'Референс №{n} сохранила. Если неочевидно, чей он, просто '
                             'скажи имя персонажа. Одежда с картинки не считается постоянной.')

    return router, drafts, welcome, world_picker
