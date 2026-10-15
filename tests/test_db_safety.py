"""Make sure a bad deployment can never silently reset roleplay history."""
import pytest

from db_safety import resolve_database_path
from storage import Store


def test_refuses_relative_database_on_bothost(tmp_path):
    with pytest.raises(RuntimeError, match='relative'):
        resolve_database_path('roleplay.sqlite3', persistent_dir=tmp_path)


def test_refuses_database_outside_persistent_mount(tmp_path):
    with pytest.raises(RuntimeError, match='outside'):
        resolve_database_path('/app/roleplay.sqlite3', persistent_dir=tmp_path)


def test_refuses_silent_empty_database(tmp_path):
    with pytest.raises(RuntimeError, match='not found'):
        resolve_database_path(str(tmp_path / 'roleplay.sqlite3'), persistent_dir=tmp_path)


def test_new_database_requires_explicit_opt_in(tmp_path):
    path = str(tmp_path / 'roleplay.sqlite3')
    assert resolve_database_path(path, persistent_dir=tmp_path, allow_new=True) == path
    db = Store(path)
    db.new_session(1, 'Saved story')
    assert resolve_database_path(path, persistent_dir=tmp_path) == path


def test_missing_active_pointer_restores_existing_story(tmp_path):
    db = Store(str(tmp_path / 'roleplay.sqlite3'))
    sid = db.new_session(22, 'My world')
    db.set_session_field(sid, 'cast', 'Vincent / Iris')
    with db.connect() as conn:
        conn.execute('DELETE FROM activity WHERE user_id=22')
    restored = db.ensure_current_session(22)
    assert restored['id'] == sid
    assert restored['cast'] == 'Vincent / Iris'
    assert len(db.list_sessions()) == 1


def test_missing_persistent_directory_does_not_get_created(tmp_path):
    missing = tmp_path / 'data'
    with pytest.raises(RuntimeError, match='persistent directory is missing'):
        resolve_database_path(str(missing / 'roleplay.sqlite3'), persistent_dir=missing)
    assert not missing.exists()
