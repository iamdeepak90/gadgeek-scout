"""
interlink_backfill.py — One-time backfill: find published articles without
internal links and inject interlinks into them using the same pipeline
as the publisher.

Usage:
    python interlink_backfill.py

Runs once and exits. Safe to re-run — skips articles that already have
internal links. Processes one article at a time with a delay to avoid
hammering Directus and OpenRouter.
"""

import logging
import time
import traceback
from urllib.parse import urlencode

from common import (
    setup_logging,
    init_db,
    get_setting,
    articles_collection,
    directus_get,
    directus_patch,
    _extract_keywords_llm,
    find_related_articles,
    inject_interlinks,
)

LOG = logging.getLogger("interlink_backfill")

BATCH_SIZE = 50       # articles fetched per page
DELAY_BETWEEN = 5     # seconds between each article (be nice to APIs)
OFFSET_START = 0      # change to resume from a specific offset


def has_internal_links(content: str) -> bool:
    """Check if article already has at least one internal <a href="/..."> link."""
    import re
    # Internal links start with / (not http)
    return bool(re.search(r'<a\s[^>]*href=["\s]/', content, re.IGNORECASE))


def fetch_articles_batch(offset: int, limit: int) -> list:
    """Fetch a batch of published articles with content and category slug."""
    col = articles_collection()
    params = urlencode({
        "filter[status][_eq]": "published",
        "fields": "id,title,content,category.slug",
        "limit": limit,
        "offset": offset,
        "sort": "-date_created",
    })
    try:
        data = directus_get(f"/items/{col}?{params}")
        return data.get("data") or []
    except Exception as e:
        LOG.error("Failed to fetch articles at offset %d: %s", offset, e)
        return []


def patch_article_content(article_id: str, content: str) -> bool:
    """Patch the content field of an article."""
    col = articles_collection()
    try:
        directus_patch(f"/items/{col}/{article_id}", {"content": content})
        return True
    except Exception as e:
        LOG.error("Failed to patch article %s: %s", article_id, e)
        return False


def process_article(article: dict) -> str:
    """Process a single article. Returns status string for logging."""
    article_id = str(article.get("id") or "")
    title = (article.get("title") or "").strip()
    content = (article.get("content") or "").strip()

    if not article_id or not content:
        return "skip:empty"

    # Skip if already has internal links
    if has_internal_links(content):
        return "skip:already_linked"

    # Step 1: Extract keywords via LLM
    keywords = _extract_keywords_llm(content)
    if not keywords:
        return "skip:no_keywords"

    LOG.info("  Keywords: %s", keywords)

    # Step 2: Find related articles in Directus
    related = find_related_articles(keywords, exclude_title=title, max_results=5)
    if not related:
        return "skip:no_related"

    LOG.info("  Related found: %d — %s", len(related), [r["title"] for r in related])

    # Step 3: Inject interlinks into content
    new_content = inject_interlinks(content, related)
    if new_content == content:
        return "skip:nothing_injected"

    # Step 4: Patch article in Directus
    if patch_article_content(article_id, new_content):
        return f"ok:{len(related)}_links"
    else:
        return "fail:patch_error"


def run_backfill() -> None:
    LOG.info("=" * 60)
    LOG.info("Interlink backfill starting...")

    if not get_setting("directus_url") or not get_setting("directus_token"):
        LOG.error("directus_url or directus_token not set. Aborting.")
        return

    if not get_setting("openrouter_api_key"):
        LOG.error("openrouter_api_key not set. Aborting.")
        return

    offset = OFFSET_START
    total_processed = 0
    total_updated = 0
    total_skipped = 0
    total_failed = 0

    while True:
        LOG.info("Fetching articles offset=%d limit=%d...", offset, BATCH_SIZE)
        articles = fetch_articles_batch(offset, BATCH_SIZE)

        if not articles:
            LOG.info("No more articles. Done.")
            break

        for idx, article in enumerate(articles, 1):
            article_id = str(article.get("id") or "")
            title = (article.get("title") or "")[:80]

            LOG.info(
                "[%d/%d | total:%d] %s",
                idx, len(articles), offset + idx, title
            )

            try:
                status = process_article(article)
            except Exception as e:
                LOG.error("Unexpected error for article %s: %s", article_id, e)
                LOG.debug(traceback.format_exc())
                status = "fail:exception"

            LOG.info("  → %s", status)

            if status.startswith("ok"):
                total_updated += 1
            elif status.startswith("skip"):
                total_skipped += 1
            else:
                total_failed += 1

            total_processed += 1

            # Delay between articles to avoid rate limiting
            time.sleep(DELAY_BETWEEN)

        offset += BATCH_SIZE

        # If we got fewer articles than batch size, we've reached the end
        if len(articles) < BATCH_SIZE:
            LOG.info("Last batch. Done.")
            break

    LOG.info("=" * 60)
    LOG.info(
        "Backfill complete — processed: %d | updated: %d | skipped: %d | failed: %d",
        total_processed, total_updated, total_skipped, total_failed,
    )
    LOG.info("=" * 60)


if __name__ == "__main__":
    setup_logging("INFO")
    init_db()
    run_backfill()