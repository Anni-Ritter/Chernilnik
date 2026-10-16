"""SQLite: полные ходы и отдельная структурированная память каждого сюжета."""
from __future__ import annotations

import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
from worlds import WorldMixin

log = logging.getLogger(__name__)


class Store(WorldMixin):
    def __init__(self, path: str):
        self.path = str(Path(path))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._backup_before_migration()
        self.init_db()
        self._backup_before_world_migration()
        self.init_worlds()

    def make_backup(self) -> Path:
        """Consistent hot snapshot; does not alter or move live SQLite."""
        source = Path(self.path)
        backup_dir = source.parent / 'backups'
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')
        destination = backup_dir / f'{source.stem}.manual_{timestamp}.sqlite3'
        try:
            with self.connect() as db, sqlite3.connect(destination) as target:
                db.backup(target)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return destination

    def _backup_before_migration(self):
        """Snapshot pre-v0.4 SQLite including WAL before adding a search index.

        Never delete the source or auto-purge backups. If a snapshot fails,
        refuse migration rather than silently risk losing a long-running story.
        """
        source = Path(self.path)
        if not source.exists() or source.stat().st_size == 0:
            return
        with self.connect() as db:
            user_version = db.execute('PRAGMA user_version').fetchone()[0]
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables or user_version >= 4:
                return
            check = db.execute('PRAGMA quick_check').fetchone()[0]
            if check != 'ok':
                raise RuntimeError('SQLite database is not healthy; migration cancelled: ' + str(check))
            backup_dir = source.parent / 'backups'
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')
            destination = backup_dir / f'{source.stem}.before_v04_{stamp}.sqlite3'
            try:
                with sqlite3.connect(destination) as target:
                    db.backup(target)
            except Exception:
                destination.unlink(missing_ok=True)
                raise
            log.warning('Created pre-migration SQLite backup: %s', destination)

    def _backup_before_world_migration(self):
        with self.connect() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            migrated = 'world_id' in {r['name'] for r in db.execute('PRAGMA table_info(sessions)')}
            if version >= 6 and migrated:
                return
            has_stories = db.execute('SELECT EXISTS(SELECT 1 FROM sessions)').fetchone()[0]
            if not has_stories:
                return
            check = db.execute('PRAGMA quick_check').fetchone()[0]
            if check != 'ok':
                raise RuntimeError('SQLite integrity check failed: ' + str(check))
        snap = self.make_backup()
        log.warning('Pre-v0.6 migration SQLite snapshot: %s', snap)

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
                CREATE TABLE IF NOT EXISTS prompt_audit (
                    session_id INTEGER PRIMARY KEY REFERENCES sessions(id),
                    stage TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_text TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS style_examples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            ''')
            self._ensure_fts(db)
            if db.execute('PRAGMA user_version').fetchone()[0] < 4:
                db.execute('PRAGMA user_version=4')

    def _ensure_fts(self, db):
        """Indexed search of old turns. Gracefully degrade if SQLite lacks FTS5."""
        try:
            present = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='turns_fts'").fetchone()
            db.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
                    user_text, bot_text, content='turns', content_rowid='id', tokenize='unicode61'
                );
                CREATE TRIGGER IF NOT EXISTS turns_search_insert AFTER INSERT ON turns BEGIN
                    INSERT INTO turns_fts(rowid, user_text, bot_text)
                    VALUES(new.id, new.user_text, new.bot_text);
                END;
                CREATE TRIGGER IF NOT EXISTS turns_search_delete AFTER DELETE ON turns BEGIN
                    INSERT INTO turns_fts(turns_fts, rowid, user_text, bot_text)
                    VALUES('delete', old.id, old.user_text, old.bot_text);
                END;
                CREATE TRIGGER IF NOT EXISTS turns_search_update AFTER UPDATE ON turns BEGIN
                    INSERT INTO turns_fts(turns_fts, rowid, user_text, bot_text)
                    VALUES('delete', old.id, old.user_text, old.bot_text);
                    INSERT INTO turns_fts(rowid, user_text, bot_text)
                    VALUES(new.id, new.user_text, new.bot_text);
                END;
            """)
            if not present:
                db.execute("INSERT INTO turns_fts(turns_fts) VALUES('rebuild')")
        except sqlite3.OperationalError as exc:
            log.warning('FTS5 is unavailable; will use a capped lexical fallback: %s', exc)

    def search_turns(self, sid: int, query: str, *, exclude_ids=(), limit: int = 3):
        """Find story-local OLD turns; never fall back to turns from another session.

        Search index contains no embeddings, making this usable without extra API
        cost; Russian inflections remain an acknowledged limitation.
        """
        import re
        from memory import tokens
        wanted = [w for w in sorted(tokens(query), key=len, reverse=True) if len(w) >= 5][:14]
        if not wanted or limit < 1:
            return []
        excluded = set(int(item) for item in exclude_ids)
        rows = []
        with self.connect() as db:
            try:
                match = ' OR '.join(f'{w}*' for w in wanted)
                rows = db.execute("""
                    SELECT t.* FROM turns_fts
                    JOIN turns t ON t.id=turns_fts.rowid
                    WHERE turns_fts MATCH ? AND t.session_id=?
                    ORDER BY bm25(turns_fts) ASC LIMIT 150
                """, (match, sid)).fetchall()
            except sqlite3.OperationalError as exc:
                log.debug('FTS search fell back to lexical scanning: %s', exc)
            if not rows:
                rows = db.execute(
                    'SELECT * FROM turns WHERE session_id=? ORDER BY id DESC LIMIT 800', (sid,)
                ).fetchall()
        wanted_set = set(wanted)
        scored = []
        for row in rows:
            if row['id'] in excluded:
                continue
            text = row['user_text'] + '\n' + row['bot_text']
            terms = tokens(text)
            overlap = sum(2 if w in terms else 1 if any(term.startswith(w[:6]) for term in terms) else 0
                          for w in wanted_set)
            if overlap == 0:
                continue
            scored.append((overlap, row['id'], row))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [row for _, _, row in scored[:min(5, limit)]]

    def add_style_example(self, user_id: int, text: str):
        text = text.strip()
        if not 50 <= len(text) <= 5500:
            raise ValueError('Style example must contain 50 to 5500 characters')
        with self.connect() as db:
            count = db.execute('SELECT COUNT(*) FROM style_examples WHERE user_id=?', (user_id,)).fetchone()[0]
            if count >= 15:
                raise ValueError('Maximum of 15 style examples')
            return db.execute('INSERT INTO style_examples(user_id,text) VALUES (?,?)',
                              (user_id, text)).lastrowid

    def list_style_examples(self, user_id: int | None = None):
        with self.connect() as db:
            if user_id is None:
                return db.execute('SELECT * FROM style_examples ORDER BY id DESC').fetchall()
            return db.execute('SELECT * FROM style_examples WHERE user_id=? ORDER BY id DESC',
                              (user_id,)).fetchall()

    def delete_style_example(self, user_id: int, example_id: int):
        with self.connect() as db:
            return db.execute('DELETE FROM style_examples WHERE id=? AND user_id=?',
                              (example_id, user_id)).rowcount > 0

    def save_prompt_audit(self, sid: int, stage: str, model: str, text: str):
        with self.connect() as db:
            db.execute("""INSERT INTO prompt_audit(session_id,stage,model,prompt_text)
                VALUES (?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET
                stage=excluded.stage,model=excluded.model,prompt_text=excluded.prompt_text,
                updated_at=CURRENT_TIMESTAMP""", (sid, stage, model, text))

    def get_prompt_audit(self, sid: int):
        with self.connect() as db:
            return db.execute('SELECT * FROM prompt_audit WHERE session_id=?', (sid,)).fetchone()

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

    def ensure_current_session(self, user_id: int):
        """Restore an existing story if the active-story pointer was lost."""
        session = self.current_session(user_id)
        if session is not None:
            return session
        existing = self.list_sessions()
        if existing:
            sid = existing[0]['id']
            self.switch_session(user_id, sid)
            return self.get_session(sid)
        sid = self.new_session(user_id, 'Свободная история')
        return self.get_session(sid)

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
            world = db.execute('SELECT world_id FROM sessions WHERE id=?', (sid,)).fetchone()
            if not world:
                raise ValueError('unknown story')
            # Reuse a world's library character instead of creating contradictory twins.
            if world['world_id'] is not None:
                existing = next((c for c in db.execute(
                    'SELECT name FROM characters WHERE world_id=?', (world['world_id'],))
                    if c['name'].casefold() == name.strip().casefold()), None)
                if existing:
                    raise ValueError('Герой с таким именем уже есть в библиотеке мира. Добавь его через «Жители мира».')
            cursor = db.execute('INSERT INTO characters(session_id,name,role,world_id) VALUES (?,?,?,?)',
                                (sid, name.strip(), role, world['world_id']))
            cid = cursor.lastrowid
            db.execute('INSERT INTO story_characters(session_id,character_id,role) VALUES (?,?,?)',
                       (sid,cid,role))
            return cid

    def list_characters(self, sid: int):
        with self.connect() as db:
            return db.execute('''SELECT c.id,c.session_id,c.name,sc.role AS role,c.description,
                c.appearance_override,c.created_at,c.world_id FROM story_characters sc
                JOIN characters c ON c.id=sc.character_id WHERE sc.session_id=?
                ORDER BY c.name COLLATE NOCASE''', (sid,)).fetchall()

    def get_character(self, cid: int):
        with self.connect() as db:
            return db.execute('SELECT * FROM characters WHERE id=?', (cid,)).fetchone()

    def set_character_role(self, cid: int, role: str):
        if role not in ('user', 'bot', 'npc'):
            raise ValueError('unknown role')
        with self.connect() as db:
            # Role is story-specific. Update memberships of a legacy single-story
            # character only; the UI uses set_story_character_role instead.
            db.execute('UPDATE characters SET role=? WHERE id=?', (role, cid))
            db.execute('UPDATE story_characters SET role=? WHERE character_id=?', (role, cid))

    def set_story_character_role(self, sid: int, cid: int, role: str):
        if role not in ('user', 'bot', 'npc'):
            raise ValueError('unknown role')
        with self.connect() as db:
            db.execute('UPDATE story_characters SET role=? WHERE session_id=? AND character_id=?',
                       (role,sid,cid))

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
            in_world = db.execute('SELECT world_id FROM characters WHERE id=?', (cid,)).fetchone()
            db.execute('DELETE FROM story_characters WHERE session_id=? AND character_id=?', (sid,cid))
            if in_world and in_world['world_id'] is not None:
                return []  # Shared library character: do not delete its sheet or photos.
            db.execute('DELETE FROM characters WHERE id=? AND session_id=?', (cid, sid))
            return [row['local_path'] for row in photos]
