"""Shared worlds, story cast memberships and evidence-backed evolving sheets.

SQLite v0.6 extension. Existing story-local rows remain valid; never deletes them.
"""
from __future__ import annotations

import sqlite3

PROFILE_FIELDS = {
    'age': 'Возраст', 'gender': 'Пол / обращение', 'species': 'Раса / вид',
    'origin': 'Родина', 'residence': 'Где живёт', 'occupation': 'Занятие / положение',
    'family': 'Семья / прошлое', 'values': 'Ценности / убеждения',
    'goals': 'Цели и желания', 'fears': 'Страхи / уязвимости',
    'defenses': 'Защитные реакции', 'attachments': 'Привязанности / близость',
    'boundaries': 'Моральные границы', 'conflicts': 'Внутренние противоречия',
    'habits': 'Привычки / речь', 'self_image': 'Представление о себе',
}


# Compact Telegram categories rather than a seventeen-button questionnaire.
PROFILE_GROUPS = {
    'base': ('🪪 Основное', ('age', 'gender', 'species', 'origin', 'residence', 'occupation')),
    'past': ('📖 Биография', ('family', 'habits')),
    'mind': ('🧠 Психология', ('values', 'goals', 'fears', 'defenses', 'conflicts', 'self_image')),
    'bond': ('❤️ Отношения и границы', ('attachments', 'boundaries')),
}


class WorldMixin:
    def init_worlds(self):
        with self.connect() as db:
            existing = {r['name'] for r in db.execute('PRAGMA table_info(sessions)')}
            if 'world_id' not in existing:
                db.execute('ALTER TABLE sessions ADD COLUMN world_id INTEGER REFERENCES worlds(id)')
            existing = {r['name'] for r in db.execute('PRAGMA table_info(characters)')}
            if 'world_id' not in existing:
                db.execute('ALTER TABLE characters ADD COLUMN world_id INTEGER REFERENCES worlds(id)')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS worlds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS story_characters (
                    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user','bot','npc')),
                    PRIMARY KEY (session_id, character_id)
                );
                CREATE INDEX IF NOT EXISTS cast_character_idx ON story_characters(character_id);
                CREATE TABLE IF NOT EXISTS world_lore (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    world_id INTEGER NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(world_id,title COLLATE NOCASE)
                );
                CREATE INDEX IF NOT EXISTS world_lore_idx ON world_lore(world_id, pinned DESC, id DESC);
                CREATE TABLE IF NOT EXISTS world_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    world_id INTEGER NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
                    session_id INTEGER REFERENCES sessions(id),
                    source_turn_id INTEGER REFERENCES turns(id),
                    text TEXT NOT NULL,
                    evidence TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'confirmed' CHECK(status IN ('confirmed','candidate','archived')),
                    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS world_events_idx ON world_events(world_id,status,id DESC);
                CREATE TABLE IF NOT EXISTS character_fields (
                    character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
                    field TEXT NOT NULL,
                    session_id INTEGER NOT NULL DEFAULT 0,
                    value TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'author',
                    certainty TEXT NOT NULL DEFAULT 'confirmed',
                    evidence TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(character_id, field, session_id)
                );
                CREATE TABLE IF NOT EXISTS character_development (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
                    session_id INTEGER NOT NULL REFERENCES sessions(id),
                    turn_id INTEGER REFERENCES turns(id),
                    insight TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    certainty TEXT NOT NULL CHECK(certainty IN ('confirmed','interpretation')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(character_id,session_id,insight)
                );
                CREATE INDEX IF NOT EXISTS character_development_idx ON character_development(character_id,session_id,id DESC);
            ''')
            # Legacy characters retain their previous story memberships and photos.
            db.execute('''INSERT OR IGNORE INTO story_characters(session_id,character_id,role)
                SELECT session_id,id,role FROM characters''')
            if db.execute('PRAGMA user_version').fetchone()[0] < 6:
                db.execute('PRAGMA user_version=6')

    def add_world(self, name: str):
        name = name.strip()
        if not 1 <= len(name) <= 100:
            raise ValueError('Название мира должно быть от 1 до 100 символов.')
        with self.connect() as db:
            if any(w['name'].casefold() == name.casefold()
                   for w in db.execute('SELECT name FROM worlds')):
                raise ValueError('Мир с таким названием уже существует.')
            return db.execute('INSERT INTO worlds(name) VALUES (?)', (name,)).lastrowid

    def list_worlds(self):
        with self.connect() as db:
            return db.execute('''SELECT w.*,
                (SELECT COUNT(*) FROM sessions s WHERE s.world_id=w.id) AS story_count,
                (SELECT COUNT(*) FROM characters c WHERE c.world_id=w.id) AS character_count
                FROM worlds w ORDER BY w.name COLLATE NOCASE''').fetchall()

    def get_world(self, wid: int):
        with self.connect() as db:
            return db.execute('SELECT * FROM worlds WHERE id=?', (wid,)).fetchone()

    def set_world_description(self, wid: int, value: str):
        with self.connect() as db:
            db.execute('UPDATE worlds SET description=? WHERE id=?', (value[:22000], wid))

    def assign_story_world(self, sid: int, wid: int | None):
        if wid is not None and not self.get_world(wid):
            raise ValueError('Мир не найден.')
        with self.connect() as db:
            story = db.execute('SELECT world_id FROM sessions WHERE id=?', (sid,)).fetchone()
            if story is None:
                raise ValueError('История не найдена.')
            # A story with an existing cast cannot silently drag characters into another universe.
            mismatch = db.execute('''SELECT COUNT(*) FROM story_characters sc
                JOIN characters c ON c.id=sc.character_id
                WHERE sc.session_id=? AND c.world_id IS NOT NULL AND c.world_id IS NOT ?''',
                (sid, wid)).fetchone()[0]
            if mismatch:
                raise ValueError('В истории есть персонажи другого мира. Не меняй мир поверх их карточек.')
            db.execute('UPDATE sessions SET world_id=? WHERE id=?', (wid, sid))
            if wid is not None:
                db.execute('''UPDATE characters SET world_id=? WHERE world_id IS NULL
                    AND id IN (SELECT character_id FROM story_characters WHERE session_id=?)''', (wid, sid))

    def world_stories(self, wid: int):
        with self.connect() as db:
            return db.execute('SELECT * FROM sessions WHERE world_id=? ORDER BY id DESC', (wid,)).fetchall()

    def world_characters(self, wid: int):
        with self.connect() as db:
            return db.execute('SELECT * FROM characters WHERE world_id=? ORDER BY name COLLATE NOCASE', (wid,)).fetchall()

    def story_has_character(self, sid: int, cid: int):
        with self.connect() as db:
            return bool(db.execute('SELECT 1 FROM story_characters WHERE session_id=? AND character_id=?', (sid,cid)).fetchone())

    def attach_character(self, sid: int, cid: int, role: str | None = None):
        if role is not None and role not in ('user','bot','npc'):
            raise ValueError('Неверная роль.')
        with self.connect() as db:
            story = db.execute('SELECT world_id FROM sessions WHERE id=?', (sid,)).fetchone()
            char = db.execute('SELECT world_id,role,session_id FROM characters WHERE id=?', (cid,)).fetchone()
            if not story or not char:
                raise ValueError('История или персонаж не найдены.')
            if story['world_id'] is None or char['world_id'] != story['world_id']:
                raise ValueError('Персонаж и история должны принадлежать одному миру.')
            db.execute('INSERT OR IGNORE INTO story_characters(session_id,character_id,role) VALUES (?,?,?)',
                       (sid,cid,role or char['role']))

    def detach_character(self, sid: int, cid: int):
        with self.connect() as db:
            return bool(db.execute('DELETE FROM story_characters WHERE session_id=? AND character_id=?',
                                   (sid,cid)).rowcount)

    def append_world_description(self, wid: int, paragraph: str):
        paragraph = paragraph.strip()
        if not paragraph:
            raise ValueError('Пустое дополнение.')
        world = self.get_world(wid)
        if not world:
            raise ValueError('Мир не найден.')
        result = world['description'].strip()
        result = (result + '\n\n' if result else '') + paragraph
        if len(result) > 22000:
            raise ValueError('Канон достиг лимита 22 000 символов. Сократи текст или используй хронику.')
        self.set_world_description(wid, result)

    def character_stories(self, cid: int):
        with self.connect() as db:
            return db.execute('''SELECT s.* FROM sessions s
                JOIN story_characters sc ON sc.session_id=s.id
                WHERE sc.character_id=? ORDER BY s.id DESC''', (cid,)).fetchall()

    def add_world_lore(self, wid: int, title: str, body: str):
        title = title.strip()
        body = body.strip()
        if not title or not body or len(title)>120 or len(body)>15000:
            raise ValueError('Нужны название до 120 и текст до 15 000 символов.')
        with self.connect() as db:
            return db.execute('INSERT INTO world_lore(world_id,title,body) VALUES (?,?,?)',
                              (wid,title,body)).lastrowid

    def list_world_lore(self, wid: int):
        with self.connect() as db:
            return db.execute('SELECT * FROM world_lore WHERE world_id=? ORDER BY pinned DESC,title COLLATE NOCASE',
                              (wid,)).fetchall()

    def get_world_lore(self, lore_id: int):
        with self.connect() as db:
            return db.execute('SELECT * FROM world_lore WHERE id=?', (lore_id,)).fetchone()

    def update_world_lore(self, lore_id: int, body: str):
        body = body.strip()
        if not 1 <= len(body) <= 15000:
            raise ValueError('Текст статьи: от 1 до 15000 символов.')
        with self.connect() as db:
            db.execute('UPDATE world_lore SET body=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                       (body,lore_id))

    def append_world_lore(self, lore_id: int, body: str):
        lore = self.get_world_lore(lore_id)
        if not lore:
            raise ValueError('Статья не найдена.')
        self.update_world_lore(lore_id, lore['body'].rstrip() + '\n\n' + body.strip())

    def pin_world_lore(self, lore_id: int, pinned: bool):
        with self.connect() as db:
            db.execute('UPDATE world_lore SET pinned=? WHERE id=?', (int(pinned),lore_id))

    def delete_world_lore(self, lore_id: int):
        with self.connect() as db:
            db.execute('DELETE FROM world_lore WHERE id=?', (lore_id,))

    def relevant_world_lore(self, wid: int, query: str, *, max_chars: int = 9500,
                             max_items: int = 7):
        from memory import tokens
        keys = tokens(query)
        def related_terms(article_terms: set[str]) -> int:
            # Very small inflection-tolerant match for proper nouns.  E.g.
            # Псеумы/Псеуме, Эльдора/Эльдоре, Севтос/Севтоса.
            # Exact matches remain preferable and short words aren't fuzzed.
            return sum(1 for term in article_terms if any(
                term == word or (len(term) >= 5 and len(word) >= 5
                                 and term[:5] == word[:5]) for word in keys))
        scored = []
        for article in self.list_world_lore(wid):
            title = article['title']
            # Title matches are much more reliable than full-text accidental overlaps.
            title_terms = tokens(title)
            title_score = 25 * related_terms(title_terms)
            mention = 90 if title.casefold() in query.casefold() else 0
            body_score = min(10, len(tokens(article['body'][:2500]) & keys))
            pinned_score = 75 if article['pinned'] else 0
            score = title_score + mention + body_score + pinned_score
            if score > 0:
                scored.append((score,article['id'],article))
        scored.sort(key=lambda x:(x[0],x[1]),reverse=True)
        out=[]
        remaining=max_chars
        for _,_,article in scored[:max_items]:
            body=article['body'][:max(0,min(remaining-120,6000))]
            if not body or remaining<180:
                break
            text=f"[{article['title']}]\n{body}"
            out.append(text)
            remaining-=len(text)
        return out

    def add_world_event(self, wid: int, text: str, sid: int | None = None, turn_id: int | None = None,
                        evidence: str = '', status: str = 'confirmed'):
        text = ' '.join(text.strip().split())[:650]
        if len(text) < 9 or status not in ('confirmed','candidate','archived'):
            return None
        with self.connect() as db:
            found = db.execute('SELECT id FROM world_events WHERE world_id=? AND lower(text)=lower(?) AND status != "archived"',
                               (wid,text)).fetchone()
            if found:
                return found['id']
            return db.execute('''INSERT INTO world_events(world_id,session_id,source_turn_id,text,evidence,status)
                VALUES (?,?,?,?,?,?)''', (wid,sid,turn_id,text,evidence[:300],status)).lastrowid

    def list_world_events(self, wid: int, limit: int = 30, include_candidates: bool = False):
        with self.connect() as db:
            if include_candidates:
                return db.execute('SELECT * FROM world_events WHERE world_id=? AND status != "archived" ORDER BY id DESC LIMIT ?',
                                  (wid,limit)).fetchall()
            return db.execute('SELECT * FROM world_events WHERE world_id=? AND status="confirmed" ORDER BY id DESC LIMIT ?',
                              (wid,limit)).fetchall()

    def set_world_event_status(self, event_id: int, status: str):
        if status not in ('confirmed','archived'):
            raise ValueError('Неверный статус.')
        with self.connect() as db:
            db.execute('UPDATE world_events SET status=? WHERE id=?', (status,event_id))

    def get_world_event(self, event_id: int):
        with self.connect() as db:
            return db.execute('SELECT * FROM world_events WHERE id=?', (event_id,)).fetchone()

    def set_profile_field(self, cid: int, field: str, value: str, *, sid: int = 0,
                          source: str = 'author', certainty: str = 'confirmed', evidence: str = ''):
        if field not in PROFILE_FIELDS:
            raise ValueError('Неизвестное поле анкеты.')
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM characters WHERE id=?', (cid,)).fetchone():
                raise ValueError('Персонаж не найден.')
            if not value.strip():
                db.execute('DELETE FROM character_fields WHERE character_id=? AND field=? AND session_id=?',
                           (cid,field,sid))
            else:
                db.execute('''INSERT INTO character_fields(character_id,field,session_id,value,source,certainty,evidence)
                    VALUES (?,?,?,?,?,?,?) ON CONFLICT(character_id,field,session_id)
                    DO UPDATE SET value=excluded.value, source=excluded.source,
                    certainty=excluded.certainty, evidence=excluded.evidence,updated_at=CURRENT_TIMESTAMP''',
                    (cid,field,sid,value.strip()[:1500],source,certainty,evidence[:350]))

    def profile_fields(self, cid: int, sid: int | None = None):
        with self.connect() as db:
            if sid is None:
                return db.execute('SELECT * FROM character_fields WHERE character_id=? AND session_id=0 ORDER BY field',
                                  (cid,)).fetchall()
            return db.execute('''SELECT * FROM character_fields WHERE character_id=? AND session_id IN (0,?)
                ORDER BY session_id ASC, field''', (cid,sid)).fetchall()

    def add_development(self, cid: int, sid: int, turn_id: int | None, insight: str,
                        evidence: str, certainty: str):
        if certainty not in ('confirmed','interpretation') or not evidence or len(insight.strip()) < 9:
            return None
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM story_characters WHERE character_id=? AND session_id=?', (cid,sid)).fetchone():
                return None
            return db.execute('''INSERT OR IGNORE INTO character_development
                (character_id,session_id,turn_id,insight,evidence,certainty) VALUES (?,?,?,?,?,?)''',
                (cid,sid,turn_id,insight.strip()[:550],evidence[:320],certainty)).lastrowid

    def development(self, cid: int, sid: int, limit: int = 25):
        with self.connect() as db:
            return db.execute('''SELECT * FROM character_development WHERE character_id=? AND session_id=?
                ORDER BY id DESC LIMIT ?''', (cid,sid,limit)).fetchall()

    def enable_library_only_characters(self):
        """v0.6.1: allow world-library character without inventing a story.

        SQLite cannot DROP NOT NULL from an existing column, so make a verified,
        backed-up table copy. Child tables retain their original FK name.
        """
        with self.connect() as db:
            col = next(r for r in db.execute('PRAGMA table_info(characters)')
                       if r['name']=='session_id')
            if not col['notnull']:
                return
            integrity = db.execute('PRAGMA quick_check').fetchone()[0]
            if integrity != 'ok':
                raise RuntimeError('Ошибка целостности базы перед обновлением анкет: '+str(integrity))
        self.make_backup()
        import sqlite3
        db=sqlite3.connect(self.path)
        try:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE characters_library_upgrade (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER REFERENCES sessions(id),
                name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'npc' CHECK(role IN ('user','bot','npc')),
                description TEXT NOT NULL DEFAULT '',
                appearance_override TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                world_id INTEGER REFERENCES worlds(id),
                UNIQUE(session_id, name COLLATE NOCASE)
            )''')
            db.execute('''INSERT INTO characters_library_upgrade
                (id,session_id,name,role,description,appearance_override,created_at,world_id)
                SELECT id,session_id,name,role,description,appearance_override,created_at,world_id
                FROM characters''')
            db.execute('DROP TABLE characters')
            db.execute('ALTER TABLE characters_library_upgrade RENAME TO characters')
            db.execute('CREATE INDEX IF NOT EXISTS characters_session_idx ON characters(session_id)')
            db.execute('PRAGMA user_version=7')
            db.commit()
            db.execute('PRAGMA foreign_keys=ON')
            mistakes=list(db.execute('PRAGMA foreign_key_check'))
            if mistakes:
                raise RuntimeError(f'Найдены нарушенные ссылки после миграции персонажей: {mistakes[:3]}')
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
