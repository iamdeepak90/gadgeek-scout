# Gadgeek Tech News Automation (Minimal + Settings UI)

This project runs a simple pipeline:

1) **Scout** reads RSS feeds (configured in `/settings`) and creates leads in Directus (`news_leads`).
2) Leads are posted to Slack **without URL** (title + category).
3) You approve/urgent/reject in Slack.
4) **Publisher** publishes one approved lead every `publish_interval_minutes`.
5) **Urgent** publishes immediately on click (Slack message updates only after publish).

## Settings UI

Open:

- `https://bot.gadgeek.in/settings`

Basic auth:

- user: `settings@gadgeek.in`
- pass: `HelloGG@$44`

All configuration is stored in a local SQLite DB at:

- `data/settings.db`

> Secrets are never shown back on the UI. If configured, you'll see a "configured" badge.

## Run

Install:

```bash
pip install -r requirements.txt
```

Run the web server:

```bash
gunicorn -w 2 -b 0.0.0.0:8000 bot_server:app
```

Run scout (cron every 30 minutes recommended):

```bash
python scout.py
```

Run publisher (as a service):

```bash
python publisher.py
```

## Slack

Set Interactivity URL to:

- `https://bot.gadgeek.in/slack/interactions`

## Directus collections (minimum)

See `directus_schema.md` for the recommended collections and fields.

