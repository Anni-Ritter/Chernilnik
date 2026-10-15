# Before deploying the database safety fix

This patch deliberately refuses to start on BotHost when the configured DB path is relative, outside `/app/data`, or points to a missing DB. It never deletes or migrates existing data automatically.

## 1. Preserve existing data

Stop the bot if possible. Make a backup of the persistent `/app/data` folder using the BotHost file manager. Do **not** delete files or overwrite old databases.

## 2. Locate the existing database

Using the BotHost terminal/file manager, inspect both `/app/data` and `/app` for `roleplay.sqlite3` or similarly named SQLite files. If a database exists only under `/app`, it may disappear on rebuilds. Back it up and copy it to `/app/data/roleplay.sqlite3` while the bot is stopped. The original file should remain intact until you've verified the copy.

If the SQLite database uses WAL and SHM sidecar files, stop the bot before copying so there are no uncommitted changes in the `-wal` file.

## 3. Check environment variables

Set `DATABASE_PATH=/app/data/roleplay.sqlite3` in BotHost. Do not use `DATABASE_PATH=roleplay.sqlite3`. `MEDIA_DIR=/app/data/portraits` for photos.

The patch will **refuse to launch** if `/app/data/roleplay.sqlite3` is missing. This is intentional, so a misconfigured update cannot silently create a blank story.

Only for a genuine *new* installation with no previous stories, temporarily set `ALLOW_NEW_DATABASE=true` for one first start, then remove this flag.

## 4. Existing stories but a new "Свободная история"?

If the `sessions` table still contains stories but the `activity` pointer is missing, the bot now restores the most recent existing story instead of creating a new one. You can always select another via the story menu.

This patch does not restore database files already removed by the hosting platform. If original files are gone, look for a BotHost backup or previous Telegram text/history export.
