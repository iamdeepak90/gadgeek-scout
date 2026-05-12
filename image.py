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
    directus_delete,
    generate_image,
    build_image_prompt,
    import_image_to_directus,
    brave_image_search,
)
from urllib.parse import urlencode

LOG = logging.getLogger("image_backfill")

BATCH_SIZE = 50
RUN_INTERVAL = 3600  # 1 hour


def fetch_articles_without_image(limit: int = BATCH_SIZE) -> list:
    """Fetch articles where featured_image is null OR has unsupported mime type (e.g. text/html)."""
    col = articles_collection()

    params_null = urlencode({
        "filter[featured_image][_null]": "true",
        "fields": "id,title,category.name",
        "limit": limit,
        "sort": "-date_created",
    })

    params_invalid = urlencode({
        "filter[featured_image][_nnull]": "true",
        "fields": "id,title,category.name,featured_image.id,featured_image.type",
        "limit": limit * 50,  # fetch more since we filter in Python
        "sort": "-date_created",
    })

    try:
        articles = []

        # 1. Null images
        data = directus_get(f"/items/{col}?{params_null}")
        articles += data.get("data") or []

        # 2. Invalid mime type — filter in Python
        data = directus_get(f"/items/{col}?{params_invalid}")
        for article in (data.get("data") or []):
            img = article.get("featured_image")
            if not isinstance(img, dict):
                continue
            img_type = (img.get("type") or "").lower()
            if img_type.startswith("image/"):
                continue  # valid, skip

            bad_file_id = img.get("id")
            LOG.warning("Article %s has bad image type '%s' (file: %s) — clearing.",
                        article.get("id"), img_type or "unknown", bad_file_id)

            # Delete the bad file from Directus
            if bad_file_id:
                try:
                    directus_delete(f"/files/{bad_file_id}")
                    LOG.info("Deleted bad file: %s", bad_file_id)
                except Exception as exc:
                    LOG.warning("Could not delete bad file %s: %s", bad_file_id, exc)

            # Clear featured_image on the article
            try:
                directus_patch(f"/items/{col}/{article['id']}", {"featured_image": None})
                article.pop("featured_image", None)
                articles.append(article)
            except Exception as exc:
                LOG.error("Could not clear featured_image on %s: %s", article["id"], exc)

        # Deduplicate by id and cap at limit
        seen = set()
        unique = []
        for a in articles:
            if a["id"] not in seen:
                seen.add(a["id"])
                unique.append(a)

        LOG.info("Found %d article(s) needing an image (null + invalid).", len(unique))
        return unique[:limit]

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

        # Step 1: Build prompt (used if Brave search finds nothing)
        try:
            prompt = build_image_prompt(title, category_name)
            LOG.debug("Prompt: %s", prompt)
        except Exception as exc:
            LOG.error("build_image_prompt failed: %s", exc)
            failed += 1
            continue

        # Step 2: Try Brave Image Search for a real photo first
        file_id = None
        brave_img = brave_image_search(title)
        if brave_img and brave_img.get("url"):
            LOG.info("Brave image found — importing to Directus.")
            try:
                file_id = import_image_to_directus(brave_img["url"], title=title)
                if file_id:
                    LOG.info("Brave image imported successfully: %s", file_id)
                else:
                    LOG.warning("Brave image import returned no file_id — will fall back to AI generation.")
            except Exception as exc:
                LOG.warning("Brave image import exception: %s — falling back to AI generation.", exc)
                file_id = None
        else:
            LOG.info("Brave image search found nothing — will use AI generation.")

        # Step 3: AI generation fallback (OpenRouter → Together, handled by common.py)
        if not file_id:
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

            # Step 3: Import AI-generated image to Directus
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