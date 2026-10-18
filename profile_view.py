"""Pure, UI-safe views of the evolving character questionnaire.

No Telegram dependency. Values are read-only until the author explicitly selects
Edit; sections show excerpts and the field page shows the complete saved value.
"""
from __future__ import annotations

from worlds import PROFILE_FIELDS, PROFILE_GROUPS


def selected_profile_fields(rows):
    """Prefer author-maintained global facts over story-local model observations."""
    fields = {}
    for row in rows:
        key = row['field']
        current = fields.get(key)
        if current is None:
            fields[key] = row
        elif current['source'] == 'author':
            continue
        elif row['source'] == 'author' or row['session_id'] != 0:
            fields[key] = row
    return fields


def group_for_field(key: str) -> str | None:
    for group, (_, keys) in PROFILE_GROUPS.items():
        if key in keys:
            return group
    return None


def field_marker(record):
    if not record:
        return '○'
    if record['certainty'] != 'confirmed':
        return '≈'
    return '✅' if record['source'] == 'author' else '✦'


def field_origin(record):
    if record is None:
        return 'Не заполнено'
    source = record['source']
    origin = {
        'author': 'Заполнено автором',
        'story': 'Извлечено из постов автора',
        'model': 'Извлечено из сюжета моделью',
    }.get(source, 'Из библиотеки / импорта')
    if record['certainty'] != 'confirmed':
        origin += ' · предположение, не подтверждённый факт'
    if record['session_id']:
        origin += ' · эта история'
    else:
        origin += ' · общая анкета'
    return origin


def _excerpt(value: str, limit: int) -> str:
    text = ' '.join(value.split())
    if len(text) <= limit:
        return text
    cutoff = text.rfind(' ', 0, limit - 1)
    if cutoff < limit // 2:
        cutoff = limit - 1
    return text[:cutoff].rstrip() + '…'


def group_text(char_name: str, group_code: str, records: dict) -> str:
    title, keys = PROFILE_GROUPS[group_code]
    count = sum(bool(records.get(k)) for k in keys)
    parts = [f'{title} · {char_name}', f'Заполнено: {count}/{len(keys)}', '']
    for key in keys:
        record = records.get(key)
        parts.append(f'{field_marker(record)} {PROFILE_FIELDS[key]}')
        parts.append(_excerpt(record['value'], 300) if record else 'Пока не указано')
        parts.append('')
    parts.append('✅ вручную  ·  ✦ из сюжета  ·  ≈ предположение  ·  ○ пусто')
    parts.append('Нажми на поле, чтобы прочитать полностью или изменить.')
    # Six fields * 300 + metadata fits into Telegram's 4096-character limit.
    return '\n'.join(parts)


def detail_text(char_name: str, field: str, record) -> str:
    title = PROFILE_FIELDS[field]
    if record:
        parts = [f'📋 {char_name} · {title}', '', record['value'], '', field_origin(record)]
        evidence = (record['evidence'] or '').strip()
        if evidence:
            parts += ['', 'Основание: ' + _excerpt(evidence, 320)]
    else:
        parts = [f'📋 {char_name} · {title}', '', 'Пока не заполнено.']
    return '\n'.join(parts)


def edit_text(char_name: str, field: str, record) -> str:
    return ('✏️ ' + char_name + ' · ' + PROFILE_FIELDS[field] + '\n\n'
            + ('Текущее значение:\n' + record['value'] + '\n\n' if record else 'Поле пока пустое.\n\n')
            + 'Пришли новое значение (до 1500 символов).\n'
              '«-» — очистить поле; «отмена» — выйти без изменений.\n'
              'Ни одно значение не меняется, пока ты не отправишь текст.')
