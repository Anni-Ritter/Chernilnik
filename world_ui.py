"""Telegram navigation for shared worlds and evolving character profiles.

Menus use short callback_data and FSM forms, and never treat form input as RP.
"""
from __future__ import annotations

import logging
import sqlite3
from io import BytesIO

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, BufferedInputFile
from worlds import PROFILE_FIELDS, PROFILE_GROUPS

log = logging.getLogger(__name__)


class Form(StatesGroup):
    world_name = State()
    world_description = State()
    story_title = State()
    world_event = State()
    lore_new = State()
    lore_edit = State()
    lore_append = State()
    profile_field = State()


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=code) for label,code in row]
        for row in rows
    ])


async def register_world_ui(db, owner: int):
    router = Router(name='worlds_ui')

    def allowed(user, chat):
        return bool(user and user.id == owner and chat and chat.type == 'private')

    def active(uid):
        return db.ensure_current_session(uid)

    async def form_text(message: Message) -> str:
        """Accept a Telegram message or UTF-8 txt/md attachment in form mode."""
        if message.text is not None:
            return message.text.strip()
        document = message.document
        if document is None or not (document.file_name or '').lower().endswith(('.txt', '.md')):
            raise ValueError('Пришли текстом или файлом .txt/.md (UTF-8).')
        if document.file_size is not None and document.file_size > 90000:
            raise ValueError('Файл слишком большой (до 90 КБ).')
        buf = BytesIO()
        await message.bot.download(document, destination=buf)
        raw = buf.getvalue()
        if len(raw) > 90000:
            raise ValueError('Файл слишком большой (до 90 КБ).')
        try:
            return raw.decode('utf-8-sig').strip()
        except UnicodeError:
            raise ValueError('Файл должен быть в кодировке UTF-8.')

    def can_see_char(cid: int, uid: int):
        char = db.get_character(cid)
        story = active(uid)
        return char if char and (db.story_has_character(story['id'], cid) or
                                (char['world_id'] is not None and db.get_world(char['world_id']))) else None

    async def home(msg, uid, page=0):
        world_rows = db.list_worlds()
        page = min(max(0,page), max(0,(len(world_rows)-1)//8))
        rows = [[('➕ Создать мир', 'w:new')], [('🌿 Без мира · свободные истории', 'w:storynew:0')]]
        for w in world_rows[page*8:page*8+8]:
            rows.append([(f'🌍 {w["name"][:40]} · 📖 {w["story_count"]} · 👤 {w["character_count"]}',
                          f'w:world:{w["id"]}')])
        if len(world_rows)>8:
            nav=[]
            if page: nav.append(('← Предыдущие',f'w:homepage:{page-1}'))
            if (page+1)*8<len(world_rows): nav.append(('Следующие →',f'w:homepage:{page+1}'))
            rows.append(nav)
        rows.append([('📖 Истории', 'ui:sessions'), ('← Меню', 'ui:home')])
        session = active(uid)
        bound = db.get_world(session['world_id']) if session['world_id'] else None
        await msg.answer('🌍 БИБЛИОТЕКА МИРОВ\n\n'
                         f'Сейчас: {bound["name"] if bound else "свободная история"} · {session["title"]}\n'
                         'Общий канон задаётся один раз. Сюжеты сохраняют собственную память.\n'
                         'Нажми на мир, чтобы открыть его страны, события и персонажей.', reply_markup=kb(rows))

    async def world_menu(msg, wid, uid):
        world = db.get_world(wid)
        if not world:
            await msg.answer('Мир не найден.')
            return
        desc = (world['description'] or 'Пока нет описания.')[:1700]
        rows = [[('📜 Заменить канон', f'w:canon:{wid}'), ('➕ Дополнить', f'w:canonadd:{wid}')],
                [('📚 Энциклопедия: страны и места', f'w:lore:{wid}')],
                [('📥 Выгрузить канон', f'w:exportcanon:{wid}')],
                [('🗞 События мира', f'w:events:{wid}'), ('👥 Жители', f'w:library:{wid}')],
                [('➕ Новая история здесь', f'w:storynew:{wid}')]]
        stories = db.world_stories(wid)
        if active(uid)['world_id'] is None:
            rows.append([('🔗 Привязать текущую историю к миру', f'w:bind:{wid}')])
        for s in stories[:12]:
            mark = '✅ ' if active(uid)['id'] == s['id'] else '📖 '
            rows.append([(mark + s['title'][:42], f'w:openstory:{s["id"]}')])
        rows.append([('← Ко всем мирам', 'w:home')])
        await msg.answer(f'🌍 {world["name"]}\nИсторий: {len(stories)} · Персонажей: {len(db.world_characters(wid))}\n\n'
                         f'📜 Канон:\n{desc}\n\n'
                         'Подробности стран, географии, магии и общества пиши обычным текстом в каноне мира. '
                         'Глобальные изменения истории — в хронике событий.', reply_markup=kb(rows))

    async def story_worlds(msg):
        rows = [[('🌿 Свободная история (без общего мира)', 'w:storynew:0')]]
        for w in db.list_worlds()[:35]:
            rows.append([(f'🌍 {w["name"][:48]}', f'w:storynew:{w["id"]}')])
        rows.append([('➕ Сначала создать мир', 'w:new'), ('← К историям', 'ui:sessions')])
        await msg.answer('📖 НОВАЯ ИСТОРИЯ\n\nВ каком мире она происходит?\n'
                         'Созданный мир можно использовать во многих историях.', reply_markup=kb(rows))

    async def lore_menu(msg, wid, page=0):
        world=db.get_world(wid)
        if not world:
            return await msg.answer('Мир не найден.')
        articles=db.list_world_lore(wid)
        page=min(max(0,page),max(0,(len(articles)-1)//10))
        rows=[[('➕ Добавить статью', f'w:loreadd:{wid}')]]
        for a in articles[page*10:page*10+10]:
            rows.append([( ('📌 ' if a['pinned'] else '📄 ') + a['title'][:49], f'w:loreitem:{a["id"]}')])
        if len(articles)>10:
            nav=[]
            if page: nav.append(('←',f'w:lorepage:{wid}:{page-1}'))
            if (page+1)*10<len(articles): nav.append(('→',f'w:lorepage:{wid}:{page+1}'))
            rows.append(nav)
        rows.append([('← К миру',f'w:world:{wid}')])
        await msg.answer(f'📚 ЭНЦИКЛОПЕДИЯ · {world["name"]}\n\n'
                         f'Статей: {len(articles)}. Сохраняй страны, города, расы, организации и законы '
                         'отдельными записями. Не нужно переписывать всё при создании новой истории.\n'
                         '📌 Закреплённые статьи всегда имеют приоритет при подборе контекста.',
                         reply_markup=kb(rows))

    async def lore_item(msg, aid):
        article=db.get_world_lore(aid)
        if not article:
            return await msg.answer('Статья не найдена.')
        buttons=[[('✏️ Изменить',f'w:loreedit:{aid}'),('➕ Дополнить',f'w:loreappend:{aid}')],
                 [('📌 Открепить' if article['pinned'] else '📌 Закрепить',f'w:lorepin:{aid}')],
                 [('🗑 Удалить статью',f'w:loredel:{aid}')],
                 [('← Энциклопедия',f'w:lore:{article["world_id"]}')]]
        body=article['body']
        if len(body)>2600:
            body=body[:2600]+'\n\n…полный текст сохранён в базе.'
        await msg.answer(f'📄 {article["title"]}\n\n{body}',reply_markup=kb(buttons))

    async def library(msg, wid, uid, page=0):
        w = db.get_world(wid)
        if not w:
            return await msg.answer('Мир не найден.')
        s = active(uid)
        same = s['world_id'] == wid
        chars = db.world_characters(wid)
        page=min(max(0,page),max(0,(len(chars)-1)//10))
        rows = []
        for ch in chars[page*10:page*10+10]:
            attached = same and db.story_has_character(s['id'], ch['id'])
            rows.append([(('✅ ' if attached else '👤 ') + ch['name'][:42], f'w:sheet:{ch["id"]}')])
        if len(chars)>10:
            nav=[]
            if page: nav.append(('←',f'w:libpage:{wid}:{page-1}'))
            if (page+1)*10<len(chars): nav.append(('→',f'w:libpage:{wid}:{page+1}'))
            rows.append(nav)
        if same:
            rows.append([('➕ Создать героя в текущей истории', 'ui:newchar')])
        else:
            rows.append([('➕ Создать историю в этом мире', f'w:storynew:{wid}')])
        rows.append([('← К миру', f'w:world:{wid}')])
        await msg.answer(f'👥 ПЕРСОНАЖИ · {w["name"]}\n\n'
                         f'Всего: {len(chars)}. ✅ — участвует в текущей истории.\n'
                         'Одна карточка на мир, но знания и психологические изменения — по отдельным сюжетам.\n'
                         'Открой карточку для анкеты или добавления в историю.', reply_markup=kb(rows))

    async def sheet(msg, cid, uid):
        ch = can_see_char(cid,uid)
        if not ch:
            return await msg.answer('Открой персонажа из списка своего мира или истории.')
        s = active(uid)
        attached = db.story_has_character(s['id'],cid)
        rows = db.profile_fields(cid, s['id'] if attached else None)
        fields = {}
        for r in rows:
            fields[r['field']] = r
        bits = [f'👤 {ch["name"]}',
                '🎭 ' + ('Твоя история · участник' if attached else 'Библиотека мира · не участвует'),
                f'Описание: {ch["description"][:400] or "не задано"}', '', '📋 АНКЕТА']
        for key,label in PROFILE_FIELDS.items():
            value = fields.get(key)
            if value:
                mark = ' ≈' if value['certainty'] != 'confirmed' else (' ✦' if value['source']=='model' else '')
                bits.append(f'• {label}: {value["value"][:220]}{mark}')
        if not rows:
            bits.append('Анкета пока пуста — это нормально.')
        buttons = [[('✏️ Заполнить анкету', f'w:fields:{cid}')]]
        if attached:
            buttons += [[('🧠 Изменения характера', f'w:history:{cid}')],
                        [('🖼 Фото и биография', f'ui:char:{cid}')]]
        if ch['world_id']:
            buttons.append([('📚 Развитие по историям', f'w:histories:{cid}')])
        if not attached:
            if s['world_id'] is not None and s['world_id'] == ch['world_id']:
                buttons.append([('➕ Добавить в текущую историю', f'w:attach:{cid}')])
        if ch['world_id']:
            buttons.append([('← Жители мира', f'w:library:{ch["world_id"]}')])
        else:
            buttons.append([('← Персонажи', 'ui:characters')])
        await msg.answer('\n'.join(bits)[:3650], reply_markup=kb(buttons))

    async def fields(msg,cid,uid, group=None):
        ch = can_see_char(cid, uid)
        if not ch:
            return await msg.answer('Персонаж не найден.')
        sheet_rows = db.profile_fields(cid, active(uid)['id'] if db.story_has_character(active(uid)['id'],cid) else None)
        present = {r['field']:r for r in sheet_rows}
        if group is None:
            rows=[]
            for code,(label,keys) in PROFILE_GROUPS.items():
                count=sum(k in present for k in keys)
                rows.append([(f'{label} · {count}/{len(keys)}',f'w:group:{cid}:{code}')])
            rows.append([('← К анкете', f'w:sheet:{cid}')])
            await msg.answer(f'📋 {ch["name"]} · анкета\n\n'
                             'Выбери раздел. Все поля необязательны. '
                             'Подтверждённые сведения автора имеют приоритет над выводами модели.',
                             reply_markup=kb(rows))
            return
        if group not in PROFILE_GROUPS:
            return await fields(msg,cid,uid)
        title,keys=PROFILE_GROUPS[group]
        rows=[]
        for key in keys:
            record=present.get(key)
            mark=('✅ ' if record and record['source']=='author' else '✦ ' if record else '○ ')
            rows.append([(mark+PROFILE_FIELDS[key],f'w:set:{cid}:{key}')])
        rows.append([('← Разделы анкеты',f'w:fields:{cid}')])
        await msg.answer(f'{title} · {ch["name"]}\n\n'
                         '✅ — заполнено тобой; ✦ — наблюдение бота; ○ — пусто.\n'
                         'Нажми на поле, чтобы заполнить или изменить его.',reply_markup=kb(rows))

    async def events(msg,wid):
        w = db.get_world(wid)
        if not w:
            return await msg.answer('Мир не найден.')
        items = db.list_world_events(wid, 20, include_candidates=True)
        rows = [[('➕ Добавить событие / закон', f'w:eventadd:{wid}')]]
        for e in items:
            rows.append([(('✓ ' if e['status']=='confirmed' else '? ') + e['text'][:43], f'w:event:{e["id"]}')])
        rows.append([('← К миру', f'w:world:{wid}')])
        await msg.answer(f'🗞 ХРОНИКА · {w["name"]}\n\n'
                         f'Записей: {len(items)} (показаны последние 20).\n'
                         'Сюжетные события автоматически пополняют хронику, когда подтверждены текстом. '
                         'Спорные сведения остаются предположениями, а не законами мира.\n'
                         'Поскольку линии могут идти в разное время, хронологию уточняй в настройках сюжета.',
                         reply_markup=kb(rows))

    async def history(msg,cid,uid,sid=None):
        sid = sid or active(uid)['id']
        if not can_see_char(cid,uid) or not db.story_has_character(sid,cid):
            return await msg.answer('Героя нет в текущей истории.')
        hist = db.development(cid,sid,18)
        lines = [f'🧠 РАЗВИТИЕ · {db.get_character(cid)["name"]}\n'
                 f'История: {db.get_session(sid)["title"]}',
                 'Здесь сохраняются наблюдения по ходам, а не выдуманный диагноз.']
        for r in reversed(hist):
            lines.append(f'\n#{r["turn_id"] or "начало"} {"≈ предположение" if r["certainty"] == "interpretation" else "✓ факт"}\n{r["insight"]}')
        if not hist:
            lines.append('\nНаблюдений пока нет. Они появятся по мере сюжета, если включена автоматическая память.')
        await msg.answer('\n'.join(lines)[:3900], reply_markup=kb([[('← К анкете',f'w:sheet:{cid}')]]))

    @router.message(F.text == '📚 Миры')
    async def reply_worlds(message: Message, state: FSMContext):
        if allowed(message.from_user,message.chat):
            await state.clear()
            await home(message,message.from_user.id)

    @router.message(Command('worlds'))
    async def worlds_command(message: Message, state: FSMContext):
        if allowed(message.from_user,message.chat):
            await state.clear()
            await home(message,message.from_user.id)

    @router.callback_query(F.data.startswith('w:'))
    async def callback(callback: CallbackQuery, state: FSMContext):
        if not allowed(callback.from_user,callback.message.chat if isinstance(callback.message,Message) else None):
            return await callback.answer('Недоступно.',show_alert=True)
        await callback.answer()
        if not isinstance(callback.message, Message):
            return
        msg,uid = callback.message,callback.from_user.id
        p = (callback.data or '').split(':')
        action = p[1] if len(p)>1 else ''
        arg = int(p[2]) if len(p)>2 and p[2].isdigit() else 0
        await state.clear()
        if action == 'home':
            await home(msg,uid)
        elif action == 'homepage':
            await home(msg,uid,arg)
        elif action == 'lorepage' and len(p)>3 and p[3].isdigit():
            await lore_menu(msg,arg,int(p[3]))
        elif action == 'libpage' and len(p)>3 and p[3].isdigit():
            await library(msg,arg,uid,int(p[3]))
        elif action == 'profiles':
            s=active(uid)
            rows=[[('👤 '+c['name'][:48], f'w:sheet:{c["id"]}')] for c in db.list_characters(s['id'])[:45]]
            rows.append([('➕ Новый персонаж', 'ui:newchar'), ('← Меню','ui:home')])
            await msg.answer('📋 АНКЕТЫ ГЕРОЕВ\n\nКарточки участников активной истории. '+
                             'Любое поле можно оставить пустым.',reply_markup=kb(rows))
        elif action == 'new':
            await state.set_state(Form.world_name)
            await msg.answer('🌍 Как называется новый мир? Пришли название одним сообщением.\n'
                             'Отмена: «🗂 Меню».')
        elif action == 'world':
            await world_menu(msg,arg,uid)
        elif action == 'bind' and db.get_world(arg):
            s=active(uid)
            if s['world_id'] is not None:
                await msg.answer('История уже принадлежит другому миру. Для отдельной ветки создай новую историю.')
            else:
                try:
                    db.assign_story_world(s['id'],arg)
                except ValueError as exc:
                    await msg.answer(str(exc))
                else:
                    await msg.answer(f'✅ «{s["title"]}» теперь в мире «{db.get_world(arg)["name"]}». '
                                     'Её старые посты и персонажи сохранены.')
                    await world_menu(msg,arg,uid)
        elif action == 'exportcanon' and db.get_world(arg):
            w=db.get_world(arg)
            await msg.answer_document(BufferedInputFile(w['description'].encode('utf-8'),
                                                     filename=f'world_{arg}_canon.txt'))
        elif action == 'canonadd' and db.get_world(arg):
            await state.set_state(Form.world_description)
            await state.update_data(wid=arg, mode='append')
            await msg.answer('➕ Пришли фрагмент канона текстом или файлом .txt/.md. '
                             'Он добавится к описанию и ничего не сотрёт. Повтори действие для следующего фрагмента.')
        elif action == 'canon' and db.get_world(arg):
            await state.set_state(Form.world_description)
            await state.update_data(wid=arg, mode='replace')
            await msg.answer('📜 Пришли описание мира сообщением или файлом .txt/.md (до 22 000 символов).\n'
                             'Можно указать страны, магию, историю и политику. Старое описание заменится.')
        elif action == 'storyworld':
            await story_worlds(msg)
        elif action == 'storynew':
            if arg and not db.get_world(arg):
                return await msg.answer('Мир не найден.')
            await state.set_state(Form.story_title)
            await state.update_data(wid=arg)
            name = db.get_world(arg)['name'] if arg else 'свободная история'
            await msg.answer(f'📖 Новая история · {name}\n\nКак её назовём?')
        elif action == 'openstory':
            if db.switch_session(uid,arg):
                await msg.answer(f'✅ Сейчас история «{active(uid)["title"]}».',
                                 reply_markup=kb([[('👤 Персонажи', 'ui:characters'), ('✒️ Первый пост', 'ui:begin')],
                                                  [('← К миру', f'w:world:{active(uid)["world_id"]}') if active(uid)['world_id'] else ('← К историям','ui:sessions')]]))
            else:
                await msg.answer('История не найдена.')
        elif action == 'lore' and db.get_world(arg):
            await lore_menu(msg,arg)
        elif action == 'loreitem':
            await lore_item(msg,arg)
        elif action == 'loreadd' and db.get_world(arg):
            await state.set_state(Form.lore_new)
            await state.update_data(wid=arg)
            await msg.answer('📚 Новая статья: пришли одним сообщением название, затем с новой строки текст.\n'
                             'Например: «Эльдора\nГосударство ангелов...». Можно также прислать .txt/.md. '
                             'После сохранения статью можно дополнять по частям.')
        elif action in ('loreedit','loreappend') and db.get_world_lore(arg):
            await state.set_state(Form.lore_edit if action=='loreedit' else Form.lore_append)
            await state.update_data(lore_id=arg)
            await msg.answer('✏️ Пришли текст статьи целиком или .txt/.md.' if action=='loreedit'
                             else '➕ Пришли фрагмент текста или .txt/.md. Старый текст не затронется.')
        elif action == 'lorepin' and db.get_world_lore(arg):
            a=db.get_world_lore(arg)
            db.pin_world_lore(arg,not bool(a['pinned']))
            await lore_item(msg,arg)
        elif action == 'loredel' and db.get_world_lore(arg):
            a=db.get_world_lore(arg)
            await msg.answer(f'Удалить статью «{a["title"]}»? Это не изменит истории и карточки.',
                             reply_markup=kb([[('🗑 Да, удалить',f'w:loredelyes:{arg}')],
                                              [('← Отмена',f'w:loreitem:{arg}')]]))
        elif action == 'loredelyes' and db.get_world_lore(arg):
            wid=db.get_world_lore(arg)['world_id']
            db.delete_world_lore(arg)
            await lore_menu(msg,wid)
        elif action == 'library':
            await library(msg,arg,uid)
        elif action == 'sheet':
            await sheet(msg,arg,uid)
        elif action == 'fields':
            await fields(msg,arg,uid)
        elif action == 'group' and len(p)>3:
            await fields(msg,arg,uid,p[3])
        elif action == 'set' and len(p)>3 and p[3] in PROFILE_FIELDS:
            if not can_see_char(arg,uid):
                return await msg.answer('Персонаж не найден.')
            await state.set_state(Form.profile_field)
            await state.update_data(cid=arg,field=p[3],sid=active(uid)['id'])
            await msg.answer(f'✏️ {db.get_character(arg)["name"]} · {PROFILE_FIELDS[p[3]]}\n'
                             'Напиши значение (до 1500 символов) или отправь «-» чтобы очистить поле.\n'
                             'Ручные поля анкеты считаются авторскими фактами.')
        elif action == 'history':
            await history(msg,arg,uid)
        elif action == 'histories' and can_see_char(arg,uid):
            rows = [[('📖 '+s['title'][:45], f'w:hist:{arg}:{s["id"]}')]
                    for s in db.character_stories(arg)[:35]]
            rows.append([('← К анкете', f'w:sheet:{arg}')])
            await msg.answer('📚 В какой истории показать развитие персонажа?', reply_markup=kb(rows))
        elif action == 'hist' and len(p)>3 and p[3].isdigit() and can_see_char(arg,uid):
            await history(msg,arg,uid,int(p[3]))
        elif action == 'attach' and can_see_char(arg,uid):
            try:
                db.attach_character(active(uid)['id'],arg)
            except ValueError as exc:
                return await msg.answer(str(exc))
            await msg.answer('✅ Персонаж добавлен. Настроить, кто его играет, можно в карточке.',
                             reply_markup=kb([[('👤 Анкета',f'w:sheet:{arg}')], [('← Персонажи','ui:characters')]]))
        elif action == 'events':
            await events(msg,arg)
        elif action == 'eventadd' and db.get_world(arg):
            await state.set_state(Form.world_event)
            await state.update_data(wid=arg)
            await msg.answer('🗞 Запиши подтверждённый общий факт или глобальное событие (до 650 символов).\n'
                             'Это будет доступно всем историям данного мира.')
        elif action == 'event':
            e=db.get_world_event(arg)
            if e:
                await msg.answer(f'🗞 {e["text"]}\n\nСтатус: {e["status"]}\n'
                                 f'Из истории: {e["session_id"] or "вручную"}\n'
                                 f'Основание: {e["evidence"][:200] or "записано автором"}',
                                 reply_markup=kb([[('✅ Подтвердить',f'w:confirm:{arg}'),('🗄 Убрать',f'w:archive:{arg}')],
                                                  [('← К хронике',f'w:events:{e["world_id"]}')]]))
        elif action in ('archive','confirm'):
            e=db.get_world_event(arg)
            if e:
                db.set_world_event_status(arg,'archived' if action=='archive' else 'confirmed')
                await events(msg,e['world_id'])

    @router.message(Form.world_name, F.text)
    async def add_world(message: Message, state: FSMContext):
        if not allowed(message.from_user,message.chat):
            return
        try:
            wid=db.add_world(message.text or '')
        except (ValueError,sqlite3.IntegrityError) as exc:
            return await message.answer('Не удалось создать мир: возможно, такое название уже есть. Попробуй другое.')
        await state.clear()
        await message.answer('✅ Мир создан. Теперь можно один раз описать его устройство.',
                             reply_markup=kb([[('📜 Заполнить канон',f'w:canon:{wid}')],
                                              [('➕ Создать первую историю',f'w:storynew:{wid}')]]))

    @router.message(Form.world_description, F.text | F.document)
    async def save_description(message: Message, state: FSMContext):
        if not allowed(message.from_user,message.chat):
            return
        data=await state.get_data()
        try:
            value = await form_text(message)
            if not db.get_world(data.get('wid',0)) or not value or len(value)>22000:
                return await message.answer('Мир не найден либо текст пуст/длиннее 22 000 символов.')
            if data.get('mode')=='append':
                db.append_world_description(data['wid'],value)
            else:
                db.set_world_description(data['wid'],value)
        except ValueError as exc:
            return await message.answer(str(exc))
        await state.clear()
        await world_menu(message,data['wid'],message.from_user.id)

    @router.message(Form.story_title, F.text)
    async def save_story(message: Message, state: FSMContext):
        if not allowed(message.from_user,message.chat):
            return
        title=(message.text or '').strip()[:100]
        if not title:
            return await message.answer('Нужно непустое название.')
        data=await state.get_data()
        wid=data.get('wid') or None
        sid=db.new_session(message.from_user.id,title)
        if wid:
            db.assign_story_world(sid,wid)
        await state.clear()
        await message.answer('✅ История создана!\n'
                             'Выбери участников из библиотеки или создай новых. Затем нажми «Первый пост».',
                             reply_markup=kb([[('👥 Жители мира', f'w:library:{wid}') if wid else ('👤 Создать персонажа','ui:newchar')],
                                              [('🎭 Роли и сцена','ui:scene'),('✒️ Первый пост','ui:begin')],
                                              [('← К историям','ui:sessions')]]))

    @router.message(Form.world_event, F.text)
    async def save_event(message: Message, state: FSMContext):
        if not allowed(message.from_user,message.chat):
            return
        data=await state.get_data()
        val=(message.text or '').strip()
        if not 9<=len(val)<=650:
            return await message.answer('Событие должно быть от 9 до 650 символов.')
        db.add_world_event(data['wid'],val)
        await state.clear()
        await events(message,data['wid'])

    @router.message(Form.lore_new, F.text | F.document)
    async def save_lore_new(message: Message, state: FSMContext):
        if not allowed(message.from_user,message.chat):
            return
        data=await state.get_data()
        try:
            raw=await form_text(message)
        except ValueError as exc:
            return await message.answer(str(exc))
        head,sep,body=raw.partition('\n')
        if not sep or not body.strip():
            return await message.answer('Пришли название на первой строке и описание со второй.')
        try:
            aid=db.add_world_lore(data['wid'],head,body)
        except (ValueError,sqlite3.IntegrityError):
            return await message.answer('Не удалось сохранить: название уже используется или отсутствует текст.')
        await state.clear()
        await lore_item(message,aid)

    @router.message(Form.lore_edit, F.text | F.document)
    @router.message(Form.lore_append, F.text | F.document)
    async def save_lore_text(message: Message, state: FSMContext):
        if not allowed(message.from_user,message.chat):
            return
        data=await state.get_data()
        aid=data.get('lore_id')
        if not aid or not db.get_world_lore(aid):
            await state.clear()
            return await message.answer('Статья не найдена.')
        try:
            value=await form_text(message)
            if await state.get_state() == Form.lore_append.state:
                db.append_world_lore(aid,value)
            else:
                db.update_world_lore(aid,value)
        except ValueError as exc:
            return await message.answer(str(exc))
        await state.clear()
        await lore_item(message,aid)

    @router.message(Form.profile_field, F.text)
    async def save_field(message: Message, state: FSMContext):
        if not allowed(message.from_user,message.chat):
            return
        data=await state.get_data()
        cid=data.get('cid')
        if not cid or not can_see_char(cid,message.from_user.id):
            await state.clear()
            return await message.answer('Не удалось открыть карточку.')
        value=(message.text or '').strip()
        if len(value)>1500:
            return await message.answer('Максимум 1500 символов.')
        # Author-defined character identity/psychology is shared across stories.
        db.set_profile_field(cid,data['field'],'' if value=='-' else value,sid=0)
        await state.clear()
        await sheet(message,cid,message.from_user.id)

    return router
