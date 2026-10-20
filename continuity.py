"""v0.8: evidence-checked, story-scoped CURRENT state; history is immutable.

This is intentionally not a world canon importer: it represents the last known
scene and the characters' *observable* conditions, not universal truths.
"""
from __future__ import annotations

import re

SCENE_ASPECTS = {'location', 'travel', 'destination', 'time', 'threat'}
CHARACTER_ASPECTS = {'location', 'pain', 'vision', 'injury', 'mark', 'condition',
                     'status', 'ability', 'equipment', 'goal', 'intent', 'allegiance'}
ASPECT_LABELS = {
    'location': 'Местонахождение', 'travel': 'Перемещение',
    'destination': 'Направление / назначение', 'time': 'Время',
    'threat': 'Непосредственная угроза', 'pain': 'Боль', 'vision': 'Зрение',
    'injury': 'Ранения', 'mark': 'Метка / проклятие', 'condition': 'Состояние',
    'status': 'Положение', 'ability': 'Способности',
    'equipment': 'Предметы / снаряжение', 'goal': 'Заявленная цель',
    'intent': 'Текущее намерение', 'allegiance': 'Принадлежность / верность',
}

SCENE_EXTRACTION_RULES = '''\n\nДОПОЛНИТЕЛЬНО: «scene_updates»: [] — до 6 доказанных ИЗМЕНЕНИЙ ТЕКУЩЕГО СОСТОЯНИЯ.
Элемент: {"scope":"scene|character", "name":"имя героя или пусто для scene",
"aspect":"...", "value":"краткое ТЕКУЩЕЕ состояние", "evidence":"ДОСЛОВНЫЙ фрагмент одного текущего поста"}.
scene: location (где сейчас), travel (движется ли/каким способом), destination (куда движется),
time (время суток / прошедшее время), threat (актуальная опасность, не догадки).
character: location, pain, vision, injury, mark, condition, status, ability, equipment,
goal, intent, allegiance. Имя персонажа ТОЛЬКО из списка активных героев.
Это СНИМОК ТЕКУЩЕГО, а не накопление прежних состояний. Например: после явного
исцеления глаза запиши vision="зрение снова обычное, без болезненных искажений"
и pain="боль исчезла"; старое «зрение двоится» НЕ остаётся действующим.
При прибытии в море запиши scene.location="на корабле в море", travel="морское путешествие";
прежний «порт отправления» — история, не текущее местонахождение.
Фиксируй только то, что реально изменилось или явно установлено в ЭТИХ ПОСТАХ.
Если изменений нет — []! Не заполняй из воображения. Не называй слух, угрозу,
желание, обещание и планы свершившимся событием. Намерение персонажа = goal/intent,
НО НЕ факт направления судна. Отдельно различай «персонаж хочет привести на суд»
и «персонаж действительно направляется к суду».
Речь персонажей (даже уверенная) НЕ доказывает объективные location, travel,
vision, pain, condition другого героя или политический факт; для этого нужно
прямое повествование либо наблюдаемое действие. Реплика о СОБСТВЕННОЙ цели
может фиксировать только его заявленное намерение (не объективный канон).
Герой СОАВТОРА не вправе задавать скрытые чувства и состояния героя ИГРОКА:
для его личных качеств требуй прямую авторскую констатацию в посте ИГРОКА.
При сомнении оставь [] или пропусти спорную запись. Не меняй прошлое, не
привноси сюжетных событий ради заполнения. Форма ответа всё ещё единый JSON
с ключами facts, world_changes, character_insights, scene_updates.''' 


def _is_dialogue(text: str, evidence: str) -> bool:
    """Conservative: don't turn speech into objective physical state."""
    # If evidence occurs inside an explicitly dialogue-marked line, reject it
    # for objective scene/conditions. Narration outside quotes is allowed.
    for line in text.splitlines():
        if evidence in line and re.match(r'^\s*[-—–«]', line):
            return True
    return False


def validated_scene_updates(payload: object, author_post: str, bot_post: str,
                            cast: list) -> list[dict]:
    """Accept only bounded, sourced current observations (not model's labels)."""
    if not isinstance(payload, dict) or not isinstance(payload.get('scene_updates'), list):
        return []
    allowed = {str(c['name']).casefold(): (c['name'], c['role']) for c in cast}
    result = {}
    for item in payload['scene_updates'][:12]:
        if not isinstance(item, dict):
            continue
        scope = str(item.get('scope', '')).strip()
        aspect = str(item.get('aspect', '')).strip().lower()
        name = str(item.get('name', '')).strip()
        value = ' '.join(str(item.get('value','')).split()).strip()
        evidence = str(item.get('evidence', '')).strip()
        if not (4 <= len(value) <= 240 and 7 <= len(evidence) <= 300):
            continue
        if scope == 'scene':
            if aspect not in SCENE_ASPECTS: continue
            name = ''
        elif scope == 'character':
            if aspect not in CHARACTER_ASPECTS or name.casefold() not in allowed: continue
            name, role = allowed[name.casefold()]
        else:
            continue
        # Spatial assertions must share a concrete geographic cue with the
        # quoted evidence. A random 'she decided' is not evidence for 'at sea'.
        if scope == 'scene' and aspect in ('location', 'travel', 'destination'):
            def stems(v):
                return {t[:3] for t in re.findall(r'[а-яёa-z]{4,}', v.casefold())
                        if t[:3] not in {'это','был','она','они','его','её','при','пос','как','буд','сво','пер','все','того','зач','тол','там','дол'}}
            if not (stems(value) & stems(evidence)):
                continue
        # The model cannot say 'source=user' and win: we derive actual provenance.
        if evidence in author_post:
            source, raw = 'author', author_post
        elif evidence in bot_post:
            source, raw = 'bot', bot_post
        else:
            continue
        # Bot cannot silently alter player-character physiology or goals.
        if scope == 'character' and role == 'user' and source != 'author':
            continue
        # We intentionally accept speech only for the actual bot character's
        # declared goal/intent; NOT as evidence of what is true outside its head.
        if _is_dialogue(raw, evidence):
            if not (scope == 'character' and source == 'bot' and role in ('bot','npc')
                    and aspect in ('goal', 'intent')):
                continue
        key = (scope, name.casefold(), aspect)
        # The last explicit statement in a single pair of posts wins only
        # within equal provenance. Author has priority over bot this turn.
        if key not in result or source == 'author':
            result[key] = {'scope':scope, 'name':name, 'aspect':aspect,
                           'value':value, 'source':source, 'evidence':evidence}
    return list(result.values())[:6]


class ContinuityMixin:
    def init_continuity(self):
        with self.connect() as con:
            con.executescript('''
              CREATE TABLE IF NOT EXISTS scene_state (
                session_id INTEGER NOT NULL REFERENCES sessions(id),
                scope TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
                aspect TEXT NOT NULL, value TEXT NOT NULL,
                source_turn_id INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '',
                edited_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (session_id,scope,name,aspect)
              );
              CREATE TABLE IF NOT EXISTS scene_state_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES sessions(id),
                scope TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
                aspect TEXT NOT NULL, old_value TEXT NOT NULL DEFAULT '',
                new_value TEXT NOT NULL, source_turn_id INTEGER NOT NULL,
                source TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
              );
              CREATE INDEX IF NOT EXISTS scene_state_history_story_idx
                ON scene_state_history(session_id,id);
              CREATE TABLE IF NOT EXISTS scene_jobs (
                session_id INTEGER NOT NULL REFERENCES sessions(id),
                turn_id INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL CHECK(status IN ('done','failed')),
                details TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (session_id,turn_id)
              );
            ''')

    def scene_state(self, sid: int):
        with self.connect() as con:
            return con.execute('''SELECT * FROM scene_state WHERE session_id=?
              ORDER BY CASE WHEN scope='scene' THEN 0 ELSE 1 END, name, aspect''',
              (sid,)).fetchall()

    def scene_snapshot(self, sid: int):
        rows = self.scene_state(sid)
        if not rows:
            return ''
        sections = ['СОСТОЯНИЕ СЦЕНЫ (АКТУАЛЬНО, заменяет устаревшие сведения из прошлых постов):']
        for row in rows:
            key = (row['name'] + ': ' if row['scope']=='character' else '') + ASPECT_LABELS.get(row['aspect'],row['aspect'])
            src = 'уточнение автора' if row['source']=='manual' else ('пост автора' if row['source']=='author' else 'пост соавтора')
            sections.append(f'• {key} — {row["value"]} ({src}, ход #{row["source_turn_id"]})')
        return '\n'.join(sections)

    def update_scene(self, sid: int, turn_id: int, items: list[dict]):
        """Upsert latest evidence only, protect manual correction and later turns."""
        saved = 0
        with self.connect() as con:
            for item in items[:12]:
                scope, name, aspect = item['scope'], item['name'], item['aspect']
                if scope == 'scene': name = ''
                old = con.execute('''SELECT * FROM scene_state WHERE session_id=? AND scope=?
                           AND name=? AND aspect=?''', (sid,scope,name,aspect)).fetchone()
                # A repaired old turn must not overwrite newer scene state.
                if old and (old['source']=='manual' or old['source_turn_id']>turn_id):
                    continue
                if old and old['source_turn_id']==turn_id and old['source']=='author' and item['source']=='bot':
                    continue
                if old and old['value']==item['value']:
                    continue
                con.execute('''INSERT INTO scene_state(session_id,scope,name,aspect,value,source_turn_id,source,evidence)
                          VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(session_id,scope,name,aspect) DO UPDATE SET
                          value=excluded.value,source_turn_id=excluded.source_turn_id,
                          source=excluded.source,evidence=excluded.evidence,edited_at=CURRENT_TIMESTAMP''',
                          (sid,scope,name,aspect,item['value'],turn_id,item['source'],item['evidence']))
                con.execute('''INSERT INTO scene_state_history(session_id,scope,name,aspect,old_value,new_value,source_turn_id,source,evidence)
                          VALUES(?,?,?,?,?,?,?,?,?)''',
                          (sid,scope,name,aspect,old['value'] if old else '',item['value'],turn_id,item['source'],item['evidence']))
                saved+=1
        return saved

    def set_scene_override(self, sid: int, scope: str, name: str, aspect: str, value: str):
        """Author correction is pinned until explicitly removed, never overwritten by model."""
        if scope == 'scene':
            if aspect not in SCENE_ASPECTS: raise ValueError('unknown scene attribute')
            name = ''
        elif scope == 'character':
            if aspect not in CHARACTER_ASPECTS: raise ValueError('unknown character attribute')
            if name.casefold() not in {x['name'].casefold() for x in self.list_characters(sid)}:
                raise ValueError('character is not active in this story')
            name = next(x['name'] for x in self.list_characters(sid) if x['name'].casefold()==name.casefold())
        else: raise ValueError('unknown scope')
        if not 3 <= len(value.strip()) <= 300: raise ValueError('invalid current state')
        with self.connect() as con:
            con.execute('''INSERT INTO scene_state(session_id,scope,name,aspect,value,source_turn_id,source,evidence)
                 VALUES(?,?,?,?,?,0,'manual','ручная поправка')
                 ON CONFLICT(session_id,scope,name,aspect) DO UPDATE SET
                 value=excluded.value,source=excluded.source,source_turn_id=0,
                 evidence=excluded.evidence,edited_at=CURRENT_TIMESTAMP''',
                 (sid,scope,name,aspect,value.strip()))

    def clear_scene_override(self, sid: int, scope: str, name: str, aspect: str):
        with self.connect() as con:
            con.execute('''DELETE FROM scene_state WHERE session_id=? AND scope=? AND name=?
                 AND aspect=? AND source='manual' ''', (sid,scope,name,aspect))

    def record_scene_job(self, sid: int, turn_id: int|None, *, success: bool, details: str=''):
        with self.connect() as con:
            con.execute('''INSERT INTO scene_jobs(session_id,turn_id,status,details) VALUES(?,?,?,?)
                 ON CONFLICT(session_id,turn_id) DO UPDATE SET status=excluded.status,
                 details=excluded.details,updated_at=CURRENT_TIMESTAMP''',
                 (sid, turn_id or 0, 'done' if success else 'failed', details[:180]))

    def scene_jobs(self, sid: int):
        with self.connect() as con:
            return con.execute('SELECT * FROM scene_jobs WHERE session_id=?',(sid,)).fetchall()
