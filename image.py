"""
image_backfill.py
─────────────────────────────────────────────────────────────────────────────
Runs every hour. Finds Articles in Directus where featured_image is null,
generates an AI image using the existing generate_image() pipeline, uploads
it via import_image_to_directus(), and patches the article record.

Uses all existing helpers from common.py — no duplicate code, no separate
env vars, no separate config. Everything flows through your existing Redis
settings (directus_url, directus_token, together_api_key, openrouter_api_key).

Run:
    python image_backfill.py

Dependencies (already in your requirements.txt):
    requests, schedule, redis
"""

import logging
import time

import schedule

from common import (
    setup_logging,
    init_db,
    get_setting,
    articles_collection,
    directus_get,
    directus_patch,
    generate_image,
    import_image_to_directus,
    build_image_prompt,
)
from urllib.parse import urlencode

LOG = logging.getLogger("image_backfill")

# How many articles to process per hourly run.
# Keep low to avoid hammering Together/OpenRouter image APIs.
BATCH_SIZE = 10


# ─────────────────────────────────────────────────────────────────────────────
# DIRECTUS HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def fetch_articles_without_image(limit: int = BATCH_SIZE) -> list:
    """
    Fetch articles where featured_image is null.
    Returns list of dicts with at minimum: id, title.
    Uses existing directus_get() from common.py.
    """
    col = articles_collection()
    params = urlencode({
        "filter[featured_image][_null]": "true",
        "fields":                        "id,title",
        "limit":                         limit,
        "sort":                          "-date_created",
    })
    try:
        data = directus_get(f"/items/{col}?{params}")
        articles = data.get("data") or []
        LOG.info("Found %d article(s) without featured_image.", len(articles))
        return articles
    except Exception as exc:
        LOG.error("Failed to fetch articles from Directus: %s", exc)
        return []


def patch_article_image(article_id: str, file_id: str, alt_text: str) -> bool:
    """
    Patch featured_image and featured_image_alt on the article.
    Uses existing directus_patch() from common.py.
    """
    col = articles_collection()
    try:
        directus_patch(f"/items/{col}/{article_id}", {
            "featured_image":     file_id,
            "featured_image_alt": alt_text,
        })
        LOG.info("Article %s updated with image %s.", article_id, file_id)
        return True
    except Exception as exc:
        LOG.error("Failed to patch article %s: %s", article_id, exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# MAIN JOB
# ─────────────────────────────────────────────────────────────────────────────

def backfill_images() -> None:
    """
    Hourly job:
      1. Fetch articles without featured_image
      2. Generate image via existing generate_image() — uses your model route
         settings (Together or OpenRouter, whatever is configured in Redis)
      3. Import image via existing import_image_to_directus()
      4. Patch article with file UUID
    """
    LOG.info("=" * 60)
    LOG.info("Image backfill job starting...")

    # Sanity check — Directus must be configured
    if not get_setting("directus_url") or not get_setting("directus_token"):
        LOG.error("directus_url or directus_token not set in settings. Skipping run.")
        return

    articles = fetch_articles_without_image(limit=BATCH_SIZE)

    if not articles:
        LOG.info("No articles need images. Job complete.")
        LOG.info("=" * 60)
        return

    success_count = 0
    fail_count    = 0

    for article in articles:
        article_id = str(article.get("id", ""))
        title      = (article.get("title") or "").strip() or f"Article {article_id}"

        LOG.info("Processing [%s] %s", article_id, title[:70])

        # ── Step 1: Build prompt using existing helper ────────────────────────
        # build_image_prompt() is already in common.py.
        # It takes (title, category_name). We pass empty string for category
        # since we only fetched id + title to keep the query light.
        prompt = build_image_prompt(title, "")

        # ── Step 2: Generate image via existing routing ───────────────────────
        # generate_image() reads model_routes:image from Redis, so it uses
        # whatever provider + model you've configured in your Settings UI
        # (Together FLUX.1-schnell by default).
        gen = generate_image(prompt)

        if not gen or not gen.get("url"):
            LOG.warning("Image generation returned nothing for article %s. Skipping.", article_id)
            fail_count += 1
            time.sleep(2)
            continue

        # ── Step 3: Import image to Directus file library ─────────────────────
        # import_image_to_directus() handles both HTTP URLs and base64 data URIs,
        # so it works regardless of whether Together returns url or b64_json.
        file_id = import_image_to_directus(gen["url"], title=title)

        if not file_id:
            LOG.warning("Directus import failed for article %s. Skipping.", article_id)
            fail_count += 1
            time.sleep(2)
            continue

        # ── Step 4: Patch the article ─────────────────────────────────────────
        alt_text = f"{title} featured image"
        ok = patch_article_image(article_id, file_id, alt_text)

        if ok:
            success_count += 1
        else:
            fail_count += 1

        # Polite delay — avoids hammering Together/OpenRouter rate limits
        time.sleep(3)

    LOG.info(
        "Backfill complete — success: %d | failed: %d | total: %d",
        success_count, fail_count, len(articles),
    )
    LOG.info("=" * 60)


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    setup_logging("INFO")
    init_db()

    LOG.info("Image backfill worker starting...")
    LOG.info("Batch size    : %d articles per run", BATCH_SIZE)
    LOG.info("Image model   : configured via Settings UI → model_routes:image")

    # Run immediately on startup, then every hour
    backfill_images()

    schedule.every(1).hours.do(backfill_images)
    LOG.info("Scheduler running. Next run in 1 hour. Ctrl+C to stop.")

    while True:
        schedule.run_pending()
        time.sleep(30)