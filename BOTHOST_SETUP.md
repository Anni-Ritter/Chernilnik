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
