"""Non-destructive database path checks for persistent BotHost deployments."""
from __future__ import annotations

import os
from pathlib import Path


def resolve_database_path(
    configured: str | None = None,
    *,
    persistent_dir: str | Path = '/app/data',
    allow_new: bool = False,
) -> str:
    """On BotHost, fail closed instead of silently creating an empty database.

    For development outside the BotHost persistent mount, a relative SQLite path
    remains supported. The caller is responsible for opting in to first-time
    database creation via ALLOW_NEW_DATABASE=true on a newly deployed BotHost.
    """
    root = Path(persistent_dir)
    value = (configured if configured is not None else os.getenv('DATABASE_PATH', '')).strip()

    if not root.is_dir():
        if value and (Path(value).is_absolute() and Path(value).as_posix().startswith(str(root) + '/')):
            raise RuntimeError(
                'The configured persistent directory is missing: ' + str(root) + '. '
                'Refusing to create a fake /app/data directory without the BotHost mount.'
            )
        return str(Path(value or 'roleplay.sqlite3'))

    root = root.resolve()
    target = Path(value or str(root / 'roleplay.sqlite3'))
    if not target.is_absolute():
        raise RuntimeError(
            'DATABASE_PATH points to a relative SQLite file: ' + str(target) + '. '
            'On BotHost use DATABASE_PATH=' + str(root / 'roleplay.sqlite3') + '. '
            'Do not restart/redeploy until you have checked the old database.'
        )
    target = target.resolve()
    if not target.is_relative_to(root):
        raise RuntimeError(
            'Database is outside the persistent BotHost directory: ' + str(target) + '. '
            'Move/recover it under ' + str(root) + ' before deployment.'
        )
    if not target.is_file() and not allow_new:
        raise RuntimeError(
            'Persistent roleplay database not found: ' + str(target) + '. '
            'Bot will NOT create an empty replacement. Check old files and backups. '
            'For an intentional first deployment only, temporarily set '
            'ALLOW_NEW_DATABASE=true, start once, then remove the flag.'
        )
    return str(target)
