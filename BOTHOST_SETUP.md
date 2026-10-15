**CURRENT VERSION v0.4**: Read `MIGRATION_v04.md` first. The rest of this file contains historical setup notes for earlier versions.

# Bothost deployment guide

This package has flat top-level files to support Git deployment or direct archive deployment.

## Free hosting (Git repository)

1. Create a GitHub repository and upload the files from this ZIP **to the root**, not inside another folder.
2. Create a new Telegram bot project on https://bothost.ru/ and link your repository + branch.
3. Select Python / aiogram; entry point: `bot.py`. Let Bothost generate Dockerfile.
4. Set the environment variables listed below and deploy.

## Paid hosting (ZIP)

Bothost currently restricts direct ZIP deployment to Basic/Pro plans and a test node. In the profile, enable archive uploads, then upload this ZIP through the dashboard.

## Environment variables

- `BOT_TOKEN`: token provided by BotFather (may be populated automatically by Bothost)
- `OPENAI_API_KEY`: your OpenAI API project key
- `OWNER_TELEGRAM_ID`: your *numeric Telegram user id*, not username
- `DATABASE_PATH=/app/data/roleplay.sqlite3` (**critical for persistence**)
- `OPENAI_MODEL=gpt-6-luna`
- `MEMORY_MODEL=gpt-6-luna`
- `AUTO_MEMORY=true`
- `MAX_OUTPUT_TOKENS=1800`

Never upload `.env`, API keys, or the SQLite database into GitHub. A starter `.env.example` contains placeholders only.

## Verify

When the deploy is healthy, visit the bot in Telegram and send `/start`, `/new Test`, `/status`, and a brief roleplay post. You need OpenAI API credits, which are separate from ChatGPT subscriptions.

The SQLite database is created automatically on first startup in `/app/data`, retained across deploys. Export stories with `/export` and keep backups.

If the bot is not responding: check deploy logs for missing env variables, Python dependency errors, or insufficient OpenAI API credits.


## Update existing BotHost Pro installation to v0.2

1. Upload this ZIP as an update to **the existing bot**, not as a new bot. Keep the same environment variables and the database path `/app/data/roleplay.sqlite3`.
2. Restart/redeploy from the BotHost panel. Your existing SQLite tables are preserved; an `openings` table is created automatically.
3. Start a fresh story with `/new`, set `/cast` and optional `/time`, then press the **Start story** Telegram button or send `/begin`. The bot speaks first.
4. Previously started stories are unchanged; `/begin` refuses to overwrite their history.


## Обновление существующего Bothost Pro бота до v0.3

**НЕ создавай нового бота и НЕ удаляй папку `/app/data`**. Залей ZIP версии 0.3 как обновление в существующий проект на Bothost Pro; главный файл — `bot.py`. Сохрани прежние `BOT_TOKEN`, `OPENAI_API_KEY` и `OWNER_TELEGRAM_ID`. Переменные `DATABASE_PATH=/app/data/roleplay.sqlite3` и `MEDIA_DIR=/app/data/portraits` должны указывать на постоянный раздел.

Необязательно: `VISION_MODEL=gpt-6-luna` (по умолчанию и так Luna). Обработка фото не требует отдельного API-ключа, но расходует токены OpenAI **один раз при загрузке или повторном анализе**.

После перезапуска отправь `/start`, нажми `👤 Персонажи` → `➕ Новый персонаж` → укажи имя и роль → `➕ Фото`; отправь изображение. Дождись описания внешности; открой карточку и проверь его. Проверяй логи если бот пишет «анализ не удался».

Схема SQLite расширяется автоматически: старые таблицы `sessions`, `turns`, `facts`, `openings` не пересоздаются; добавляются `characters` и `character_photos`. Ролевые посты и память останутся на месте **при условии, что путь к существующей SQLite БД остался тем же**. Сделай резервную копию `/app/data` перед обновлением.


## v0.3.2 IMPORTANT

Before upgrading, read `DB_RECOVERY.md` and back up `/app/data`. The bot now fails with a clear error instead of creating a new database if an existing BotHost SQLite file cannot be found. Set `DATABASE_PATH=/app/data/roleplay.sqlite3` explicitly in the BotHost environment.


## Обновление v0.5 (через GitHub / BotHost Pro)

1. Сначала в Telegram отправьте `/backup` и скачайте базу. Убедитесь, что `DATABASE_PATH=/app/data/roleplay.sqlite3`.
2. Скопируйте все файлы патча в **корень** проекта, включая новый `generation_models.py`. Не удаляйте `db_safety.py`, SQLite и `/app/data/portraits`.
3. В переменных окружения BotHost добавьте `POLZA_API_KEY`. Сохраните прежние `OPENAI_API_KEY`, `MEMORY_MODEL` и `VISION_MODEL`.
4. Обновите проект и перезапустите процесс, затем откройте Telegram: **Меню → Модель текста** либо `/model`.
5. Убедитесь, что выбрано `polza · google/gemini-3.7-flash` и попробуйте **новую тестовую** историю. Старые истории не будут переписаны.

**Важно:** `GENERATION_MODEL_CONFIG` по умолчанию находится рядом с SQLite: `/app/data/generation_model.json`. Это отдельный маленький файл с ID выбранной модели, **не** ключом. SQLite-схема не изменяется. Если после перезапуска модель сбросилась, проверьте сохранность этого файла и доступ на запись в `/app/data`.
