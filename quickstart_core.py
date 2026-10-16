"""Durable conversational story setup. No ORM or Telegram dependency.

A draft is private to the bot owner and lives outside the story database. Nothing
becomes canon until its roles and world have been validated and committed.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path


START_WORDS = re.compile(r'^\s*(?:погнали|начинай|начн[её]м|старт|всё[,! ]*погнали|давай\s+(?:начн[её]м|играть)|можно\s+начинать)[.!\s]*$', re.I)
NEW_STORY = re.compile(r'^\s*(?:(?:хочу|давай|начн[её]м|создай|сделай)\s+(?:новую\s+|начать\s+)?(?:историю|ролевую|ролку)|новую\s+(?:историю|ролевую|ролку)|сыграем\s+(?:в\s+)?(?:историю|ролку))\b', re.I)


def wants_start(text: str) -> bool:
    return bool(START_WORDS.fullmatch(text.strip()))


def wants_new_story(text: str) -> bool:
    return bool(NEW_STORY.search(text[:500]))


def find_world(text: str, worlds: list) -> int | None:
    """Exact name surrounded by non-word boundaries; no fuzzy location guessing."""
    found = []
    for world in worlds:
        name = world['name']
        if re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', text, re.I):
            found.append(world['id'])
    return found[0] if len(found) == 1 else None


class QuickDrafts:
    def __init__(self, directory: str | Path):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, uid: int):
        return self.root / f'{int(uid)}.json'

    def get(self, uid: int) -> dict | None:
        path = self._path(uid)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            return data if data.get('version') == 1 else None
        except (OSError, ValueError, TypeError):
            return None

    def save(self, uid: int, data: dict):
        path = self._path(uid)
        data['version'] = 1
        tmp = path.with_suffix('.tmp')
        with tmp.open('w', encoding='utf-8') as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)

    def start(self, uid: int, wid: int | None, *, title: str = '', seed: str = '') -> dict:
        old = self.get(uid)
        # Do not lose a partially committed draft on an accidental second /new.
        if old and old.get('session_id'):
            return old
        data = {'version': 1, 'world_id': wid, 'phase': 'collect' if wid is not None else 'world',
                'title': title[:100], 'notes': ([seed[:9000]] if seed else []), 'photos': [],
                'session_id': None, 'needs_retry': False, 'assistant_replies': []}
        self.save(uid, data)
        return data

    def add_note(self, uid: int, text: str):
        data = self.get(uid)
        if data is None:
            raise ValueError('Нет подготовки истории.')
        text = text.strip()
        if text:
            if sum(len(s) for s in data['notes']) + len(text) > 28000:
                raise ValueError('Для этой подготовки накоплено слишком много текста (28 000 символов).')
            data['notes'].append(text)
        self.save(uid, data)
        return data

    def add_photo(self, uid: int, file_id: str, path: str, caption: str = ''):
        data = self.get(uid)
        if data is None:
            raise ValueError('Нет подготовки истории.')
        if len(data['photos']) >= 12:
            raise ValueError('В одной подготовке поддерживается до 12 изображений.')
        data['photos'].append({'index': len(data['photos'])+1, 'file_id': file_id,
                               'path': path, 'caption': caption[:800],
                               'near_text': '\n'.join(data['notes'][-2:])[-1200:], 'analysis': ''})
        self.save(uid, data)
        return data

    def set_photo_analysis(self, uid: int, index: int, analysis: str, *, failed: bool = False):
        data = self.get(uid)
        if data is None:
            return
        for item in data.get('photos', []):
            if item['index'] == index:
                item['analysis'] = analysis[:2500] if not failed else ''
                item['analysis_error'] = bool(failed)
                break
        self.save(uid, data)

    def clear(self, uid: int):
        self._path(uid).unlink(missing_ok=True)


EXTRACT_SYSTEM = '''Ты технический разборщик авторского описания ролевой. Верни ТОЛЬКО JSON объект.
Сохраняй только предоставленные автором факты, НЕ придумывай биографии, чувства и психологические объяснения.
Различай "мой/моя/я играю" = user и "твой/твоя/ты играешь/бот" = bot. Если не указано, role="unknown".
Сведения о месте и начале истории — это завязка, не установленные тобой канонические факты.
Если несколько версий персонажа имеют одинаковое имя, НЕ объединяй их автоматически.
Названия и описания персонажей не смешивай. Одежду с картинки НЕ делай постоянной внешностью.
Формат: {"title":"краткое название истории", "premise":"идея и исходная ситуация, без новых фактов",
"characters":[{"name":"", "role":"bot|user|npc|unknown", "description":"данные автора о характере, прошлом и целях",
"appearance":"постоянные особенности внешности, явно описанные автором"}],
"photos":[{"index":1,"character":"точное имя персонажа или пустая строка"}]}.
Привязка фото только когда имя прямо названо в подписи или соседних описаниях; иначе оставь пустую строку.
Нельзя добавлять персонажей, только упомянутых как фоновые имена, если автор не предлагает их отыгрывать.
Все строки по-русски; значения не должны быть длиннее 6000 символов.'''


def setup_prompt(data: dict) -> str:
    notes = '\n\n'.join(f'СООБЩЕНИЕ {i+1}:\n{item}' for i,item in enumerate(data['notes']))
    images = '\n'.join(f'ФОТО {p["index"]}: подпись: {p["caption"]!r}; ближайший текст: {p["near_text"]!r}; устойчивые визуальные признаки: {p.get("analysis", "")!r}'
                       for p in data['photos'])
    return ('Описание автора для НОВОЙ ролевой (последние исправления важнее ранних):\n'
            + notes + '\n\n' + images + '\n\nОтветь JSON, не прозой.')


def decode_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw, flags=re.I)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        pos, end = raw.find('{'), raw.rfind('}')
        if pos < 0 or end < pos:
            raise ValueError('Модель не вернула JSON.') from None
        result = json.loads(raw[pos:end+1])
    if not isinstance(result, dict):
        raise ValueError('Ожидался JSON-объект.')
    return result


def validate_plan(raw: dict, photos: list) -> dict:
    if not isinstance(raw.get('characters'), list) or len(raw['characters']) > 24:
        raise ValueError('Не удалось разобрать персонажей.')
    result = {'title': str(raw.get('title') or 'Новая история')[:100].strip(),
              'premise': str(raw.get('premise') or '')[:9000].strip(), 'characters': [], 'photos': []}
    names = set()
    for item in raw['characters']:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()[:80]
        if not name or name.casefold() in names:
            continue
        role = str(item.get('role') or 'unknown').lower()
        if role not in ('user','bot','npc','unknown'):
            role = 'unknown'
        names.add(name.casefold())
        result['characters'].append({'name': name, 'role': role,
                    'description': str(item.get('description') or '')[:6000].strip(),
                    'appearance': str(item.get('appearance') or '')[:2500].strip()})
    photo_by_index = {p['index']: p for p in photos}
    for item in (raw.get('photos') or []):
        if not isinstance(item, dict):
            continue
        try:
            n = int(item.get('index'))
        except (TypeError, ValueError):
            continue
        c = str(item.get('character') or '').strip()
        matching = next((i['name'] for i in result['characters'] if i['name'].casefold() == c.casefold()), None)
        if n in photo_by_index and matching and n not in [r['index'] for r in result['photos']]:
            result['photos'].append({'index': n, 'character': matching})
    return result


def need_clarification(plan: dict, draft: dict) -> str | None:
    if draft['world_id'] is None and draft.get('phase') == 'world':
        return 'В каком мире играем? Можешь назвать существующий или выбрать свободную историю.'
    if not plan['characters']:
        return 'Расскажи хотя бы о герое, которого мне предстоит отыгрывать.'
    ambiguous = [c['name'] for c in plan['characters'] if c['role']=='unknown']
    if ambiguous:
        return 'Кто играет за ' + ', '.join(ambiguous[:4]) + ' — ты или я?'
    if not any(c['role'] == 'bot' for c in plan['characters']):
        return 'Кого из персонажей ты отдаёшь мне для отыгрыша?'
    mapped = {p['index'] for p in plan['photos']}
    unassigned = [p['index'] for p in draft['photos'] if p['index'] not in mapped]
    if unassigned:
        return 'Чьи фотографии под номерами ' + ', '.join(map(str,unassigned[:6])) + '? Назови имена героев.'
    return None


def install_plan(db, drafts: QuickDrafts, uid: int, draft: dict, plan: dict) -> int:
    sid = draft.get('session_id')
    if not sid:
        title = draft['title'] or plan['title'] or 'Новая история'
        sid = db.new_session(uid, title[:100])
        draft['session_id'] = sid
        draft['phase'] = 'starting'
        drafts.save(uid, draft)
    else:
        db.switch_session(uid, sid)
    wid = draft.get('world_id') or 0
    if wid:
        db.assign_story_world(sid, wid)
    # Setup details are STORY LOCAL. Global canon from the selected world is inherited.
    db.set_session_field(sid, 'world', (
        'ЗАВЯЗКА, ЗАДАННАЯ АВТОРОМ:\n' + plan['premise'] +
        '\n\nСЛОВА АВТОРА (если резюме расходится, исходник приоритетнее):\n' +
        '\n'.join(draft['notes']))[:11000])
    db.set_session_field(sid, 'cast', '\n'.join(
        f'{x["name"]}: {"ИГРОК" if x["role"]=="user" else "СОАВТОР"} управляет. '
        + ((x['description'] + '. ') if x['description'] else '')
        + ((f'Устойчивая внешность: {x["appearance"]}') if x['appearance'] else '')
        for x in plan['characters']))
    attached = {}
    for c in plan['characters']:
        existing = next((x for x in db.world_characters(wid)
                         if x['name'].casefold() == c['name'].casefold()), None) if wid else None
        current = next((x for x in db.list_characters(sid)
                        if x['name'].casefold() == c['name'].casefold()), None)
        if current:
            cid = current['id']
        elif existing:
            cid = existing['id']
            db.attach_character(sid,cid,role=c['role'])
        else:
            cid = db.add_character(sid, c['name'], c['role'])
            if c['description']:
                db.set_character_text(cid,'description',c['description'])
            if c['appearance']:
                db.set_character_text(cid,'appearance_override',c['appearance'])
        db.set_story_character_role(sid,cid,c['role'])
        attached[c['name'].casefold()] = cid
    for pic in plan['photos']:
        cid = attached.get(pic['character'].casefold())
        data = next((p for p in draft['photos'] if p['index'] == pic['index']),None)
        if cid and data and len(db.photos(cid)) < 5:
            already = any(p['telegram_file_id'] == data['file_id'] for p in db.photos(cid))
            if not already:
                analysis = data.get('analysis') or ''
                db.add_photo(cid, data['file_id'], data['path'],
                             description=analysis, status='ok' if analysis else 'pending')
    return sid

