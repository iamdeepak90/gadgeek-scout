"""
image.py — Hourly backfill: generate AI images for articles missing featured_image
"""

import logging
import time
import traceback

from common import (
    setup_logging,
    init_db,
    get_setting,
    articles_collection,
    directus_get,
    directus_patch,
    generate_image,
    build_image_prompt,
    import_image_to_directus,
)
from urllib.parse import urlencode

LOG = logging.getLogger("image_backfill")

BATCH_SIZE = 10
RUN_INTERVAL = 3600  # 1 hour


def fetch_articles_without_image(limit: int = BATCH_SIZE) -> list:
    """Fetch articles where featured_image is null, including category name."""
    col = articles_collection()
    params = urlencode({
        "filter[featured_image][_null]": "true",
        "fields": "id,title,category.name",
        "limit": limit,
        "sort": "-date_created",
    })
    try:
        data = directus_get(f"/items/{col}?{params}")
        if data is None:
            LOG.error("directus_get returned None — check directus_url and directus_token.")
            return []
        articles = data.get("data") or []
        LOG.info("Found %d article(s) without featured_image.", len(articles))
        return articles
    except Exception as exc:
        LOG.error("Failed to fetch articles: %s", exc)
        LOG.debug(traceback.format_exc())
        return []


def extract_category_name(article: dict) -> str:
    """Safely extract category name from nested Directus response."""
    category = article.get("category")
    if isinstance(category, dict):
        return (category.get("name") or "").strip()
    if isinstance(category, str):
        return category.strip()
    return ""


def patch_article_image(article_id: str, file_id: str, alt_text: str) -> bool:
    """Patch featured_image on the article."""
    col = articles_collection()
    try:
        directus_patch(f"/items/{col}/{article_id}", {
            "featured_image": file_id,
            "featured_image_alt": alt_text,
        })
        LOG.info("Article %s patched with image %s.", article_id, file_id)
        return True
    except Exception as exc:
        LOG.error("Failed to patch article %s: %s", article_id, exc)
        LOG.debug(traceback.format_exc())
        return False


def backfill_images() -> None:
    """Main job: find articles without images → generate → upload → patch."""
    LOG.info("=" * 60)
    LOG.info("Image backfill job starting...")

    if not get_setting("directus_url") or not get_setting("directus_token"):
        LOG.error("directus_url or directus_token not set. Skipping.")
        return

    articles = fetch_articles_without_image(limit=BATCH_SIZE)
    if not articles:
        LOG.info("No articles need images. Done.")
        LOG.info("=" * 60)
        return

    success = 0
    failed = 0

    for idx, article in enumerate(articles, 1):
        article_id = str(article.get("id", ""))
        title = (article.get("title") or "").strip() or f"Article {article_id}"
        category_name = extract_category_name(article)

        LOG.info(
            "[%d/%d] %s (category: %s)",
            idx, len(articles), title[:80],
            category_name or "uncategorized",
        )

        # Step 1: Build prompt
        try:
            prompt = build_image_prompt(title, category_name)
            LOG.debug("Prompt: %s", prompt)
        except Exception as exc:
            LOG.error("build_image_prompt failed: %s", exc)
            failed += 1
            continue

        # Step 2: Generate image (OpenRouter → Together fallback, handled by common.py)
        try:
            gen = generate_image(prompt)
        except Exception as exc:
            LOG.error("generate_image exception: %s", exc)
            LOG.debug(traceback.format_exc())
            failed += 1
            time.sleep(2)
            continue

        if not gen:
            LOG.warning("Image generation returned nothing. Skipping.")
            failed += 1
            time.sleep(2)
            continue

        image_url = gen.get("url") or gen.get("b64_json") or gen.get("data")
        if not image_url:
            LOG.warning("No usable image data. Response keys: %s", list(gen.keys()))
            failed += 1
            time.sleep(2)
            continue

        LOG.info("Image generated successfully.")

        # Step 3: Import to Directus
        try:
            file_id = import_image_to_directus(image_url, title=title)
        except Exception as exc:
            LOG.error("import_image_to_directus exception: %s", exc)
            LOG.debug(traceback.format_exc())
            failed += 1
            time.sleep(2)
            continue

        if not file_id:
            LOG.warning("Directus import returned no file_id. Skipping.")
            failed += 1
            time.sleep(2)
            continue

        LOG.info("Uploaded to Directus — file_id: %s", file_id)

        # Step 4: Patch article
        alt_text = f"{title} featured image"
        if patch_article_image(article_id, file_id, alt_text):
            success += 1
        else:
            failed += 1

        time.sleep(3)

    LOG.info("Done — success: %d | failed: %d | total: %d", success, failed, len(articles))
    LOG.info("=" * 60)


if __name__ == "__main__":
    setup_logging("INFO")
    init_db()

    LOG.info("Image backfill worker starting (batch: %d, interval: %ds)", BATCH_SIZE, RUN_INTERVAL)

    while True:
        try:
            backfill_images()
        except Exception as exc:
            LOG.error("Backfill job crashed: %s", exc)
            LOG.debug(traceback.format_exc())

        LOG.info("Next run in %d seconds.", RUN_INTERVAL)
        time.sleep(RUN_INTERVAL)