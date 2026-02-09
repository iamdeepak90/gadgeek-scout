import time
from typing import Any, Dict, List, Tuple

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
    DEFAULT_CATEGORY_UUID,
    get_setting,
)

def _normalize(s: str) -> str:
    return (s or "").lower()

def _score_category(blob: str, keywords: List[str]) -> int:
    if not blob or not keywords:
        return 0
    score = 0
    for kw in keywords:
        k = (kw or "").strip().lower()
        if not k:
            continue
        # substring match; you can tighten to word-boundary if you prefer
        if k in blob:
            score += 1
    return score

def pick_category(title: str, description: str, content: str, feed_cat: str) -> Tuple[str, str]:
    """
    Returns (category_id, category_name). Falls back to DEFAULT_CATEGORY_UUID if no match.
    """
    cats = get_categories()
    blob = " ".join([title, description, content, feed_cat])
    blob_l = _normalize(blob)

    best = None
    for c in cats:
        cid = c.get("id") or ""
        name = c.get("name") or ""
        kw = c.get("keywords") or []
        score = _score_category(blob_l, kw)
        if score <= 0:
            continue
        item = (score, -int(c.get("priority") or 999), cid, name)
        if best is None or item > best:
            best = item

    if best:
        _, _, cid, name = best
        return cid, name

    # fallback
    return DEFAULT_CATEGORY_UUID, "Uncategorized"

def scout_once() -> int:
    init_db()
    feeds = [f for f in list_feeds() if f.get("enabled")]
    if not feeds:
        LOG.info("No enabled feeds configured.")
        return 0

    created = 0
    for feed in feeds:
        url = feed.get("url") or ""
        if not url:
            continue
        try:
            parsed = parse_feed(url)
        except Exception as e:
            LOG.warning("Failed to fetch feed %s: %s", url, e)
            continue

        selectors = {
            "title_key": feed.get("title_key") or None,
            "description_key": feed.get("description_key") or None,
            "content_key": feed.get("content_key") or None,
            "category_key": feed.get("category_key") or None,
        }

        entries = getattr(parsed, "entries", []) or []
        for entry in entries[:30]:
            fields = extract_entry_fields(entry, selectors)
            title = fields.get("title", "").strip()
            link = fields.get("link", "").strip()
            desc = fields.get("description", "").strip()
            content = fields.get("content", "").strip()
            feed_cat = fields.get("category", "").strip() or (feed.get("category_hint") or "")

            if not title or not link:
                continue
            try:
                if lead_exists_by_url(link):
                    continue
            except Exception as e:
                LOG.warning("Directus lead_exists failed; continuing: %s", e)

            cat_id, cat_name = pick_category(title, desc, content, feed_cat)

            try:
                lead_id = create_lead(title=title, source_url=link, category_id=cat_id)
                slack_post_lead(title=title, category_name=cat_name, lead_id=lead_id)
                created += 1
            except Exception as e:
                LOG.exception("Failed to create/post lead for %s: %s", link, e)

    return created

def main():
    setup_logging()
    init_db()
    interval_min = int(float(get_setting("scout_interval_minutes", "30") or "30"))
    sleep_s = max(60, interval_min * 60)
    while True:
        try:
            created = scout_once()
            LOG.info("Scout completed. Created %d leads.", created)
        except Exception as e:
            LOG.exception("Scout loop error: %s", e)
        time.sleep(sleep_s)

if __name__ == "__main__":
    main()
