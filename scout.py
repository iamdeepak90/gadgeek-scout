import sys
from typing import Any, Dict, List, Tuple, Optional

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
    looks_like_uuid,
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

def match_category(
    entry: Dict[str, str],
    categories: List[Dict[str, Any]],
    category_hint: str = "",
    default_category_id: str = DEFAULT_CATEGORY_UUID,
) -> Tuple[str, str, int]:
    """Return (category_id, category_name, score).

    Uses hint if it matches a category (by UUID or by slug). Otherwise keyword match on
    title+description+content+entry.category. If nothing matches, returns the default
    category UUID.
    """

    # Build lookup maps
    by_id = {str(c.get("id")): c for c in categories if c.get("id")}
    by_slug = {str(c.get("slug")): c for c in categories if c.get("slug")}

    hint = (category_hint or "").strip()
    if hint:
        if looks_like_uuid(hint) and hint in by_id:
            c = by_id[hint]
            return str(c["id"]), c.get("name") or "", 999
        if hint in by_slug:
            c = by_slug[hint]
            return str(c["id"]), c.get("name") or "", 999

    blob = " ".join(
        [
            entry.get("title", ""),
            entry.get("description", ""),
            entry.get("content", ""),
            entry.get("category", ""),
        ]
    )

    best_id: Optional[str] = None
    best_name = ""
    best_score = -1
    best_priority = 10**9

    for c in categories:
        cid = str(c.get("id") or "")
        if not cid:
            continue
        score = keyword_score(blob, c.get("keywords", []))
        priority = int(c.get("priority") or 999)
        if score > best_score or (score == best_score and priority < best_priority):
            best_id = cid
            best_name = c.get("name") or ""
            best_score = score
            best_priority = priority

    if best_id and best_score > 0:
        return best_id, best_name, best_score

    # No match: use default UUID
    return default_category_id, (by_id.get(default_category_id, {}) or {}).get("name", ""), 0

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

    # Per-category cap uses category UUIDs (relation field)
    per_cat_cap = {str(c["id"]): int(c.get("posts_per_scout", 0)) for c in categories if c.get("id")}
    picked_per_cat = {cid: 0 for cid in per_cat_cap.keys()}

    # Ensure default category exists in caps so unmatched items can still be created.
    per_cat_cap.setdefault(DEFAULT_CATEGORY_UUID, 999)
    picked_per_cat.setdefault(DEFAULT_CATEGORY_UUID, 0)

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
