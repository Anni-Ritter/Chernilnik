"""SQLite: полные ходы и отдельная структурированная память каждого сюжета."""
from __future__ import annotations

import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path: str):
        self.path = str(Path(path))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def init_db(self):
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    world TEXT NOT NULL DEFAULT '',
                    cast TEXT NOT NULL DEFAULT '',
                    scene_time TEXT NOT NULL DEFAULT '',
                    current_location TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS openings (
                    session_id INTEGER PRIMARY KEY REFERENCES sessions(id),
                    bot_text TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL REFERENCES sessions(id),
                    user_text TEXT NOT NULL,
                    bot_text TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL REFERENCES sessions(id),
                    text TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'event',
                    importance INTEGER NOT NULL DEFAULT 3 CHECK(importance BETWEEN 1 AND 5),
                    pinned INTEGER NOT NULL DEFAULT 0,
                    certainty TEXT NOT NULL DEFAULT 'confirmed',
                    known_by TEXT NOT NULL DEFAULT '',
                    source_turn_id INTEGER REFERENCES turns(id),
                    evidence TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS facts_session_idx ON facts(session_id, importance DESC);
                CREATE INDEX IF NOT EXISTS turns_session_idx ON turns(session_id, id DESC);
                CREATE TABLE IF NOT EXISTS usage_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL REFERENCES sessions(id),
                    operation TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS activity (
                    user_id INTEGER PRIMARY KEY,
                    session_id INTEGER REFERENCES sessions(id)
                );
                CREATE TABLE IF NOT EXISTS characters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL REFERENCES sessions(id),
                    name TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'npc' CHECK(role IN ('user','bot','npc')),
                    description TEXT NOT NULL DEFAULT '',
                    appearance_override TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(session_id, name COLLATE NOCASE)
                );
                CREATE INDEX IF NOT EXISTS characters_session_idx ON characters(session_id);
                CREATE TABLE IF NOT EXISTS character_photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    character_id INTEGER NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
                    telegram_file_id TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    visual_description TEXT NOT NULL DEFAULT '',
                    analysis_status TEXT NOT NULL DEFAULT 'pending',
                    is_primary INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS photos_character_idx ON character_photos(character_id);
                CREATE TABLE IF NOT EXISTS drafts (
                    user_id INTEGER PRIMARY KEY,
                    session_id INTEGER NOT NULL REFERENCES sessions(id),
                    text TEXT NOT NULL DEFAULT ''
                );
            ''')

    def new_session(self, user_id: int, title: str):
        with self.connect() as db:
            cursor = db.execute("INSERT INTO sessions(title) VALUES (?)", (title,))
            sid = cursor.lastrowid
            db.execute("INSERT INTO activity(user_id,session_id) VALUES (?,?) "
                       "ON CONFLICT(user_id) DO UPDATE SET session_id=excluded.session_id", (user_id, sid))
        return sid

    def list_sessions(self):
        with self.connect() as db:
            return db.execute("SELECT * FROM sessions ORDER BY id DESC").fetchall()

    def get_session(self, sid: int):
        with self.connect() as db:
            return db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()

    def current_session(self, user_id: int):
        with self.connect() as db:
            return db.execute("SELECT s.* FROM activity a JOIN sessions s ON s.id=a.session_id "
                              "WHERE a.user_id=?", (user_id,)).fetchone()

    def switch_session(self, user_id: int, sid: int):
        if not self.get_session(sid):
            return False
        with self.connect() as db:
            db.execute("INSERT INTO activity(user_id,session_id) VALUES (?,?) "
                       "ON CONFLICT(user_id) DO UPDATE SET session_id=excluded.session_id", (user_id, sid))
            db.execute("DELETE FROM drafts WHERE user_id=?", (user_id,))
        return True

    def set_session_field(self, sid: int, field: str, value: str):
        if field not in {'world', 'cast', 'scene_time', 'current_location'}:
            raise ValueError('invalid field')
        with self.connect() as db:
            db.execute(f'UPDATE sessions SET {field}=? WHERE id=?', (value, sid))

    def get_opening(self, sid: int):
        with self.connect() as db:
            return db.execute('SELECT * FROM openings WHERE session_id=?', (sid,)).fetchone()

    def add_opening(self, sid: int, bot_text: str, model: str, in_tokens: int, out_tokens: int):
        """One and only one opening per story: no rerolls, even after bot restarts."""
        with self.connect() as db:
            cursor = db.execute('INSERT OR IGNORE INTO openings(session_id,bot_text,model,input_tokens,output_tokens) '
                                'VALUES (?,?,?,?,?)', (sid, bot_text, model, in_tokens, out_tokens))
            if cursor.rowcount != 1:
                raise ValueError('The opening already exists')

    def add_turn(self, sid: int, user_text: str, bot_text: str, model: str, in_tokens: int, out_tokens: int):
        with self.connect() as db:
            cur = db.execute("INSERT INTO turns(session_id,user_text,bot_text,model,input_tokens,output_tokens) "
                             "VALUES (?,?,?,?,?,?)", (sid, user_text, bot_text, model, in_tokens, out_tokens))
            return cur.lastrowid

    def recent_turns(self, sid: int, limit: int = 8):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM turns WHERE session_id=? ORDER BY id DESC LIMIT ?", (sid, limit)).fetchall()
        return list(reversed(rows))

    def add_fact(self, sid: int, text: str, category='event', importance=3, pinned=False,
                 certainty='confirmed', known_by='', source_turn_id=None, evidence=''):
        text = ' '.join(text.strip().split())[:600]
        if len(text) < 9:
            return None
        with self.connect() as db:
            exists = db.execute("SELECT id FROM facts WHERE session_id=? AND lower(text)=lower(?)", (sid, text)).fetchone()
            if exists:
                if pinned:
                    db.execute("UPDATE facts SET pinned=1, importance=5 WHERE id=?", (exists['id'],))
                return exists['id']
            cur = db.execute("INSERT INTO facts(session_id,text,category,importance,pinned,certainty,known_by,source_turn_id,evidence) "
                             "VALUES (?,?,?,?,?,?,?,?,?)",
                             (sid, text, category[:30], importance, int(bool(pinned)), certainty[:25],
                              known_by[:160], source_turn_id, evidence[:220]))
            return cur.lastrowid

    def list_facts(self, sid: int, limit: int = 10000):
        with self.connect() as db:
            return db.execute("SELECT * FROM facts WHERE session_id=? ORDER BY pinned DESC, importance DESC, id DESC LIMIT ?",
                              (sid, limit)).fetchall()

    def add_usage(self, sid: int, operation: str, model: str, input_tokens: int, output_tokens: int):
        with self.connect() as db:
            db.execute("INSERT INTO usage_log(session_id,operation,model,input_tokens,output_tokens) "
                       "VALUES (?,?,?,?,?)", (sid, operation, model, input_tokens, output_tokens))

    def usage_summary(self, sid: int):
        with self.connect() as db:
            return db.execute("SELECT operation, model, SUM(input_tokens) AS input_tokens, "
                              "SUM(output_tokens) AS output_tokens FROM usage_log "
                              "WHERE session_id=? GROUP BY operation,model", (sid,)).fetchall()

    def draft_active(self, user_id: int):
        with self.connect() as db:
            return db.execute("SELECT * FROM drafts WHERE user_id=?", (user_id,)).fetchone()

    def start_draft(self, user_id: int, sid: int):
        with self.connect() as db:
            db.execute("INSERT INTO drafts(user_id,session_id,text) VALUES (?,?,'') "
                       "ON CONFLICT(user_id) DO UPDATE SET session_id=excluded.session_id,text=''", (user_id, sid))

    def append_draft(self, user_id: int, value: str):
        with self.connect() as db:
            db.execute("UPDATE drafts SET text=text || CASE WHEN text='' THEN '' ELSE '\n\n' END || ? WHERE user_id=?",
                       (value, user_id))

    def clear_draft(self, user_id: int):
        with self.connect() as db:
            db.execute("DELETE FROM drafts WHERE user_id=?", (user_id,))

    def export(self, sid: int):
        session = self.get_session(sid)
        if not session:
            raise ValueError('unknown session')
        with self.connect() as db:
            rows = db.execute("SELECT * FROM turns WHERE session_id=? ORDER BY id", (sid,)).fetchall()
        output = [f"# {session['title']}", f"\n**Мир:** {session['world'] or 'Свободная ролевая'}",
                  f"\n**Роли:** {session['cast'] or 'Не установлены'}", ""]
        opening = self.get_opening(sid)
        if opening:
            output.extend(['## Первый пост соавтора', '', opening['bot_text'], ''])
        chars = self.list_characters(sid)
        if chars:
            output.extend(['## Карточки персонажей', ''])
            for ch in chars:
                output.extend([f"### {ch['name']} ({ch['role']})", '', ch['description'], '',
                               'Уточнение внешности автора: ' + ch['appearance_override'], ''])
                for photo in self.photos(ch['id']):
                    output.extend([f"Фото-референс #{photo['id']} (основной: {bool(photo['is_primary'])}): "
                                   + photo['visual_description'], ''])
        for row in rows:
            output.extend([f"## Ход {row['id']} · {row['created_at']}", "", "### Ты", "", row['user_text'],
                           "", "### Соавтор", "", row['bot_text'], ""])
        return '\n'.join(output)

    # Character sheets and image references are separate for each story.
    # The schema uses CREATE IF NOT EXISTS, so old Bothost databases migrate in place.
    def add_character(self, sid: int, name: str, role: str = 'npc') -> int:
        if role not in ('user', 'bot', 'npc') or not (1 <= len(name.strip()) <= 80):
            raise ValueError('invalid character')
        with self.connect() as db:
            cursor = db.execute('INSERT INTO characters(session_id,name,role) VALUES (?,?,?)',
                                (sid, name.strip(), role))
            return cursor.lastrowid

    def list_characters(self, sid: int):
        with self.connect() as db:
            return db.execute('SELECT * FROM characters WHERE session_id=? ORDER BY name COLLATE NOCASE', (sid,)).fetchall()

    def get_character(self, cid: int):
        with self.connect() as db:
            return db.execute('SELECT * FROM characters WHERE id=?', (cid,)).fetchone()

    def set_character_role(self, cid: int, role: str):
        if role not in ('user', 'bot', 'npc'):
            raise ValueError('unknown role')
        with self.connect() as db:
            db.execute('UPDATE characters SET role=? WHERE id=?', (role, cid))

    def set_character_text(self, cid: int, field: str, value: str):
        if field not in ('description', 'appearance_override'):
            raise ValueError('invalid character field')
        with self.connect() as db:
            db.execute(f'UPDATE characters SET {field}=? WHERE id=?', (value[:6000], cid))

    def photos(self, cid: int):
        with self.connect() as db:
            return db.execute('SELECT * FROM character_photos WHERE character_id=? ORDER BY is_primary DESC, id DESC',
                              (cid,)).fetchall()

    def get_photo(self, pid: int):
        with self.connect() as db:
            return db.execute('SELECT * FROM character_photos WHERE id=?', (pid,)).fetchone()

    def add_photo(self, cid: int, file_id: str, path: str, description: str = '', status: str = 'ok'):
        with self.connect() as db:
            existing = db.execute('SELECT COUNT(*) FROM character_photos WHERE character_id=?', (cid,)).fetchone()[0]
            if existing >= 5:
                raise ValueError('max 5 photos per character')
            db.execute('UPDATE character_photos SET is_primary=0 WHERE character_id=?', (cid,))
            cur = db.execute('INSERT INTO character_photos(character_id,telegram_file_id,local_path,visual_description,analysis_status,is_primary) '
                             'VALUES (?,?,?,?,?,1)', (cid, file_id, path, description[:2500], status))
            return cur.lastrowid

    def update_photo_analysis(self, pid: int, description: str, status: str):
        with self.connect() as db:
            db.execute('UPDATE character_photos SET visual_description=?,analysis_status=? WHERE id=?',
                       (description[:2500], status, pid))

    def make_primary_photo(self, cid: int, pid: int):
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM character_photos WHERE id=? AND character_id=?', (pid, cid)).fetchone():
                raise ValueError('photo does not belong to character')
            db.execute('UPDATE character_photos SET is_primary=0 WHERE character_id=?', (cid,))
            db.execute('UPDATE character_photos SET is_primary=1 WHERE id=?', (pid,))

    def delete_photo(self, cid: int, pid: int):
        with self.connect() as db:
            row = db.execute('SELECT * FROM character_photos WHERE id=? AND character_id=?', (pid, cid)).fetchone()
            if row is None:
                return None
            db.execute('DELETE FROM character_photos WHERE id=?', (pid,))
            if row['is_primary']:
                next_photo = db.execute('SELECT id FROM character_photos WHERE character_id=? ORDER BY id DESC LIMIT 1',
                                        (cid,)).fetchone()
                if next_photo:
                    db.execute('UPDATE character_photos SET is_primary=1 WHERE id=?', (next_photo['id'],))
            return dict(row)

    def rename_character(self, cid: int, name: str):
        if not (1 <= len(name.strip()) <= 80):
            raise ValueError('invalid name')
        with self.connect() as db:
            db.execute('UPDATE characters SET name=? WHERE id=?', (name.strip(), cid))

    def delete_character(self, sid: int, cid: int):
        with self.connect() as db:
            record = db.execute('SELECT * FROM characters WHERE id=? AND session_id=?', (cid, sid)).fetchone()
            if not record:
                return []
            photos = db.execute('SELECT local_path FROM character_photos WHERE character_id=?', (cid,)).fetchall()
            db.execute('DELETE FROM characters WHERE id=? AND session_id=?', (cid, sid))
            return [row['local_path'] for row in photos]
