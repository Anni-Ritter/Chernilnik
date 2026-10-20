"""v0.8.1: owner-confirmed timeline rewind with hot SQLite snapshot.

Never edits Telegram history; never rewrites generated prose. Works on one story only.
"""
from __future__ import annotations

import sqlite3
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RewindResult:
    removed: int
    last_kept_id: int | None
    backup_path: Path


class RewindMixin:
    def rewind_candidates(self, sid: int, query: str = '', limit: int = 12):
        """Show narrative turns rather than Telegram message ids."""
        with self.connect() as con:
            # Casefold in Python so Cyrillic lookups work reliably on SQLite.
            rows = con.execute('''SELECT id,user_text,bot_text,created_at FROM turns
                WHERE session_id=? ORDER BY id DESC LIMIT 2000''',(sid,)).fetchall()
        if query.strip():
            needle = query.strip().casefold()
            # Handle inflection like «поцелуй» vs «поцеловала» without a
            # paid search call. Exact phrase still gets first priority.
            terms = re.findall(r'[а-яёa-z0-9]{5,}', needle)
            def matches(row):
                text = (row['user_text']+'\n'+row['bot_text']).casefold()
                if needle in text:
                    return True
                words = re.findall(r'[а-яёa-z0-9]{5,}',text)
                return bool(terms) and all(any(w.startswith(t[:5]) for w in words) for t in terms)
            rows = [r for r in rows if matches(r)]
        return rows[:max(1,min(limit,20))]

    def rewind_preview(self, sid: int, target_id: int, keep: bool, expected_tail: int | None = None):
        with self.connect() as con:
            target = con.execute('SELECT * FROM turns WHERE session_id=? AND id=?',
                                 (sid,target_id)).fetchone()
            if target is None:
                raise ValueError('Ход не найден в текущей истории.')
            tail = con.execute('SELECT MAX(id) FROM turns WHERE session_id=?',(sid,)).fetchone()[0]
            if expected_tail is not None and tail != expected_tail:
                raise ValueError('Пока ты выбирала точку отката, появились новые ходы. Открой /rewind снова.')
            op = '>' if keep else '>='
            count = con.execute(f'SELECT COUNT(*) FROM turns WHERE session_id=? AND id {op} ?',
                                (sid,target_id)).fetchone()[0]
            if count < 1:
                raise ValueError('Нет более поздних ходов, которые можно откатить.')
            return {'target': target, 'tail': tail, 'removed':count, 'keep':keep}

    def rewind_to(self, sid: int, target_id: int, keep: bool, expected_tail: int) -> RewindResult:
        """Back up first, then atomically trim one story and its derived timeline.

        Manual, story-scoped character overrides are not automatically time-stamped;
        they are preserved and need review. Auto character-field summaries are cleared
        to prevent post-cutoff beliefs contaminating the revised story.
        """
        self.rewind_preview(sid, target_id, keep, expected_tail)
        backup = self.make_backup()
        if not backup.exists() or backup.stat().st_size == 0:
            raise RuntimeError('Не удалось проверить резервную копию. Откат отменён.')
        op = '>' if keep else '>='
        with self.connect() as con:
            # Transaction guards against a change after preview/backup.
            con.execute('BEGIN IMMEDIATE')
            tail = con.execute('SELECT MAX(id) FROM turns WHERE session_id=?',(sid,)).fetchone()[0]
            if tail != expected_tail:
                raise ValueError('История изменилась за время подтверждения. Откат отменён.')
            remaining_target = con.execute('SELECT 1 FROM turns WHERE session_id=? AND id=?',
                                           (sid,target_id)).fetchone()
            if not remaining_target:
                raise ValueError('Выбранный ход больше не существует.')
            count = con.execute(f'SELECT COUNT(*) FROM turns WHERE session_id=? AND id {op} ?',
                                (sid,target_id)).fetchone()[0]
            if count < 1:
                raise ValueError('Нечего откатывать.')

            # Delete dependent, *post-cutoff* story evidence before the turns
            # because foreign keys intentionally do not cascade narrative loss.
            for table, field in (
                ('facts','source_turn_id'),
                ('world_events','source_turn_id'),
                ('character_development','turn_id'),
                ('memory_jobs','turn_id'),
                ('scene_jobs','turn_id'),
                ('scene_state_history','source_turn_id'),
            ):
                con.execute(f'DELETE FROM {table} WHERE session_id=? AND {field} {op} ?',
                            (sid,target_id))

            # Current scene is reconstructed ONLY from retained historical
            # state updates. Manual edits have no timestamp; discard to avoid
            # silently pinning a now-discarded later event.
            con.execute('DELETE FROM scene_state WHERE session_id=?',(sid,))
            kept_history = con.execute('''SELECT h.* FROM scene_state_history h
                WHERE h.session_id=? AND h.id IN (
                  SELECT MAX(id) FROM scene_state_history WHERE session_id=?
                  GROUP BY scope,name,aspect
                )''',(sid,sid)).fetchall()
            for r in kept_history:
                con.execute('''INSERT INTO scene_state
                  (session_id,scope,name,aspect,value,source_turn_id,source,evidence)
                  VALUES (?,?,?,?,?,?,?,?)''',
                  (sid,r['scope'],r['name'],r['aspect'],r['new_value'],
                   r['source_turn_id'],r['source'],r['evidence']))

            # Character profile fields have no turn_id, only evidence. Retain
            # them ONLY when their quoted evidence still exists in a kept
            # post. Otherwise drop stale profiles inferred from the discarded
            # branch. World-wide and manually-authored entries are untouched.
            kept_posts = con.execute(f'''SELECT user_text,bot_text FROM turns
                WHERE session_id=? AND id {'<=' if keep else '<'} ?''',
                (sid,target_id)).fetchall()
            opening = con.execute('SELECT bot_text FROM openings WHERE session_id=?',(sid,)).fetchone()
            source_posts = [text for r in kept_posts for text in (r['user_text'],r['bot_text'])]
            if opening:
                source_posts.append(opening['bot_text'])
            automatic = con.execute('''SELECT character_id,field,evidence FROM character_fields
                WHERE session_id=? AND source IN ('model','story')''',(sid,)).fetchall()
            for field in automatic:
                evidence = field['evidence'].strip()
                if not evidence or not any(evidence in post for post in source_posts):
                    con.execute('''DELETE FROM character_fields
                        WHERE character_id=? AND field=? AND session_id=?''',
                        (field['character_id'],field['field'],sid))
            con.execute('DELETE FROM prompt_audit WHERE session_id=?',(sid,))
            con.execute('DELETE FROM drafts WHERE session_id=?',(sid,))
            con.execute(f'DELETE FROM turns WHERE session_id=? AND id {op} ?',(sid,target_id))
            errors = con.execute('PRAGMA foreign_key_check').fetchall()
            if errors:
                raise sqlite3.IntegrityError('После отката обнаружены некорректные связи в SQLite.')
            last = con.execute('SELECT MAX(id) FROM turns WHERE session_id=?',(sid,)).fetchone()[0]
            # Usage log is kept as honest financial history; other stories,
            # world lore, character photos and global profiles untouched.
        return RewindResult(count, last, backup)
