# Tech News Automation (Production)

This project implements your finalized flow:

Discovery (RSS + optional NewsData) → category filter → dedupe → Slack approval → deep research (LangSearch) → Fact Pack (Gemini Flash-Lite) → Long article (Gemini Flash) → Humanization/SEO pack → image selection → publish to Directus.

**No `.env` file** is used. All credentials are in `config.py`.

---

## 1) Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 2) Configure

Edit `config.py`:

- Directus URL + token
- Slack bot token + signing secret + channel
- LangSearch key (recommended)
- Gemini key
- Optional image provider keys

---

## 3) Directus setup

Follow `directus_schema.md` exactly.

---

## 4) Run services

### A) Discovery (Scout)

Long-running:
```bash
python scout.py
```

Or run once (recommended via cron):
```bash
python -c "import scout; scout.run_once()"
```

### B) Slack interactive server

```bash
gunicorn -w 2 -b 0.0.0.0:8000 bot_server:app
```

Configure Slack Interactivity URL to:
`https://YOUR-SERVER/slack/interactions`

### C) Publisher

Long-running:
```bash
python publisher.py
```

Run once:
```bash
python publisher.py --once
```

Process a specific lead:
```bash
python publisher.py --lead-id 123
```

---

## 5) Operational notes (robustness)

- If Slack is misconfigured and `REQUIRE_SLACK_APPROVAL=True`, Scout will still create leads and then auto-queue if Slack message fails.
- Publisher uses a simple file lock: `data/publisher.lock` to avoid duplicate runs.
- Publisher enforces:
  - publish window (Asia/Kolkata)
  - max posts/day
  - minimum minutes between publishes

---

## 6) Content structure guarantees

The writer produces:
- H2: Article Highlights (bullets)
- H2: Hook (120–150 words)
- 4–5 H2 sections (each 100–200 words) with optional H3
- one table
- image figure block
- H2: Sources (URL list)

**No JSON-LD** is generated (per your request).

---

## 7) Troubleshooting

- Check logs in `data/app.log`
- If a lead is stuck, look at `news_leads.last_error` in Directus.
- If extraction fails for many sources, add more RSS feeds or enable Brave fallback.

