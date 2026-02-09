import sys
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
)

def keyword_score(text: str, keywords: List[str]) -> int:
    t = (text or "").lower()
    score = 0
    for kw in keywords:
        k = (kw or "").strip().lower()
        if not k:
            continue
        # simple contains
        if k in t:
            score += 1
    return score

def match_category(entry: Dict[str, str], categories: List[Dict[str, Any]], category_hint: str = "") -> Tuple[str, str, int]:
    """
    Returns (category_id, category_name, score).
    Uses hint if valid; otherwise keyword match on title+description+content.
    """
    if category_hint:
        for c in categories:
            if c["slug"] == category_hint:
                return str(c.get("id") or ""), c["name"], 999

    blob = " ".join([entry.get("title",""), entry.get("description",""), entry.get("content",""), entry.get("category","")])
    best = None
    for c in categories:
        score = keyword_score(blob, c.get("keywords", []))
        if best is None or score > best[2] or (score == best[2] and c["priority"] < best[3]):
            best = (str(c.get("id") or ""), c["name"], score, c["priority"])
    if not best:
        return "", "", 0
    return best[0], best[1], best[2]

def scout_once() -> int:
    init_db()
    feeds = [f for f in list_feeds() if f.get("enabled")]
    if not feeds:
        LOG.error("No RSS feeds configured. Go to /settings -> RSS Feeds.")
        return 0

    categories = get_categories()
    if not categories:
        LOG.error("No enabled categories found in Directus. Ensure categories collection has enabled=true and posts_per_scout>0.")
        return 0

    per_cat_cap = {str(c.get("id") or ""): int(c.get("posts_per_scout", 0)) for c in categories}
    picked_per_cat = {str(c.get("id") or ""): 0 for c in categories}

    created = 0

    # Process feeds newest-first; feedparser entries are usually newest-first already.
    for feed_cfg in feeds:
        url = feed_cfg["url"]
        category_hint = (feed_cfg.get("category_hint") or "").strip()

        try:
            parsed = parse_feed(url)
        except Exception as e:
            LOG.warning("Failed to parse feed %s: %s", url, e)
            continue

        entries = parsed.entries or []
        for ent in entries[:50]:
            fields = extract_entry_fields(ent, feed_cfg)
            title = fields.get("title") or ""
            link = fields.get("link") or ""
            if not title or not link:
                continue

            cat_id, cat_name, score = match_category(fields, categories, category_hint=category_hint)

            # If no match found, fall back to default category UUID
            if not cat_id:
                cat_id = DEFAULT_CATEGORY_UUID
                # Try to resolve the default category name
                for c in categories:
                    if str(c.get("id") or "") == cat_id:
                        cat_name = c.get("name") or "General"
                        break
                if not cat_name:
                    cat_name = "General"

            if per_cat_cap.get(cat_id, 0) <= 0:
                continue
            if picked_per_cat.get(cat_id, 0) >= per_cat_cap.get(cat_id, 0):
                continue

            # Dedupe only against news_leads.source_url
            try:
                if lead_exists_by_url(link):
                    continue
            except Exception as e:
                LOG.error("Directus dedupe check failed: %s", e)
                continue

            try:
                lead_id = create_lead(title=title, source_url=link, category_id=cat_id)
            except Exception as e:
                LOG.error("Failed to create lead in Directus: %s", e)
                continue

            try:
                slack_post_lead(title=title, category_name=cat_name, lead_id=lead_id)
            except Exception as e:
                LOG.error("Failed to post lead %s to Slack: %s", lead_id, e)
                # lead still exists; skip posting. You can approve in Directus.
            picked_per_cat[cat_id] = picked_per_cat.get(cat_id, 0) + 1
            created += 1

    LOG.info("Scout completed. Created %s leads.", created)
    return created

def main():
    setup_logging()
    try:
        scout_once()
    except Exception as e:
        LOG.exception("Scout failed: %s", e)
        sys.exit(1)

if __name__ == "__main__":
    main()
