"""A real conversational companion for setup, not an auto-acknowledgement.

User-supplied facts live in the on-disk quickstart draft until explicit start;
AI responses are conversational and never silently become authorial canon.
"""
from __future__ import annotations

import re

DIALOGUE_SYSTEM = '''Ты Чернильник — живой, внимательный ИИ-соавтор, с которым автор ОБСУЖДАЕТ будущую текстовую ролевую, прежде чем начать игру.
Общайся по-русски естественно и содержательно, примерно как хороший собеседник: замечай конкретные идеи, их последствия и отношения героев, если полезно — предлагай интересные возможности. Не пересказывай каждое предложение автора и не изображай офисного секретаря.

ВАЖНО: сейчас режим ПОДГОТОВКИ, не литературный пост. Автор вправе присылать новые описания, исправления, фотографии и обсуждать сюжет в любом порядке. Ты не начинаешь художественную сцену и не управляешь персонажами, пока автор явно не скажет «погнали»; запуск выполняет отдельный механизм.

Всё, что автор прислал, уже сохраняется во ВРЕМЕННОМ ЧЕРНОВИКЕ. Фактические карточки персонажей, отношения и новая история создаются только при запуске. Если автор спрашивает «ты записала?», различай эти два состояния честно. Не говори «внесла в анкету / энциклопедию / канон», если это ещё черновик.

Помни разделение ролей: «твой персонаж» = твой (бота), «мой персонаж» = персонаж автора; нельзя присваивать герою автора мысли, решения, скрытые мотивы или развязки. Предположения и предложения обозначай именно как идеи, а не утверждённый канон. Если автор исправляет детали, последнее исправление имеет приоритет. Одежда, поза, макияж и аксессуары на изображении не считаются постоянной внешностью.

Отвечай на ПОСЛЕДНЕЕ сообщение автора и весь смысл диалога, а не одним шаблоном «слушаю, продолжай». Не нужно каждый раз напоминать «погнали» или спрашивать, готовы ли начинать. Спрашивай ОДИН конкретный вопрос, только если без него действительно сложно понять задумку; если всё понятно, просто реагируй по существу. Можно пошутить естественно, если уместно, но не превращай каждую реплику в шутку. Обычно 2–5 предложений, но если автор задаёт большой вопрос, ответь полноценно.

Если мир ещё не выбран, сначала учитывай, какие миры реально имеются, и мягко уточняй при необходимости; не навязывай список кнопок. Не считай секреты из справочника известными персонажам. Не переопределяй законы мира собственными догадками.'''

# Requests like "что ты поняла" should not be copied into the canonical story seed.
STATUS_OR_DISCUSSION = re.compile(
    r'^(?:ну\s+)?(?:ты\s+)?(?:что\s+(?:ты\s+)?(?:понял[аи]?|запомнил[аи]?|записал[аи]?)|'
    r'что\s+у\s+нас\s+есть|что\s+сейчас\s+известно|'
    r'(?:все|всё)\s+ли\s+(?:ты\s+)?(?:понял[аи]?|запомнил[аи]?)|'
    r'как\s+тебе\s+\S+|что\s+думаешь\s+про\b|'
    r'(?:а\s+)?(?:как\s+)?(?:тебе\s+)?(?:нравится|кажется|думаешь)\b|'
    r'(?:можешь\s+)?(?:перескажи|подытожь|суммируй|напомни)\b)',
    re.I,
)


def is_discussion_only(text: str) -> bool:
    """Conservative: never accidentally drop new lore just because it's a question."""
    text = text.strip()
    if '\n' in text or len(text) > 260:
        return False
    return bool(STATUS_OR_DISCUSSION.search(text))


def setup_world_context(db, world_id: int | None, notes: list[str]) -> str:
    worlds = db.list_worlds()
    if world_id is None:
        names = ', '.join(w['name'] for w in worlds[:15])
        return 'Общий мир ещё не выбран. Доступные миры: ' + (names or 'пока нет') + '. Можно играть без общего мира.'
    if not world_id:
        return 'Решение автора: свободная история, без готового мира.'
    world = db.get_world(world_id)
    if not world:
        return 'Мир больше не найден. Не придумывай канон.'
    context = f'Выбран общий мир: {world["name"]}.\nОбщее описание:\n{world["description"][:3200]}'
    query = '\n'.join(notes[-7:])[-9000:]
    matches = db.relevant_world_lore(world_id, query, max_chars=3800, max_items=4)
    # Russian inflections (Эльдора / в Эльдоре) often defeat a raw token match.
    # Supplement title matches conservatively, never from other worlds.
    included = {m.split(']')[0][1:].casefold() for m in matches if m.startswith('[')}
    for article in db.list_world_lore(world_id):
        title = article['title']
        folded = title.casefold()
        stem = folded[:-1] if len(folded) >= 6 and folded[-1] in 'аяоеьй' else folded
        if folded not in included and len(stem) >= 5 and stem in query.casefold():
            matches.append('['+title+']\n'+article['body'][:1800])
            included.add(folded)
            if len(matches) >= 4:
                break
    for article in matches:
        context += '\n\nСтатья мира:\n' + article
    # A shared-world character card is reusable, but story-local experiences
    # and hidden secrets from other stories must not leak into a new setup.
    roster = []
    for character in db.world_characters(world_id):
        name = character['name']
        if name.casefold() not in query.casefold():
            continue
        info = ('Персонаж из библиотеки: ' + name
                + '\nОбщее описание: ' + (character['description'] or 'пока не заполнено')[:700]
                + '\nПостоянная внешность: ' + (character['appearance_override'] or 'не уточнена')[:300])
        global_fields = db.profile_fields(character['id'])
        if global_fields:
            info += '\nГлобальные поля анкеты: ' + '; '.join(
                f'{x["field"]}: {x["value"][:120]}' for x in global_fields[:8])
        roster.append(info)
        if len(roster) >= 4:
            break
    if roster:
        context += '\n\nОБЩИЕ карточки известных персонажей (НЕ опыт из других историй):\n' + '\n'.join(roster)
    return context[:11500]


def setup_dialogue_messages(db, draft: dict, latest: str, *, is_photo: bool = False) -> list[dict]:
    notes = list(draft.get('notes') or [])
    full_notes = '\n\n'.join(notes)
    earliest = full_notes[:6500] if len(full_notes) > 11500 else ''
    latest_notes = full_notes[-11500:]
    photos = []
    for p in draft.get('photos', [])[-6:]:
        item = f'Фото №{p["index"]}: подпись: {p.get("caption", "")}; возле описания: {p.get("near_text", "")[:330]}'
        if p.get('analysis'):
            item += f'; наблюдаемые ПОСТОЯННЫЕ особенности: {p["analysis"][:850]}'
        elif p.get('analysis_error'):
            item += '; изображение сохранено, но пока не разобрано визуально'
        photos.append(item)
    conversation = '\n'.join('ЧЕРНИЛЬНИК: '+r[:750] for r in draft.get('assistant_replies', [])[-4:])
    user = (
        setup_world_context(db, draft.get('world_id'), notes)
        + '\n\nИСХОДНЫЕ ПОЛЬЗОВАТЕЛЬСКИЕ ОПИСАНИЯ (это черновик, исправления позднее важнее):\n'
        + (earliest + '\n\nПОСЛЕДНИЕ ПОДРОБНОСТИ:\n' if earliest else '') + latest_notes
        + '\n\nРЕФЕРЕНСЫ:\n' + ('\n'.join(photos) if photos else '(пока нет)')
        + '\n\nТВОИ НЕДАВНИЕ ОТВЕТЫ (не трактуй как канон):\n' + conversation
        + '\n\nПОСЛЕДНЕЕ СООБЩЕНИЕ АВТОРА, НА НЕГО НУЖНО ОТВЕТИТЬ:\n'
        + (f'[Прислано изображение персонажа]\n{latest}' if is_photo else latest)
    )
    return [{'role':'system','content':DIALOGUE_SYSTEM}, {'role':'user','content':user}]


def save_assistant_reply(drafts, uid: int, reply: str):
    draft = drafts.get(uid)
    if not draft:
        return
    draft.setdefault('assistant_replies', []).append(reply[:1400])
    draft['assistant_replies'] = draft['assistant_replies'][-6:]
    drafts.save(uid, draft)


async def generate_setup_reply(engine, db, draft: dict, latest: str, *, is_photo: bool = False) -> str:
    """Small natural-language call. No JSON contract or new writes to story DB."""
    choice = engine.generation_choice()
    client = engine.polza_client if choice.provider == 'polza' else engine.client
    if client is None:
        raise RuntimeError('Выбранная модель недоступна: проверь ключ провайдера.')
    messages = setup_dialogue_messages(db, draft, latest, is_photo=is_photo)
    if choice.provider == 'polza':
        response = await client.chat.completions.create(
            model=choice.model, messages=messages, max_tokens=420)
        reply = response.choices[0].message.content if response.choices else None
    else:
        response = await client.responses.create(
            model=choice.model, instructions=messages[0]['content'],
            input=messages[1]['content'], max_output_tokens=420, store=False)
        reply = response.output_text
    if not isinstance(reply, str) or not reply.strip():
        raise RuntimeError('Модель не ответила на сообщение в подготовке.')
    return reply.strip()[:3400]
