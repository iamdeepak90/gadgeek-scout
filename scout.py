import sys
import time
from typing import Any, Dict, List

from common import (
    LOG,
    setup_logging,
    init_db,
    list_feeds,
    parse_feed,
    extract_entry_fields,
    lead_exists_by_url,
    create_lead,
    slack_post_lead,
    get_categories,
    get_setting,
    DEFAULT_CATEGORY_UUID,
)

# Latest entries per feed to check
ENTRIES_PER_FEED = 25


def scout_once() -> int:
    """Scan RSS feeds, create leads, and post to Slack with category dropdown.

    Category is NOT decided here — the user picks it in Slack before approving.
    Leads are created with DEFAULT_CATEGORY_UUID as a placeholder.
    """
    init_db()
    feeds = [f for f in list_feeds() if f.get("enabled")]
    if not feeds:
        LOG.error("No RSS feeds configured. Go to /settings -> RSS Feeds.")
        return 0

    categories = get_categories()
    if not categories:
        LOG.error("No enabled categories found in Directus.")
        return 0

    created = 0

    for feed_cfg in feeds:
        url = feed_cfg["url"]

        try:
            parsed = parse_feed(url)
        except Exception as e:
            LOG.warning("Failed to parse feed %s: %s", url, e)
            continue

        entries = parsed.entries or []

        for ent in entries[:ENTRIES_PER_FEED]:
            fields = extract_entry_fields(ent, feed_cfg)
            title = (fields.get("title") or "").strip()
            link = (fields.get("link") or "").strip()

            if not title or not link:
                continue

            # Dedupe by source URL
            try:
                if lead_exists_by_url(link):
                    continue
            except Exception as e:
                LOG.error("Directus dedupe check failed: %s", e)
                continue

            # Create lead with default category placeholder.
            # The real category is selected in Slack and saved on approve/urgent.
            try:
                lead_id = create_lead(
                    title=title,
                    source_url=link,
                    category_id=DEFAULT_CATEGORY_UUID,
                )
            except Exception as e:
                LOG.error("Failed to create lead in Directus: %s", e)
                continue

            # Post to Slack with category dropdown
            try:
                slack_post_lead(
                    title=title,
                    source_url=link,
                    lead_id=lead_id,
                    categories=categories,
                )
            except Exception as e:
                LOG.error("Failed to post lead %s to Slack: %s", lead_id, e)

            created += 1

    LOG.info("Scout completed. Created %d leads.", created)
    return created


def _parse_interval_minutes() -> int:
    raw = get_setting("scout_interval_minutes", "60")
    try:
        return int(float(raw or "60"))
    except Exception:
        return 20


def _scout_loop() -> None:
    interval_min = _parse_interval_minutes()
    sleep_s = max(30, interval_min * 60)
    LOG.info("Scout loop started. Will run every %s minutes.", interval_min)

    while True:
        try:
            scout_once()
        except Exception as e:
            LOG.exception("Scout loop error: %s", e)
        time.sleep(sleep_s)


def main() -> None:
    setup_logging()
    init_db()
    _scout_loop()


if __name__ == "__main__":
    main()