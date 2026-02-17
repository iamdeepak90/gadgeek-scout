"""
Robust RSS news scout

Key changes vs old scout.py:
- Only 2 LLM calls per run:
  1) semantic dedup + quality ranking (across all candidates)
  2) category mapping (batched) for shortlisted candidates
- No per-entry category LLM calls during collection.
- Batched Directus URL duplication checks (news_leads + articles) over last N hours.
- Candidate collection limited per feed (default 25) with in-run URL/title dedup.
- Minimal, readable logs (phase summaries + errors). Use DEBUG for deep troubleshooting.

This file depends on the project's `common.py` helpers.
"""

import sys
import re
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

from common import (
    LOG,
    setup_logging,
    init_db,
    list_feeds,
    parse_feed,
    extract_entry_fields,
    create_lead,
    slack_post_lead,
    get_categories,
    get_setting,
    articles_collection,
    directus_get,
    request_with_retry,
    DEFAULT_CATEGORY_UUID,
    OPENROUTER_CHAT_URL,
)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

# Models (OpenRouter)
LLM_FILTER_MODEL = "meta-llama/llama-4-scout"
LLM_CATMATCH_MODEL = "meta-llama/llama-3.1-8b-instruct"
LLM_CATMATCH_FALLBACK = "deepseek/deepseek-r1-distill-llama-70b"

# Limits
ENTRIES_PER_FEED = 25                 # how many items to read from each feed per run
GLOBAL_SCOUT_CAP = 40                 # max posts per run (Slack/Directus)
LLM_CANDIDATE_LIMIT = 220             # cap candidates sent to the LLM filter prompt
LLM_RANK_RETURN_LIMIT = 120           # ask LLM to return up to this many ranked picks

# "Duplicate memory" windows
DIRECTUS_URL_DUP_HOURS = 168          # URL duplication check in Directus (7 days)
TITLE_DUP_HOURS = 168                 # titles loaded for semantic dedup (7 days)

# Prompt shaping
EXISTING_TITLE_LIMIT = 140            # number of existing titles to include in prompt
CANDIDATE_DESC_CHARS = 140            # how much description/snippet to include per candidate in LLM prompts

# URL normalization
STRIP_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "fbclid", "gclid", "ref", "source", "ncid", "ocid",
    "sr_share", "dicbo", "soc_src", "soc_trk",
}


# ─────────────────────────────────────────────────────────────────────────────
# DATA MODEL
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    title: str
    link: str
    norm_url: str
    feed_name: str
    description: str = ""
    rss_category: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS: NORMALIZATION
# ─────────────────────────────────────────────────────────────────────────────

def normalize_url(url: str) -> str:
    """Normalize URL: lowercase host, strip tracking params, www, trailing slash, fragment."""
    try:
        parsed = urlparse(url.strip())
        scheme = parsed.scheme.lower() or "https"
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        query_params = parse_qs(parsed.query, keep_blank_values=False)
        filtered = {k: v for k, v in query_params.items() if k.lower() not in STRIP_PARAMS}
        clean_query = urlencode(filtered, doseq=True) if filtered else ""
        path = parsed.path.rstrip("/")
        return urlunparse((scheme, host, path, parsed.params, clean_query, ""))
    except Exception:
        return url.strip().rstrip("/")


_title_key_re = re.compile(r"[^a-z0-9]+")

def title_key(title: str) -> str:
    """Cheap title fingerprint for in-run dedup."""
    t = (title or "").strip().lower()
    t = _title_key_re.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def _cap(s: str, n: int) -> str:
    s = (s or "").strip()
    return s[:n] if len(s) > n else s


# ─────────────────────────────────────────────────────────────────────────────
# DIRECTUS: BATCH URL DEDUP (7 days)
# ─────────────────────────────────────────────────────────────────────────────

def _utc_cutoff_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")


def _chunks(seq: Sequence[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(seq), size):
        yield list(seq[i:i+size])


def _fetch_existing_urls_for_collection(
    collection: str,
    urls_to_check: Sequence[str],
    cutoff_iso: str,
    chunk_size: int = 25,
) -> List[str]:
    """
    Query Directus for source_url in urls_to_check within date_created >= cutoff_iso.
    Returns list of matching source_url values.
    """
    found: List[str] = []
    if not urls_to_check:
        return found

    for chunk in _chunks(list(urls_to_check), chunk_size):
        # Directus supports _in as comma-separated list.
        # Values are URL-decoded server-side, so urlencode is safe.
        params = urlencode(
            {
                "filter[date_created][_gte]": cutoff_iso,
                "filter[source_url][_in]": ",".join(chunk),
                "fields": "source_url",
                "limit": 500,
            }
        )
        try:
            data = directus_get(f"/items/{collection}?{params}")
            for item in (data.get("data") or []):
                u = (item.get("source_url") or "").strip()
                if u:
                    found.append(u)
        except Exception as exc:
            LOG.warning("Directus URL check failed for %s: %s", collection, exc)

    return found


def fetch_existing_norm_urls(
    candidates: Sequence[Candidate],
    hours: int = DIRECTUS_URL_DUP_HOURS,
) -> set:
    """
    Batched URL dedup across news_leads and articles within last N hours.
    Returns a set of *normalized* URLs that already exist.
    """
    cutoff_iso = _utc_cutoff_iso(hours)

    raw_urls: List[str] = []
    norm_urls: List[str] = []
    for c in candidates:
        if c.link:
            raw_urls.append(c.link)
        if c.norm_url and c.norm_url != c.link:
            norm_urls.append(c.norm_url)

    urls_to_check = list(dict.fromkeys(raw_urls + norm_urls))  # preserve order, unique

    leads_urls = _fetch_existing_urls_for_collection("news_leads", urls_to_check, cutoff_iso)
    art_col = articles_collection()
    articles_urls = _fetch_existing_urls_for_collection(art_col, urls_to_check, cutoff_iso)

    found_norm = set()
    for u in leads_urls + articles_urls:
        found_norm.add(normalize_url(u))

    return found_norm


# ─────────────────────────────────────────────────────────────────────────────
# DIRECTUS: RECENT TITLES (7 days)
# ─────────────────────────────────────────────────────────────────────────────

def get_recent_titles(hours: int = TITLE_DUP_HOURS) -> List[str]:
    """Fetch titles from BOTH news_leads AND articles from last N hours."""
    cutoff = _utc_cutoff_iso(hours)
    titles: List[str] = []

    # news_leads
    try:
        params = urlencode(
            {
                "filter[date_created][_gte]": cutoff,
                "fields": "title",
                "limit": 300,
                "sort": "-date_created",
            }
        )
        data = directus_get(f"/items/news_leads?{params}")
        for item in (data.get("data") or []):
            t = (item.get("title") or "").strip()
            if t:
                titles.append(t)
    except Exception as exc:
        LOG.warning("Failed to fetch recent titles from news_leads: %s", exc)

    # articles
    try:
        col = articles_collection()
        params = urlencode(
            {
                "filter[date_created][_gte]": cutoff,
                "fields": "title",
                "limit": 300,
                "sort": "-date_created",
            }
        )
        data = directus_get(f"/items/{col}?{params}")
        for item in (data.get("data") or []):
            t = (item.get("title") or "").strip()
            if t:
                titles.append(t)
    except Exception as exc:
        LOG.warning("Failed to fetch recent titles from articles: %s", exc)

    # de-dupe titles (case-insensitive)
    seen = set()
    unique: List[str] = []
    for t in titles:
        k = t.strip().lower()
        if k and k not in seen:
            seen.add(k)
            unique.append(t)

    return unique


# ─────────────────────────────────────────────────────────────────────────────
# OPENROUTER: BASIC CHAT
# ─────────────────────────────────────────────────────────────────────────────

def openrouter_chat(
    messages: List[Dict[str, str]],
    model: str,
    *,
    max_tokens: int,
    temperature: float,
    timeout: int = 60,
    attempts: int = 2,
) -> str:
    api_key = get_setting("openrouter_api_key")
    if not api_key:
        raise RuntimeError("OpenRouter API key not configured.")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://bot.gadgeek.in",
        "X-Title": "Gadgeek Tech News",
    }
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    resp = request_with_retry(
        "POST",
        OPENROUTER_CHAT_URL,
        headers=headers,
        json_body=payload,
        timeout=timeout,
        max_attempts=attempts,
    )
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenRouter returned no choices: {data}")
    return ((choices[0].get("message") or {}).get("content") or "").strip()


# ─────────────────────────────────────────────────────────────────────────────
# LLM #1: SEMANTIC DEDUP + QUALITY RANKING
# ─────────────────────────────────────────────────────────────────────────────

def llm_rank_candidates(
    candidates: List[Candidate],
    existing_titles: List[str],
    *,
    max_return: int,
) -> List[int]:
    """
    One LLM call: rank candidates best→worst, skipping semantic duplicates.
    Returns list of 0-based indices into `candidates`, in ranked order.

    If LLM is unavailable, returns indices in original order.
    """
    if not candidates:
        return []

    api_key = get_setting("openrouter_api_key")
    if not api_key:
        LOG.info("No OpenRouter key set — skipping LLM ranking.")
        return list(range(len(candidates)))

    existing_block = ""
    if existing_titles:
        ex = existing_titles[:EXISTING_TITLE_LIMIT]
        existing_block = "SECTION A — ALREADY SCOUTED/PUBLISHED (last 7 days):\n"
        existing_block += "\n".join(f"- {t}" for t in ex)
        existing_block += "\n\n"

    # Candidate lines
    lines = []
    for i, c in enumerate(candidates, start=1):
        desc = _cap(c.description, CANDIDATE_DESC_CHARS)
        feed = _cap(c.feed_name, 30)
        extra = f" — {desc}" if desc else ""
        lines.append(f"{i}. [{feed}] {c.title}{extra}")

    candidates_block = "SECTION B — NEW CANDIDATES:\n" + "\n".join(lines)

    system = (
        "You are an editor for a technology news website.\n"
        "You will rank new RSS articles, remove duplicates, and keep only high-value news.\n"
        "You must be strict about duplicates and fluff."
    )

    user = (
        f"{existing_block}{candidates_block}\n\n"
        f"TASK:\n"
        f"1) From SECTION B, remove semantic duplicates and low-value items.\n"
        f"2) Return up to {max_return} remaining items in BEST→WORST order.\n\n"
        "DUPLICATE RULES:\n"
        "- Duplicate = the SAME news event reported by different sources.\n"
        "- Not duplicate = same product but a different event/angle (launch vs leak vs sale vs update).\n"
        "- If a candidate matches the SAME event as any title in SECTION A, treat as duplicate and remove.\n\n"
        "QUALITY RULES (prefer): launches, major updates, breaking news, India relevance, meaningful specs/pricing.\n"
        "Skip: PR fluff, listicles, generic tips, outdated items.\n\n"
        "OUTPUT FORMAT (strict):\n"
        "Return ONLY the numbers from SECTION B, comma-separated, in BEST→WORST order.\n"
        "Example: 7,2,19,4\n"
        "Do not add any other text."
    )

    try:
        content = openrouter_chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            LLM_FILTER_MODEL,
            max_tokens=240,
            temperature=0.1,
            timeout=75,
            attempts=2,
        )
        # Keep order as returned
        nums = [int(x) for x in re.findall(r"\d+", content)]
        ranked: List[int] = []
        seen = set()
        for n in nums:
            if 1 <= n <= len(candidates) and n not in seen:
                ranked.append(n - 1)
                seen.add(n)
            if len(ranked) >= max_return:
                break

        if ranked:
            return ranked

        LOG.warning("LLM ranking returned no valid indices. Falling back to original order.")
        return list(range(len(candidates)))

    except Exception as exc:
        LOG.warning("LLM ranking failed (%s). Falling back to original order.", exc)
        return list(range(len(candidates)))


# ─────────────────────────────────────────────────────────────────────────────
# LLM #2: CATEGORY MAPPING (BATCHED)
# ─────────────────────────────────────────────────────────────────────────────

def _build_category_reference(categories: List[Dict[str, Any]]) -> str:
    lines = []
    for c in categories:
        cid = str(c.get("id") or "").strip()
        slug = (c.get("slug") or "").strip()
        name = (c.get("name") or "").strip()
        if cid and name:
            # Include slug if present (helps model), but ID is authoritative.
            if slug:
                lines.append(f"{cid} | {slug} | {name}")
            else:
                lines.append(f"{cid} | {name}")
    return "\n".join(lines)


def llm_map_categories(
    items: List[Candidate],
    categories: List[Dict[str, Any]],
) -> Dict[int, str]:
    """
    One batched LLM call to map each item to a Directus category UUID.
    Returns dict {item_index (0-based within `items`): category_uuid}.

    If LLM fails, all items map to DEFAULT_CATEGORY_UUID.
    """
    if not items:
        return {}

    api_key = get_setting("openrouter_api_key")
    if not api_key:
        LOG.info("No OpenRouter key set — using default category for all picks.")
        return {i: DEFAULT_CATEGORY_UUID for i in range(len(items))}

    valid_ids = {str(c.get("id") or "") for c in categories if c.get("id")}
    cat_ref = _build_category_reference(categories)

    # Article snippets
    art_lines = []
    for i, c in enumerate(items, start=1):
        desc = _cap(c.description, CANDIDATE_DESC_CHARS)
        rss = _cap(c.rss_category, 60)
        feed = _cap(c.feed_name, 30)
        parts = [f"Title: {c.title}"]
        if desc:
            parts.append(f"Description: {desc}")
        if rss:
            parts.append(f"RSS category: {rss}")
        parts.append(f"Source: {feed}")
        snippet = " | ".join(parts)
        art_lines.append(f"{i}. {snippet}")

    system = (
        "You are a content classification assistant.\n"
        "Assign the single best category from the provided list to each article.\n"
        "You must output only JSON."
    )

    user = (
        "CATEGORIES (Directus UUID | slug | name):\n"
        f"{cat_ref}\n\n"
        "ARTICLES:\n"
        f"{chr(10).join(art_lines)}\n\n"
        "TASK:\n"
        "For each article number, choose exactly ONE category UUID from the list.\n"
        "If nothing fits well, use the most general category.\n\n"
        "OUTPUT JSON ONLY in this exact shape:\n"
        '[{"n": 1, "category_id": "UUID"}, {"n": 2, "category_id": "UUID"}]\n'
        "No extra keys. No markdown. No explanation."
    )

    last_exc: Optional[Exception] = None
    for model in (LLM_CATMATCH_MODEL, LLM_CATMATCH_FALLBACK):
        try:
            content = openrouter_chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                model,
                max_tokens=900,
                temperature=0.0,
                timeout=75,
                attempts=2,
            )

            # Extract JSON array robustly
            json_text = content.strip()
            # Sometimes models wrap in code fences; strip them.
            if json_text.startswith("```"):
                json_text = re.sub(r"^```[a-zA-Z]*\s*", "", json_text)
                json_text = re.sub(r"\s*```$", "", json_text).strip()

            data = json.loads(json_text)
            if not isinstance(data, list):
                raise ValueError("Category map output is not a JSON list")

            mapping: Dict[int, str] = {}
            for obj in data:
                if not isinstance(obj, dict):
                    continue
                n = obj.get("n")
                cid = str(obj.get("category_id") or "").strip()
                if isinstance(n, int) and 1 <= n <= len(items) and cid in valid_ids:
                    mapping[n - 1] = cid

            if mapping:
                return mapping

            LOG.warning("LLM category mapping returned empty/invalid mapping with model %s.", model)

        except Exception as exc:
            last_exc = exc
            LOG.warning("LLM category mapping failed with model %s (%s).", model, exc)

    # Fallback: default category for all
    if last_exc:
        LOG.warning("Category mapping failed; using default category for all picks.")
    return {i: DEFAULT_CATEGORY_UUID for i in range(len(items))}


# ─────────────────────────────────────────────────────────────────────────────
# MAIN SCOUT
# ─────────────────────────────────────────────────────────────────────────────

def scout_once() -> int:
    init_db()

    feeds = [f for f in list_feeds() if f.get("enabled")]
    if not feeds:
        LOG.error("No RSS feeds configured/enabled.")
        return 0

    categories = get_categories()
    if not categories:
        LOG.error("No enabled categories found in Directus.")
        return 0

    per_cat_cap = {str(c.get("id") or ""): int(c.get("posts_per_scout", 0) or 0) for c in categories}

    LOG.info("Scout started: feeds=%d, entries_per_feed=%d, directus_url_dup=%dh, title_dup=%dh",
             len(feeds), ENTRIES_PER_FEED, DIRECTUS_URL_DUP_HOURS, TITLE_DUP_HOURS)

    # Phase 1: collect candidates (cheap)
    candidates: List[Candidate] = []
    seen_norm_urls = set()
    seen_title_keys = set()

    skipped = {
        "parse_fail": 0,
        "missing_title_or_link": 0,
        "dup_in_run_url": 0,
        "dup_in_run_title": 0,
    }

    for feed_cfg in feeds:
        feed_url = feed_cfg.get("url") or ""
        feed_name = (feed_cfg.get("name") or feed_cfg.get("title") or feed_url).strip() or "feed"
        try:
            parsed = parse_feed(feed_url)
        except Exception:
            skipped["parse_fail"] += 1
            continue

        entries = parsed.entries or []
        for ent in entries[:ENTRIES_PER_FEED]:
            fields = extract_entry_fields(ent, feed_cfg)
            title = (fields.get("title") or "").strip()
            link = (fields.get("link") or "").strip()
            if not title or not link:
                skipped["missing_title_or_link"] += 1
                continue

            norm = normalize_url(link)
            if norm in seen_norm_urls:
                skipped["dup_in_run_url"] += 1
                continue

            tk = title_key(title)
            if tk and tk in seen_title_keys:
                skipped["dup_in_run_title"] += 1
                seen_norm_urls.add(norm)
                continue

            desc = (fields.get("description") or fields.get("content") or "").strip()
            rss_cat = (fields.get("category") or "").strip()

            candidates.append(
                Candidate(
                    title=title,
                    link=link,
                    norm_url=norm,
                    feed_name=feed_name,
                    description=_cap(desc, 500),
                    rss_category=rss_cat,
                )
            )
            seen_norm_urls.add(norm)
            if tk:
                seen_title_keys.add(tk)

    if not candidates:
        LOG.info("No candidates collected. Skipped: %s", skipped)
        return 0

    LOG.info("Phase 1: collected=%d candidates. Skipped=%s", len(candidates), skipped)

    # Phase 2: batched URL duplication check in Directus (7 days)
    existing_norm_urls = fetch_existing_norm_urls(candidates, hours=DIRECTUS_URL_DUP_HOURS)
    before = len(candidates)
    candidates = [c for c in candidates if c.norm_url not in existing_norm_urls]
    LOG.info("Phase 2: directus URL dedup removed=%d, remaining=%d", before - len(candidates), len(candidates))

    if not candidates:
        LOG.info("No candidates left after Directus URL dedup.")
        return 0

    # Limit LLM input size (avoid huge prompts) with a balanced per-feed sample
    if len(candidates) > LLM_CANDIDATE_LIMIT:
        per_feed = max(1, LLM_CANDIDATE_LIMIT // max(1, len(feeds)))
        buckets: Dict[str, List[Candidate]] = {}
        for c in candidates:
            buckets.setdefault(c.feed_name, []).append(c)

        sampled: List[Candidate] = []
        # First pass: take up to `per_feed` from each feed
        for fname in sorted(buckets.keys()):
            sampled.extend(buckets[fname][:per_feed])

        # Second pass: fill remaining slots with the earliest remaining items overall
        if len(sampled) < LLM_CANDIDATE_LIMIT:
            remainder: List[Candidate] = []
            for fname in sorted(buckets.keys()):
                remainder.extend(buckets[fname][per_feed:])
            sampled.extend(remainder[: max(0, LLM_CANDIDATE_LIMIT - len(sampled))])

        candidates_for_llm = sampled[:LLM_CANDIDATE_LIMIT]
        LOG.info("Trimming candidates for LLM ranking: %d → %d (per_feed=%d)", len(candidates), len(candidates_for_llm), per_feed)
    else:
        candidates_for_llm = candidates

    # Phase 3: LLM ranking (semantic dedup + quality)
    existing_titles = get_recent_titles(hours=TITLE_DUP_HOURS)
    ranked_idx = llm_rank_candidates(
        candidates_for_llm,
        existing_titles,
        max_return=min(LLM_RANK_RETURN_LIMIT, len(candidates_for_llm)),
    )
    ranked = [candidates_for_llm[i] for i in ranked_idx] if ranked_idx else candidates_for_llm
    LOG.info("Phase 3: ranked=%d (from %d candidates sent to LLM)", len(ranked), len(candidates_for_llm))

    # We will try to fill up to GLOBAL_SCOUT_CAP after category caps.
    # To reduce dropped slots, categorize more than GLOBAL_SCOUT_CAP if available.
    categorize_n = min(len(ranked), max(GLOBAL_SCOUT_CAP * 3, GLOBAL_SCOUT_CAP))
    to_categorize = ranked[:categorize_n]

    # Phase 4: Category mapping (batched)
    cat_map = llm_map_categories(to_categorize, categories)

    # Ensure per-category caps are applied and category id is valid
    valid_cat_ids = {str(c.get("id") or "") for c in categories if c.get("id")}
    default_cat_name = next((c.get("name") for c in categories if str(c.get("id") or "") == DEFAULT_CATEGORY_UUID), "General")

    # Build id -> name map
    cat_name_by_id = {str(c.get("id") or ""): (c.get("name") or "") for c in categories}

    # Phase 5: Create leads + post Slack (apply caps)
    picked_per_cat = {cid: 0 for cid in valid_cat_ids}
    created = 0
    created_by_cat: Dict[str, int] = {}

    for i, cand in enumerate(ranked):
        if created >= GLOBAL_SCOUT_CAP:
            break

        # category for this candidate (only for those we categorized; rest default)
        cid = cat_map.get(i, DEFAULT_CATEGORY_UUID)
        if cid not in valid_cat_ids:
            cid = DEFAULT_CATEGORY_UUID

        cap = int(per_cat_cap.get(cid, 0) or 0)
        if cap <= 0:
            # category not intended for scouting
            continue
        if picked_per_cat.get(cid, 0) >= cap:
            continue

        # Create lead
        try:
            lead_id = create_lead(title=cand.title, source_url=cand.link, category_id=cid)
        except Exception as exc:
            LOG.error("Create lead failed: %s", exc)
            continue

        # Slack post
        cat_name = cat_name_by_id.get(cid) or default_cat_name
        try:
            slack_post_lead(title=cand.title, category_name=cat_name, lead_id=lead_id)
        except Exception as exc:
            LOG.error("Slack post failed (lead_id=%s): %s", lead_id, exc)

        picked_per_cat[cid] = picked_per_cat.get(cid, 0) + 1
        created_by_cat[cid] = created_by_cat.get(cid, 0) + 1
        created += 1

    # Summary
    if created_by_cat:
        summary_bits = []
        for cid, cnt in sorted(created_by_cat.items(), key=lambda x: -x[1]):
            summary_bits.append(f"{cat_name_by_id.get(cid, cid) or cid}: {cnt}")
        per_cat_summary = "; ".join(summary_bits)
    else:
        per_cat_summary = "none"

    LOG.info("Scout complete: created=%d (cap=%d). Per-category: %s", created, GLOBAL_SCOUT_CAP, per_cat_summary)
    return created


def main() -> None:
    setup_logging()
    try:
        scout_once()
    except Exception as exc:
        LOG.exception("Scout failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()