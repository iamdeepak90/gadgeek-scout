"""
publisher.py — Processes approved leads (queued) and publishes articles.

Run as a service:
  python publisher.py

Run once:
  python publisher.py --once

Process a specific lead:
  python publisher.py --lead-id 123
"""

from __future__ import annotations

import argparse
import traceback
import time
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import config
from common import (
    log,
    acquire_lock, release_lock,
    directus_find_items, directus_update_lead, directus_create_article, directus_item_exists_by_filters,
    canonicalize_url, compute_fingerprint, slugify, domain_of,
    langsearch_web_search, langsearch_rerank, brave_search,
    fetch_url_text, pick_unique_domains,
    pick_image_for_query,
    get_published_today, increment_published_today, get_last_publish_ts, set_last_publish_ts,
    in_publish_window, word_count
)
from ai_content import create_complete_article


def now_local() -> dt.datetime:
    return dt.datetime.now(ZoneInfo(config.TIMEZONE))

def can_publish_now() -> Tuple[bool, str]:
    # publish window
    nl = now_local()
    if not in_publish_window(nl, config.PUBLISH_WINDOW_START, config.PUBLISH_WINDOW_END):
        return False, "outside publish window"

    # daily cap
    if get_published_today() >= config.MAX_PUBLISH_PER_DAY:
        return False, "daily publish cap reached"

    # minimum gap
    last = get_last_publish_ts()
    if last:
        delta_min = (dt.datetime.now(dt.timezone.utc) - last).total_seconds() / 60.0
        if delta_min < config.MIN_MINUTES_BETWEEN_PUBLISHES:
            return False, f"min gap not met ({delta_min:.1f}m)"
    return True, "ok"


def get_queued_leads(limit: int = 10) -> List[Dict[str,Any]]:
    # Priority sort: lowest number first (0 = urgent)
    fields = [
        "id",
        config.LEAD_F_TITLE,
        config.LEAD_F_SOURCE_URL,
        config.LEAD_F_CATEGORY,
        config.LEAD_F_FINGERPRINT,
        config.LEAD_F_STATUS,
        config.LEAD_F_PRIORITY,
    ]
    items = directus_find_items(
        config.LEADS_COLLECTION,
        {
            config.LEAD_F_STATUS: {"_eq": config.LEAD_STATUS_QUEUED}
        },
        fields=fields,
        limit=limit
    )
    # sort by priority then by id (stable)
    def pri(x):
        try:
            return int(x.get(config.LEAD_F_PRIORITY) or 9)
        except Exception:
            return 9
    items.sort(key=lambda x: (pri(x), x.get("id")))
    return items


def lead_to_sources(lead: Dict[str,Any]) -> List[str]:
    """
    Source expansion strategy:
      - include seed url
      - LangSearch search (title)
      - optional rerank
      - fallback to Brave search
      - dedupe by domain
    """
    title = lead.get(config.LEAD_F_TITLE) or ""
    seed_url = canonicalize_url(lead.get(config.LEAD_F_SOURCE_URL) or "")
    urls: List[str] = []
    if seed_url:
        urls.append(seed_url)

    # Primary: LangSearch
    try:
        results = langsearch_web_search(title, count=config.LANGSEARCH_RESULTS, freshness=config.LANGSEARCH_FRESHNESS, summary=False)
        if config.LANGSEARCH_ENABLE_RERANK and results:
            try:
                ranked = langsearch_rerank(title, results, top_n=min(config.LANGSEARCH_TOP_SOURCES, len(results)))
            except Exception as e:
                log.warning(f"LangSearch rerank failed, continuing without rerank: {e}")
                ranked = results
        else:
            ranked = results
        for r in ranked:
            u = canonicalize_url(r.get("url",""))
            if u:
                urls.append(u)
    except Exception as e:
        log.warning(f"LangSearch failed: {e}")

    # Fallback: Brave
    if len(pick_unique_domains(urls, 20)) < 3 and config.BRAVE_SEARCH_API_KEY:
        try:
            b = brave_search(title, count=config.BRAVE_RESULTS)
            for r in b:
                u = canonicalize_url(r.get("url",""))
                if u:
                    urls.append(u)
        except Exception as e:
            log.warning(f"Brave fallback failed: {e}")

    # Keep unique domains, prefer more sources
    urls = pick_unique_domains(urls, max_items=max(6, config.LANGSEARCH_TOP_SOURCES))
    return urls


def build_source_pack(urls: List[str]) -> List[Dict[str,Any]]:
    sources: List[Dict[str,Any]] = []
    for u in urls:
        try:
            text = fetch_url_text(u)
            if not text or len(text) < 400:
                continue
            sources.append({
                "url": u,
                "domain": domain_of(u),
                "title": "",  # optional
                "text": text
            })
        except Exception as e:
            log.warning(f"Fetch/extract failed for {u}: {e}")
    # Ensure unique domains
    by_domain = {}
    for s in sources:
        if s["domain"] not in by_domain:
            by_domain[s["domain"]] = s
    return list(by_domain.values())


def publish_article(lead: Dict[str,Any]) -> Dict[str,Any]:
    title = lead.get(config.LEAD_F_TITLE) or ""
    seed_url = canonicalize_url(lead.get(config.LEAD_F_SOURCE_URL) or "")
    category_slug = lead.get(config.LEAD_F_CATEGORY) or ""
    fp = lead.get(config.LEAD_F_FINGERPRINT) or compute_fingerprint(title, seed_url)

    # Source expansion
    urls = lead_to_sources(lead)
    sources = build_source_pack(urls)

    # Hard gate: at least 3 distinct domains
    if len({s["domain"] for s in sources}) < 3:
        raise RuntimeError("Not enough distinct sources (need ≥3 domains). Will not publish.")

    # Image
    # Query: use title; in future you can use fact_pack.entities
    img = pick_image_for_query(title) or {}
    image_url = img.get("url") or ""
    image_credit = img.get("credit") or ""
    # alt/caption will be produced by SEO step, but we need placeholders now
    image_alt = "Tech news illustration"
    image_caption = "Image for the story"

    # AI pipeline
    result = create_complete_article(
        title=title,
        seed_url=seed_url,
        category_slug=category_slug,
        sources=sources,
        image_url=image_url or "https://via.placeholder.com/1400x800?text=Tech+News",
        image_alt=image_alt,
        image_caption=image_caption
    )

    html = result["html"]
    seo = result["seo"] or {}

    # Replace image metadata with SEO alt/caption if provided
    final_alt = seo.get("image_alt") or image_alt
    final_caption = seo.get("image_caption") or image_caption
    html = html.replace(image_alt, final_alt).replace(image_caption, final_caption)

    # Create article record
    slug = slugify(title)
    # Ensure slug uniqueness
    if directus_item_exists_by_filters(config.ARTICLES_COLLECTION, {config.ART_F_SLUG: {"_eq": slug}}):
        slug = f"{slug}-{fp[:6]}"
    short_desc = seo.get("short_description") or ""
    meta_title = seo.get("meta_title") or title[:70]
    meta_desc = seo.get("meta_description") or short_desc[:160]
    focus_keyword = seo.get("focus_keyword") or (seo.get("tags",[None])[0] if isinstance(seo.get("tags"), list) else "")
    tags = seo.get("tags") if isinstance(seo.get("tags"), list) else []

    article_payload = {
        config.ART_F_STATUS: config.ARTICLE_STATUS_PUBLISHED,
        config.ART_F_TITLE: title,
        config.ART_F_SLUG: slug,
        config.ART_F_SHORT_DESCRIPTION: short_desc,
        config.ART_F_CONTENT: html,
        config.ART_F_CATEGORY: category_slug,
        config.ART_F_SOURCE_URL: seed_url,
        config.ART_F_FINGERPRINT: fp,
        config.ART_F_SOURCES: [s["url"] for s in sources],
        config.ART_F_FEATURED_IMAGE_URL: image_url,
        config.ART_F_FEATURED_IMAGE_ALT: final_alt,
        config.ART_F_FEATURED_IMAGE_CREDIT: image_credit,
        config.ART_F_FOCUS_KEYWORD: focus_keyword,
        config.ART_F_TAGS: tags,
        config.ART_F_META_TITLE: meta_title,
        config.ART_F_META_DESCRIPTION: meta_desc,
        config.ART_F_WORD_COUNT: int(result.get("word_count") or 0),
        config.ART_F_PUBLISHED_AT: None,
    }

    created = directus_create_article(article_payload)
    return created


def process_lead(lead: Dict[str,Any]) -> Dict[str,Any]:
    lead_id = lead.get("id")
    title = lead.get(config.LEAD_F_TITLE) or ""
    seed_url = canonicalize_url(lead.get(config.LEAD_F_SOURCE_URL) or "")
    fp = lead.get(config.LEAD_F_FINGERPRINT) or compute_fingerprint(title, seed_url)

    # Hard duplicate gate (authoritative): skip if already published
    try:
        if directus_item_exists_by_filters(config.ARTICLES_COLLECTION, {config.ART_F_FINGERPRINT: {"_eq": fp}}) or \
           directus_item_exists_by_filters(config.ARTICLES_COLLECTION, {config.ART_F_SOURCE_URL: {"_eq": seed_url}}):
            directus_update_lead(lead_id, {config.LEAD_F_STATUS: config.LEAD_STATUS_PROCESSED, config.LEAD_F_LAST_ERROR: "duplicate_skip"})
            return {"ok": True, "lead_id": lead_id, "article_id": None, "skipped": True}
    except Exception as e:
        log.warning(f"Duplicate check failed (continuing): {e}")

    # Mark processing
    directus_update_lead(lead_id, {config.LEAD_F_STATUS: config.LEAD_STATUS_PROCESSING, config.LEAD_F_LAST_ERROR: ""})

    try:
        article = publish_article(lead)
        article_id = article.get("id") if isinstance(article, dict) else None
        directus_update_lead(lead_id, {config.LEAD_F_STATUS: config.LEAD_STATUS_PROCESSED})
        increment_published_today()
        set_last_publish_ts(dt.datetime.now(dt.timezone.utc))
        return {"ok": True, "lead_id": lead_id, "article_id": article_id}
    except Exception as e:
        directus_update_lead(lead_id, {config.LEAD_F_STATUS: config.LEAD_STATUS_FAILED, config.LEAD_F_LAST_ERROR: str(e)[:500]})
        raise


def process_lead_id(lead_id: Any) -> Dict[str,Any]:
    items = directus_find_items(
        config.LEADS_COLLECTION,
        {"id": {"_eq": lead_id}},
        fields=["*"],
        limit=1
    )
    if not items:
        raise RuntimeError("Lead not found")
    return process_lead(items[0])


def run_loop(once: bool = False):
    log.info("Publisher started")
    while True:
        ok, reason = can_publish_now()
        if not ok:
            log.info(f"Publish paused: {reason}")
            time.sleep(60)
            if once:
                return
            continue

        if not acquire_lock(config.LOCK_PATH):
            log.info("Another publisher instance is running (lock held).")
            time.sleep(20)
            if once:
                return
            continue

        try:
            leads = get_queued_leads(limit=10)
            if not leads:
                log.info("No queued leads.")
                time.sleep(60)
                if once:
                    return
                continue

            for lead in leads:
                ok, reason = can_publish_now()
                if not ok:
                    log.info(f"Publish paused mid-batch: {reason}")
                    break
                try:
                    log.info(f"Processing lead {lead.get('id')}: {lead.get(config.LEAD_F_TITLE)}")
                    res = process_lead(lead)
                    log.info(f"Published article_id={res.get('article_id')}")
                except Exception as e:
                    log.error(f"Lead processing failed: {e}\n{traceback.format_exc()}")

                # gap is enforced by can_publish_now using last publish ts
                time.sleep(5)

        finally:
            release_lock(config.LOCK_PATH)

        if once:
            return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="Run one loop iteration and exit")
    ap.add_argument("--lead-id", default="", help="Process a specific lead id and exit")
    args = ap.parse_args()

    if args.lead_id:
        res = process_lead_id(args.lead_id)
        print(res)
        return

    run_loop(once=args.once)


if __name__ == "__main__":
    main()
