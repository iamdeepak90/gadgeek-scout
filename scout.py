"""
scout.py — Discovery service (RSS + optional NewsData), category filter, dedupe, Slack notify.

Run this on a schedule (cron/systemd) or as a long-running loop.

Flow:
- Discover candidates (RSS + optional NewsData)
- Classify into selected categories
- Dedupe (local + Directus Articles + Directus leads)
- Create lead in Directus with status pending (or queued if Slack approval disabled)
- Send approval message to Slack
"""

from __future__ import annotations

import time
import traceback
from typing import Any, Dict, List, Tuple

import config
from common import (
    log, state,
    fetch_rss_entries, classify_item,
    canonicalize_url, compute_fingerprint, domain_of,
    directus_item_exists_by_filters, directus_create_lead, directus_update_lead,
    directus_find_items,
    slack_post_candidate,
    newsdata_discover,
    utcnow_iso,
)


def _priority_boost(category_slug: str) -> int:
    """Boost score by configured category priority.

    priority: 1 highest, 3 lowest.
    """
    p = int(config.FEATURED_CATEGORIES.get(category_slug, {}).get("priority", 3))
    return {1: 30, 2: 15, 3: 0}.get(p, 0)


def _final_score(category_slug: str, match_score: int) -> int:
    return int(match_score) + _priority_boost(category_slug)


def already_seen(title: str, url: str) -> bool:
    fp = compute_fingerprint(title, url)
    if state.has_fp(fp):
        return True

    # Check leads (in case it was discovered before but not published)
    try:
        if directus_item_exists_by_filters(config.LEADS_COLLECTION, {
            config.LEAD_F_FINGERPRINT: {"_eq": fp}
        }):
            state.put_fp(fp, "lead_exists")
            return True
    except Exception as e:
        log.warning(f"Directus lead-exists check failed: {e}")

    # Check articles (published already)
    try:
        if directus_item_exists_by_filters(config.ARTICLES_COLLECTION, {
            config.ART_F_FINGERPRINT: {"_eq": fp}
        }):
            state.put_fp(fp, "article_exists")
            return True
    except Exception as e:
        log.warning(f"Directus article-exists check failed: {e}")

    return False


def create_lead(item: Dict[str,Any], category_slug: str, score: int, matched_keywords: List[str], status: str) -> Any:
    title = item["title"]
    url = canonicalize_url(item["url"])
    fp = compute_fingerprint(title, url)

    lead = {
        config.LEAD_F_TITLE: title,
        config.LEAD_F_SOURCE_URL: url,
        config.LEAD_F_SOURCE_DOMAIN: domain_of(url),
        config.LEAD_F_CATEGORY: category_slug,
        config.LEAD_F_STATUS: status,
        # Always stamp discovery time; RSS timestamps can be missing or inconsistent.
        config.LEAD_F_DISCOVERED_AT: item.get("discovered_at") or utcnow_iso(),
        config.LEAD_F_PUBLISHED_AT: item.get("published_raw") or "",
        config.LEAD_F_FINGERPRINT: fp,
        config.LEAD_F_MATCHED_KEYWORDS: matched_keywords,
        config.LEAD_F_PRIORITY: int(config.FEATURED_CATEGORIES.get(category_slug, {}).get("priority", 3)),
    }
    created = directus_create_lead(lead)
    lead_id = created.get("id") if isinstance(created, dict) else created
    state.put_fp(fp, "lead_created")
    return lead_id, fp


def discover_candidates() -> List[Dict[str,Any]]:
    items: List[Dict[str,Any]] = []

    if config.ENABLE_RSS_DISCOVERY:
        for feed_url in config.RSS_FEEDS:
            try:
                for e in fetch_rss_entries(feed_url, limit=50):
                    if not e.get("title") or not e.get("url"):
                        continue
                    e["discovered_at"] = None
                    items.append(e)
            except Exception as e:
                log.warning(f"RSS fetch failed for {feed_url}: {e}")

    if config.ENABLE_NEWSDATA_DISCOVERY:
        # Use a few broad queries, then category filter will decide. Keep very cheap.
        queries = ["smartphone", "laptop", "AI", "cybersecurity", "gaming", "chip", "smart home"]
        for q in queries:
            try:
                for r in newsdata_discover(q, language="en"):
                    if not r.get("title") or not r.get("url"):
                        continue
                    r["source_feed"] = "newsdata.io"
                    r["discovered_at"] = None
                    items.append(r)
            except Exception as e:
                log.warning(f"NewsData discover failed for '{q}': {e}")

    # Basic URL canonicalization
    for it in items:
        it["url"] = canonicalize_url(it.get("url",""))

    # Remove empty urls
    items = [it for it in items if it.get("url")]

    # De-dup within the batch
    seen = set()
    deduped = []
    for it in items:
        k = it["url"]
        if k in seen:
            continue
        seen.add(k)
        deduped.append(it)
    return deduped


def run_once() -> int:
    """Discover, create leads, and post a limited number to Slack.

    Important production behaviors:
    - Hard cap Slack posts per run (prevents 150+ messages).
    - Per-category and per-domain caps (prevents single-category spam).
    - Priority-aware ordering using FEATURED_CATEGORIES[slug]['priority'].
    """

    # Limits (defaults if not present)
    max_new_leads = getattr(config, "SCOUT_MAX_NEW_LEADS_PER_RUN", 80)
    max_slack = getattr(config, "SCOUT_MAX_SLACK_PER_RUN", 10)
    max_slack_per_cat = getattr(config, "SCOUT_MAX_SLACK_PER_CATEGORY_PER_RUN", 2)
    max_slack_per_domain = getattr(config, "SCOUT_MAX_SLACK_PER_DOMAIN_PER_RUN", 2)
    min_score = getattr(config, "SCOUT_MIN_SCORE_TO_CREATE", 0)

    items = discover_candidates()
    log.info(f"Discovered {len(items)} candidates")

    # 1) Classify quickly and compute final score
    candidates: List[Dict[str, Any]] = []
    for it in items:
        try:
            title = it.get("title") or ""
            url = it.get("url") or ""
            if not title or not url:
                continue
            category_slug, match_score, hits = classify_item(title, it.get("snippet", ""))
            if not category_slug:
                continue
            fs = _final_score(category_slug, match_score)
            if fs < min_score:
                continue
            candidates.append({
                "item": it,
                "title": title,
                "url": url,
                "category_slug": category_slug,
                "match_score": int(match_score),
                "final_score": int(fs),
                "hits": hits,
                "domain": domain_of(url),
            })
        except Exception:
            continue

    candidates.sort(key=lambda x: x.get("final_score", 0), reverse=True)

    # 2) Create leads (cap to avoid excessive DB writes)
    created_leads: List[Dict[str, Any]] = []
    created_count = 0
    for c in candidates:
        if created_count >= max_new_leads:
            break
        try:
            title = c["title"]
            url = c["url"]
            if already_seen(title, url):
                continue
            category_slug = c["category_slug"]
            hits = c.get("hits") or []
            status = config.LEAD_STATUS_PENDING if config.REQUIRE_SLACK_APPROVAL else config.LEAD_STATUS_QUEUED
            lead_id, _ = create_lead(c["item"], category_slug, c["final_score"], hits, status)
            created_leads.append({
                "id": lead_id,
                "title": title,
                "url": url,
                "category_slug": category_slug,
                "score": c["final_score"],
                "hits": hits,
                "domain": c.get("domain") or domain_of(url),
            })
            created_count += 1
        except Exception as e:
            log.error(f"Error creating lead: {e}\n{traceback.format_exc()}")

    # 3) Slack posting (hard cap + quotas)
    if config.REQUIRE_SLACK_APPROVAL and max_slack > 0:
        slack_remaining = max_slack

        def _select_with_quotas(rows: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            cat_count: Dict[str, int] = {}
            dom_count: Dict[str, int] = {}
            for r in rows:
                if len(out) >= limit:
                    break
                cat = r.get("category_slug") or ""
                dom = r.get("domain") or domain_of(r.get("url") or "")
                if max_slack_per_cat and cat_count.get(cat, 0) >= max_slack_per_cat:
                    continue
                if max_slack_per_domain and dom_count.get(dom, 0) >= max_slack_per_domain:
                    continue
                out.append(r)
                cat_count[cat] = cat_count.get(cat, 0) + 1
                dom_count[dom] = dom_count.get(dom, 0) + 1
            return out

        # Prefer newest/highest score among newly created leads
        created_leads.sort(key=lambda x: int(x.get("score", 0)), reverse=True)
        to_post = _select_with_quotas(created_leads, slack_remaining)

        def _post_one(lead_row: Dict[str, Any]) -> bool:
            lead_id = lead_row["id"]
            title = lead_row.get("title") or ""
            url = lead_row.get("url") or ""
            category_slug = lead_row.get("category_slug") or ""
            score = int(lead_row.get("score") or 0)
            hits = lead_row.get("hits") or []
            resp = slack_post_candidate(lead_id, title, url, category_slug, score, hits)
            if resp:
                ch, ts = resp
                directus_update_lead(lead_id, {config.LEAD_F_SLACK_TS: ts, config.LEAD_F_SLACK_CHANNEL: ch})
                return True
            # If Slack fails, still queue it (avoid interruptions)
            directus_update_lead(lead_id, {config.LEAD_F_STATUS: config.LEAD_STATUS_QUEUED})
            return False

        posted = 0
        for row in to_post:
            if slack_remaining <= 0:
                break
            if _post_one(row):
                posted += 1
                slack_remaining -= 1

        # Post backlog pending leads that do not yet have slack_ts
        if slack_remaining > 0:
            try:
                backlog_filters = {
                    config.LEAD_F_STATUS: {"_eq": config.LEAD_STATUS_PENDING},
                    config.LEAD_F_SLACK_TS: {"_null": True},
                }
                fields = [
                    "id",
                    config.LEAD_F_TITLE,
                    config.LEAD_F_SOURCE_URL,
                    config.LEAD_F_CATEGORY,
                    config.LEAD_F_PRIORITY,
                    config.LEAD_F_MATCHED_KEYWORDS,
                ]
                backlog = directus_find_items(
                    config.LEADS_COLLECTION,
                    backlog_filters,
                    fields=fields,
                    limit=50,
                    sort=f"-{config.LEAD_F_PRIORITY},-{config.LEAD_F_DISCOVERED_AT}",
                )
                backlog_rows: List[Dict[str, Any]] = []
                for b in backlog:
                    backlog_rows.append({
                        "id": b.get("id"),
                        "title": b.get(config.LEAD_F_TITLE) or "",
                        "url": b.get(config.LEAD_F_SOURCE_URL) or "",
                        "category_slug": b.get(config.LEAD_F_CATEGORY) or "",
                        "score": int(b.get(config.LEAD_F_PRIORITY) or 3) * 10,
                        "hits": b.get(config.LEAD_F_MATCHED_KEYWORDS) or [],
                        "domain": domain_of(b.get(config.LEAD_F_SOURCE_URL) or ""),
                    })
                backlog_rows.sort(key=lambda x: int(x.get("score", 0)), reverse=True)
                backlog_pick = _select_with_quotas(backlog_rows, slack_remaining)
                for row in backlog_pick:
                    if slack_remaining <= 0:
                        break
                    if _post_one(row):
                        posted += 1
                        slack_remaining -= 1
            except Exception as e:
                log.warning(f"Backlog Slack posting failed: {e}")

        log.info(f"Slack posted {posted} item(s) (cap={max_slack})")

    log.info(f"New leads created: {created_count}")
    return created_count


def main():
    interval = int(getattr(config, "SCOUT_INTERVAL_SECONDS", 30 * 60))
    log.info("Scout started")
    while True:
        try:
            run_once()
        except Exception as e:
            log.error(f"Scout run failed: {e}\n{traceback.format_exc()}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
