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
from typing import Any, Dict, List

import config
from common import (
    log, state,
    fetch_rss_entries, classify_item,
    canonicalize_url, compute_fingerprint, domain_of,
    directus_item_exists_by_filters, directus_create_lead, directus_update_lead,
    slack_post_candidate,
    newsdata_discover
)


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
        config.LEAD_F_DISCOVERED_AT: item.get("discovered_at") or None,
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
    new_count = 0
    items = discover_candidates()
    log.info(f"Discovered {len(items)} candidates")

    for it in items:
        try:
            title = it["title"]
            url = it["url"]
            if already_seen(title, url):
                continue

            category_slug, score, hits = classify_item(title, it.get("snippet",""))
            if not category_slug:
                continue

            # Create lead
            status = config.LEAD_STATUS_PENDING if config.REQUIRE_SLACK_APPROVAL else config.LEAD_STATUS_QUEUED
            lead_id, fp = create_lead(it, category_slug, score, hits, status)

            # Slack notify
            if config.REQUIRE_SLACK_APPROVAL:
                resp = slack_post_candidate(lead_id, title, url, category_slug, score, hits)
                if resp:
                    ch, ts = resp
                    directus_update_lead(lead_id, {config.LEAD_F_SLACK_TS: ts, config.LEAD_F_SLACK_CHANNEL: ch})
                else:
                    # If Slack fails, still queue it (avoid interruption)
                    directus_update_lead(lead_id, {config.LEAD_F_STATUS: config.LEAD_STATUS_QUEUED})
            new_count += 1

        except Exception as e:
            log.error(f"Error processing candidate: {e}\n{traceback.format_exc()}")

    log.info(f"New leads created: {new_count}")
    return new_count


def main():
    interval = 20 * 60  # 20 minutes
    log.info("Scout started")
    while True:
        try:
            run_once()
        except Exception as e:
            log.error(f"Scout run failed: {e}\n{traceback.format_exc()}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
