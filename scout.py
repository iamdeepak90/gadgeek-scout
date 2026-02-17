"""
News scout (robust + low-noise)

What this version does:
- Reads up to ENTRIES_PER_FEED items per RSS feed.
- In-run dedup (normalized URL + cheap title fingerprint).
- Directus URL dedup (last DIRECTUS_URL_DUP_HOURS) against ONE collection (LEADS_COLLECTION).
- LLM call #1: semantic dedup + quality ranking (returns ordered numbers).
- LLM call #2: category mapping for shortlisted items (batched).
- If category mapping is missing/invalid for an item, that item is SKIPPED (no default fallback).
- Applies per-category caps and posts to Slack.

Depends on project helpers in common.py:
  LOG, setup_logging, init_db, list_feeds, parse_feed, extract_entry_fields,
  create_lead, slack_post_lead, get_categories, get_setting, directus_get,
  request_with_retry, OPENROUTER_CHAT_URL
"""

import sys
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence
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
    directus_get,
    request_with_retry,
    OPENROUTER_CHAT_URL,
)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

# Directus collection to check duplicates against
LEADS_COLLECTION = "news_leads"   # change if your leads collection is different

# Limits
ENTRIES_PER_FEED = 25             # items read per feed
MAX_POSTS_PER_RUN = 30            # total posts created per run (0 = unlimited)

# Duplicate windows
DIRECTUS_URL_DUP_HOURS = 168      # Directus URL dup window (7 days)
TITLE_DUP_HOURS = 168             # titles loaded for LLM semantic dup (7 days)

# LLM models (OpenRouter)
LLM_RANK_MODEL = "meta-llama/llama-4-scout"
LLM_CAT_MODEL = "meta-llama/llama-3.1-8b-instruct"
LLM_CAT_FALLBACK = "deepseek/deepseek-r1-distill-llama-70b"

# Prompt shaping / safety
LLM_CANDIDATE_LIMIT = 220         # cap candidates sent to ranking prompt
RANK_RETURN_LIMIT = 140           # ask LLM to return up to this many items ranked
EXISTING_TITLE_LIMIT = 140        # number of existing titles to include
DESC_CHARS = 200                  # chars of description included in prompts

# URL normalization
STRIP_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "fbclid", "gclid", "ref", "source", "ncid", "ocid",
    "sr_share", "dicbo", "soc_src", "soc_trk",
}

# Directus batching (keep small to avoid huge query strings with long URLs)
DIRECTUS_IN_CHUNK = 5


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
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

_title_key_re = re.compile(r"[^a-z0-9]+")

def title_key(title: str) -> str:
    t = (title or "").strip().lower()
    t = _title_key_re.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()

def _cap(s: str, n: int) -> str:
    s = (s or "").strip()
    return s[:n] if len(s) > n else s

def normalize_url(url: str) -> str:
    """Normalize URL: lowercase host, strip tracking params, remove www, strip fragment, strip trailing slash."""
    try:
        parsed = urlparse(url.strip())
        scheme = parsed.scheme.lower() or "https"
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        qp = parse_qs(parsed.query, keep_blank_values=False)
        filtered = {k: v for k, v in qp.items() if k.lower() not in STRIP_PARAMS}
        clean_query = urlencode(filtered, doseq=True) if filtered else ""
        path = parsed.path.rstrip("/")
        return urlunparse((scheme, host, path, parsed.params, clean_query, ""))
    except Exception:
        return url.strip().rstrip("/")

def _utc_cutoff_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")

def _chunks(seq: Sequence[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(seq), size):
        yield list(seq[i:i + size])


# ─────────────────────────────────────────────────────────────────────────────
# DIRECTUS: DUP CHECK + RECENT TITLES (LEADS COLLECTION ONLY)
# ─────────────────────────────────────────────────────────────────────────────

def _directus_fetch_field_in(
    collection: str,
    field: str,
    values: Sequence[str],
    cutoff_iso: str,
    *,
    return_field: str,
    chunk_size: int = DIRECTUS_IN_CHUNK,
    limit: int = 500,
) -> List[str]:
    """
    Fetch return_field values where:
      date_created >= cutoff_iso AND field IN values

    Note: uses GET with querystring; chunk_size is intentionally small to avoid very long URLs.
    """
    out: List[str] = []
    if not values:
        return out

    # unique while preserving order
    values_u = list(dict.fromkeys([v for v in values if v]))
    for chunk in _chunks(values_u, chunk_size):
        params = urlencode(
            {
                "filter[date_created][_gte]": cutoff_iso,
                f"filter[{field}][_in]": ",".join(chunk),
                "fields": return_field,
                "limit": limit,
            }
        )
        data = directus_get(f"/items/{collection}?{params}")
        for item in (data.get("data") or []):
            v = (item.get(return_field) or "").strip()
            if v:
                out.append(v)
    return out

def fetch_existing_norm_urls(candidates: Sequence[Candidate], hours: int = DIRECTUS_URL_DUP_HOURS) -> set:
    """
    Batched URL dedup against LEADS_COLLECTION within last N hours.
    Returns a set of normalized URLs that already exist.
    """
    cutoff_iso = _utc_cutoff_iso(hours)

    raw_urls: List[str] = []
    norm_urls: List[str] = []
    for c in candidates:
        if c.link:
            raw_urls.append(c.link)
        if c.norm_url and c.norm_url != c.link:
            norm_urls.append(c.norm_url)

    urls_to_check = list(dict.fromkeys(raw_urls + norm_urls))
    try:
        found = _directus_fetch_field_in(
            LEADS_COLLECTION,
            "source_url",
            urls_to_check,
            cutoff_iso,
            return_field="source_url",
        )
    except Exception as exc:
        LOG.warning("Directus URL dedup query failed (collection=%s): %s", LEADS_COLLECTION, exc)
        return set()

    return {normalize_url(u) for u in found}

def get_recent_titles(hours: int = TITLE_DUP_HOURS) -> List[str]:
    """Recent titles from LEADS_COLLECTION only (last N hours)."""
    cutoff = _utc_cutoff_iso(hours)
    titles: List[str] = []
    try:
        params = urlencode(
            {
                "filter[date_created][_gte]": cutoff,
                "fields": "title",
                "limit": 300,
                "sort": "-date_created",
            }
        )
        data = directus_get(f"/items/{LEADS_COLLECTION}?{params}")
        for item in (data.get("data") or []):
            t = (item.get("title") or "").strip()
            if t:
                titles.append(t)
    except Exception as exc:
        LOG.warning("Failed to fetch recent titles from %s: %s", LEADS_COLLECTION, exc)

    # de-dupe case-insensitive
    seen = set()
    uniq: List[str] = []
    for t in titles:
        k = t.lower().strip()
        if k and k not in seen:
            seen.add(k)
            uniq.append(t)
    return uniq


# ─────────────────────────────────────────────────────────────────────────────
# OPENROUTER CHAT
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
        "POST", OPENROUTER_CHAT_URL,
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
    Returns a list of 0-based indices into candidates, in best→worst order.
    Falls back to original order if no OpenRouter key or model error.
    """
    if not candidates:
        return []

    if not get_setting("openrouter_api_key"):
        return list(range(len(candidates)))

    existing_block = ""
    if existing_titles:
        ex = existing_titles[:EXISTING_TITLE_LIMIT]
        existing_block = "SECTION A — ALREADY SCOUTED/PUBLISHED (last 7 days):\n"
        existing_block += "\n".join(f"- {t}" for t in ex)
        existing_block += "\n\n"

    cand_lines = []
    for i, c in enumerate(candidates, start=1):
        feed = _cap(c.feed_name, 60)
        desc = _cap(c.description, DESC_CHARS)
        extra = f" — {desc}" if desc else ""
        cand_lines.append(f"{i}. [{feed}] {c.title}{extra}")

    user = (
        f"{existing_block}"
        f"SECTION B — NEW CANDIDATES:\n" + "\n".join(cand_lines) + "\n\n"
        "TASK:\n"
        "1) Remove semantic duplicates and low-value items.\n"
        f"2) Return up to {max_return} remaining items in BEST→WORST order.\n\n"
        "DUPLICATE RULES:\n"
        "- Duplicate = SAME news event across sources.\n"
        "- Not duplicate = same product but different event/angle (launch vs leak vs sale vs update).\n"
        "- If a candidate matches the SAME event as any title in SECTION A, remove it.\n\n"
        "QUALITY (prefer): launches, major updates, breaking news, India relevance, meaningful specs/pricing.\n"
        "Skip: PR fluff, listicles, generic tips, outdated.\n\n"
        "OUTPUT FORMAT (strict): numbers from SECTION B, comma-separated, BEST→WORST.\n"
        "Example: 7,2,19,4\n"
        "No other text."
    )

    system = (
        "You are a strict tech-news editor. Follow output format exactly."
    )

    try:
        content = openrouter_chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            LLM_RANK_MODEL,
            max_tokens=260,
            temperature=0.1,
            timeout=80,
            attempts=2,
        )

        nums = [int(x) for x in re.findall(r"\d+", content)]
        ranked: List[int] = []
        seen = set()
        for n in nums:
            if 1 <= n <= len(candidates) and n not in seen:
                ranked.append(n - 1)
                seen.add(n)
            if len(ranked) >= max_return:
                break

        return ranked if ranked else list(range(len(candidates)))

    except Exception as exc:
        LOG.warning("LLM ranking failed: %s", exc)
        return list(range(len(candidates)))


# ─────────────────────────────────────────────────────────────────────────────
# LLM #2: CATEGORY MAPPING (NO JSON; LINE FORMAT FOR ROBUSTNESS)
# ─────────────────────────────────────────────────────────────────────────────

_uuid_re = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

def _category_reference(categories: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for c in categories:
        cid = str(c.get("id") or "").strip()
        if not cid:
            continue
        slug = (c.get("slug") or "").strip()
        name = (c.get("name") or "").strip()
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
    Returns mapping: {index_in_items: category_uuid}.

    IMPORTANT:
    - If LLM doesn't provide a valid category for an item, that item is SKIPPED later.
    - If the LLM call fails entirely, returns empty dict (no default fallback).
    """
    if not items:
        return {}

    if not get_setting("openrouter_api_key"):
        LOG.warning("No OpenRouter key set — cannot map categories; will skip all.")
        return {}

    valid_ids = {str(c.get("id") or "") for c in categories if c.get("id")}
    cat_ref = _category_reference(categories)

    art_lines: List[str] = []
    for i, c in enumerate(items, start=1):
        desc = _cap(c.description, DESC_CHARS)
        rss = _cap(c.rss_category, 60)
        feed = _cap(c.feed_name, 30)
        parts = [f"Title: {c.title}"]
        if desc:
            parts.append(f"Description: {desc}")
        if rss:
            parts.append(f"RSS category: {rss}")
        parts.append(f"Source: {feed}")
        art_lines.append(f"{i}. " + " | ".join(parts))

    system = (
        "You are a content classification assistant.\n"
        "Choose the single best category UUID from the list for each article.\n"
        "Follow output format exactly."
    )

    user = (
        "CATEGORIES (UUID | slug | name):\n"
        f"{cat_ref}\n\n"
        "ARTICLES:\n"
        + "\n".join(art_lines)
        + "\n\n"
        "OUTPUT FORMAT (strict):\n"
        "One line per article in the form:\n"
        "n: UUID\n"
        "Example:\n"
        "1: 11111111-1111-1111-1111-111111111111\n"
        "2: 22222222-2222-2222-2222-222222222222\n"
        "Return ONLY these lines. No extra text."
    )

    last_exc: Optional[Exception] = None
    for model in (LLM_CAT_MODEL, LLM_CAT_FALLBACK):
        try:
            content = openrouter_chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                model,
                max_tokens=900,
                temperature=0.0,
                timeout=80,
                attempts=2,
            )

            # Parse lines like "n: uuid"
            mapping: Dict[int, str] = {}

            # Fast path: line-based parse
            for line in content.splitlines():
                m = re.match(r"^\s*(\d{1,4})\s*[:\|\-,]\s*(" + _uuid_re.pattern + r")\s*$", line.strip())
                if not m:
                    continue
                n = int(m.group(1))
                cid = m.group(2)
                if 1 <= n <= len(items) and cid in valid_ids:
                    mapping[n - 1] = cid

            if mapping:
                return mapping

            # Salvage: find all (n, uuid) pairs anywhere
            pairs = re.findall(r"(\d{1,4}).{0,10}(" + _uuid_re.pattern + r")", content, flags=re.IGNORECASE | re.DOTALL)
            for n_str, cid in pairs:
                n = int(n_str)
                if 1 <= n <= len(items) and cid in valid_ids:
                    mapping[n - 1] = cid

            if mapping:
                return mapping

            LOG.warning("LLM category mapping returned no valid category IDs (model=%s).", model)

        except Exception as exc:
            last_exc = exc
            LOG.warning("LLM category mapping failed (model=%s): %s", model, exc)

    if last_exc:
        LOG.warning("Category mapping failed for all models; will skip all candidates.")
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def _trim_candidates_for_llm(candidates: List[Candidate], feeds_count: int) -> List[Candidate]:
    """Limit candidates for ranking prompt while keeping some balance across feeds."""
    if len(candidates) <= LLM_CANDIDATE_LIMIT:
        return candidates

    per_feed = max(1, LLM_CANDIDATE_LIMIT // max(1, feeds_count))
    buckets: Dict[str, List[Candidate]] = {}
    for c in candidates:
        buckets.setdefault(c.feed_name, []).append(c)

    sampled: List[Candidate] = []
    for fname in sorted(buckets.keys()):
        sampled.extend(buckets[fname][:per_feed])

    if len(sampled) < LLM_CANDIDATE_LIMIT:
        remainder: List[Candidate] = []
        for fname in sorted(buckets.keys()):
            remainder.extend(buckets[fname][per_feed:])
        sampled.extend(remainder[: max(0, LLM_CANDIDATE_LIMIT - len(sampled))])

    return sampled[:LLM_CANDIDATE_LIMIT]

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
    valid_cat_ids = set(per_cat_cap.keys())

    LOG.info(
        "Scout started: feeds=%d, entries_per_feed=%d, directus_dup=%dh, max_posts=%s",
        len(feeds),
        ENTRIES_PER_FEED,
        DIRECTUS_URL_DUP_HOURS,
        ("unlimited" if MAX_POSTS_PER_RUN == 0 else str(MAX_POSTS_PER_RUN)),
    )

    # Phase 1: collect candidates
    candidates: List[Candidate] = []
    seen_urls = set()
    seen_titles = set()
    skipped = {"parse_fail": 0, "missing": 0, "dup_url": 0, "dup_title": 0}

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
                skipped["missing"] += 1
                continue

            norm = normalize_url(link)
            if norm in seen_urls:
                skipped["dup_url"] += 1
                continue

            tk = title_key(title)
            if tk and tk in seen_titles:
                skipped["dup_title"] += 1
                seen_urls.add(norm)
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
            seen_urls.add(norm)
            if tk:
                seen_titles.add(tk)

    if not candidates:
        LOG.info("No candidates collected. Skipped=%s", skipped)
        return 0

    LOG.info("Phase 1: collected=%d candidates (skipped=%s)", len(candidates), skipped)

    # Phase 2: Directus URL dedup (leads only)
    existing_norm = fetch_existing_norm_urls(candidates, hours=DIRECTUS_URL_DUP_HOURS)
    if existing_norm:
        before = len(candidates)
        candidates = [c for c in candidates if c.norm_url not in existing_norm]
        LOG.info("Phase 2: directus URL dedup removed=%d, remaining=%d", before - len(candidates), len(candidates))
    else:
        LOG.info("Phase 2: directus URL dedup found=0 (or query skipped). Remaining=%d", len(candidates))

    if not candidates:
        LOG.info("No candidates left after Directus URL dedup.")
        return 0

    # Phase 3: LLM ranking
    candidates_for_llm = _trim_candidates_for_llm(candidates, feeds_count=len(feeds))
    existing_titles = get_recent_titles(hours=TITLE_DUP_HOURS)
    ranked_idx = llm_rank_candidates(
        candidates_for_llm,
        existing_titles,
        max_return=min(RANK_RETURN_LIMIT, len(candidates_for_llm)),
    )
    ranked = [candidates_for_llm[i] for i in ranked_idx] if ranked_idx else candidates_for_llm
    LOG.info("Phase 3: ranked=%d (from %d candidates sent to LLM)", len(ranked), len(candidates_for_llm))

    # Decide how many to categorize.
    # We need buffer because category caps may skip some; but too many increases prompt size.
    target_posts = MAX_POSTS_PER_RUN if MAX_POSTS_PER_RUN > 0 else 120
    categorize_n = min(len(ranked), max(target_posts * 4, target_posts, 60))
    to_categorize = ranked[:categorize_n]

    # Phase 4: Category mapping (batched)
    cat_map = llm_map_categories(to_categorize, categories)
    LOG.info("Phase 4: category_mapped=%d/%d", len(cat_map), len(to_categorize))

    if not cat_map:
        LOG.warning("No category mapping produced; skipping all (as requested).")
        return 0

    # Phase 5: create leads + Slack (caps)
    picked_per_cat: Dict[str, int] = {cid: 0 for cid in valid_cat_ids}
    created = 0
    created_by_cat: Dict[str, int] = {}
    cat_name_by_id = {str(c.get("id") or ""): (c.get("name") or "") for c in categories}

    # iterate in ranked order, but only across categorized items (no fallback)
    for idx, cand in enumerate(to_categorize):
        if MAX_POSTS_PER_RUN and created >= MAX_POSTS_PER_RUN:
            break

        cid = cat_map.get(idx)
        if not cid:
            continue  # no category => skip (requested)
        if cid not in valid_cat_ids:
            continue

        cap = int(per_cat_cap.get(cid, 0) or 0)
        if cap <= 0:
            continue
        if picked_per_cat.get(cid, 0) >= cap:
            continue

        try:
            lead_id = create_lead(title=cand.title, source_url=cand.link, category_id=cid)
        except Exception as exc:
            LOG.error("Create lead failed: %s", exc)
            continue

        try:
            slack_post_lead(title=cand.title, category_name=(cat_name_by_id.get(cid) or ""), lead_id=lead_id)
        except Exception as exc:
            LOG.error("Slack post failed (lead_id=%s): %s", lead_id, exc)

        picked_per_cat[cid] = picked_per_cat.get(cid, 0) + 1
        created_by_cat[cid] = created_by_cat.get(cid, 0) + 1
        created += 1

    if created_by_cat:
        parts = [f"{cat_name_by_id.get(cid, cid)}={cnt}" for cid, cnt in sorted(created_by_cat.items(), key=lambda x: -x[1])]
        LOG.info("Scout complete: created=%d (%s)", created, ", ".join(parts))
    else:
        LOG.info("Scout complete: created=0 (nothing passed caps/category mapping).")

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