"""
image.py
─────────────────────────────────────────────────────────────────────────────
Runs every hour. Finds Articles in Directus where featured_image is null,
generates an AI image using the existing generate_image() pipeline, uploads
it via import_image_to_directus(), and patches the article record.

Uses all existing helpers from common.py — no duplicate code, no separate
env vars, no separate config. Everything flows through your existing Redis
settings (directus_url, directus_token, together_api_key, openrouter_api_key).

Run:
    python image.py

Dependencies (already in your requirements.txt):
    requests, schedule, redis
"""

import logging
import time
import traceback

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
BATCH_SIZE = 10


# ─────────────────────────────────────────────────────────────────────────────
# DIRECTUS HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def fetch_articles_without_image(limit: int = BATCH_SIZE) -> list:
    """
    Fetch articles where featured_image is null.
    Includes category_name via relational field for image prompt.
    Returns list of dicts: id, title, category.name
    """
    col = articles_collection()
    params = urlencode({
        "filter[featured_image][_null]": "true",
        "fields": "id,title,category.name",
        "limit": limit,
        "sort": "-date_created",
    })
    try:
        url = f"/items/{col}?{params}"
        LOG.debug("Fetching articles: %s", url)
        data = directus_get(url)

        if data is None:
            LOG.error("directus_get returned None. Check directus_url and directus_token.")
            return []

        articles = data.get("data") or []
        LOG.info("Found %d article(s) without featured_image.", len(articles))
        return articles

    except Exception as exc:
        LOG.error("Failed to fetch articles from Directus: %s", exc)
        LOG.debug(traceback.format_exc())
        return []


def extract_category_name(article: dict) -> str:
    """
    Safely extract category name from nested Directus response.
    Handles both dict (relational) and None cases.
    """
    category = article.get("category")
    if isinstance(category, dict):
        return (category.get("name") or "").strip()
    if isinstance(category, str):
        return category.strip()
    return ""


def patch_article_image(article_id: str, file_id: str, alt_text: str) -> bool:
    """
    Patch featured_image and featured_image_alt on the article.
    """
    col = articles_collection()
    try:
        directus_patch(f"/items/{col}/{article_id}", {
            "featured_image": file_id,
            "featured_image_alt": alt_text,
        })
        LOG.info("Article %s updated with image %s.", article_id, file_id)
        return True
    except Exception as exc:
        LOG.error("Failed to patch article %s: %s", article_id, exc)
        LOG.debug(traceback.format_exc())
        return False


# ─────────────────────────────────────────────────────────────────────────────
# MAIN JOB
# ─────────────────────────────────────────────────────────────────────────────

def backfill_images() -> None:
    """
    Hourly job:
      1. Fetch articles without featured_image (including category name)
      2. Generate image via existing generate_image()
      3. Import image via existing import_image_to_directus()
      4. Patch article with file UUID
    """
    LOG.info("=" * 60)
    LOG.info("Image backfill job starting...")

    # Sanity checks
    directus_url = get_setting("directus_url")
    directus_token = get_setting("directus_token")

    if not directus_url:
        LOG.error("directus_url not set in settings. Skipping run.")
        return
    if not directus_token:
        LOG.error("directus_token not set in settings. Skipping run.")
        return

    LOG.info("Directus URL: %s", directus_url)

    articles = fetch_articles_without_image(limit=BATCH_SIZE)

    if not articles:
        LOG.info("No articles need images. Job complete.")
        LOG.info("=" * 60)
        return

    success_count = 0
    fail_count = 0

    for idx, article in enumerate(articles, 1):
        article_id = str(article.get("id", ""))
        title = (article.get("title") or "").strip() or f"Article {article_id}"
        category_name = extract_category_name(article)

        LOG.info(
            "[%d/%d] Processing article %s: %.70s (category: %s)",
            idx, len(articles), article_id, title,
            category_name or "uncategorized",
        )

        # ── Step 1: Build prompt with actual category name ────────────────
        try:
            prompt = build_image_prompt(title, category_name)
            LOG.debug("Image prompt: %s", prompt)
        except Exception as exc:
            LOG.error("build_image_prompt failed for article %s: %s", article_id, exc)
            fail_count += 1
            continue

        # ── Step 2: Generate image ────────────────────────────────────────
        try:
            gen = generate_image(prompt)
        except Exception as exc:
            LOG.error("generate_image raised exception for article %s: %s", article_id, exc)
            LOG.debug(traceback.format_exc())
            fail_count += 1
            time.sleep(2)
            continue

        if not gen:
            LOG.warning("generate_image returned None/empty for article %s. Skipping.", article_id)
            fail_count += 1
            time.sleep(2)
            continue

        image_url = gen.get("url") or gen.get("b64_json") or gen.get("data")
        if not image_url:
            LOG.warning(
                "No usable image URL/data in response for article %s. Keys returned: %s",
                article_id, list(gen.keys()),
            )
            fail_count += 1
            time.sleep(2)
            continue

        LOG.info("Image generated successfully for article %s.", article_id)

        # ── Step 3: Import image to Directus ──────────────────────────────
        try:
            file_id = import_image_to_directus(image_url, title=title)
        except Exception as exc:
            LOG.error("import_image_to_directus raised exception for article %s: %s", article_id, exc)
            LOG.debug(traceback.format_exc())
            fail_count += 1
            time.sleep(2)
            continue

        if not file_id:
            LOG.warning("Directus import returned no file_id for article %s. Skipping.", article_id)
            fail_count += 1
            time.sleep(2)
            continue

        LOG.info("Image imported to Directus — file_id: %s", file_id)

        # ── Step 4: Patch the article ─────────────────────────────────────
        alt_text = f"{title} featured image"
        ok = patch_article_image(article_id, file_id, alt_text)

        if ok:
            success_count += 1
        else:
            fail_count += 1

        # Polite delay between articles
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
    LOG.info("Batch size: %d articles per run", BATCH_SIZE)
    LOG.info("Image model: configured via Settings UI -> model_routes:image")

    # Run immediately on startup, then every hour
    backfill_images()

    schedule.every(1).hours.do(backfill_images)
    LOG.info("Scheduler running. Next run in 1 hour. Ctrl+C to stop.")

    while True:
        schedule.run_pending()
        time.sleep(30)