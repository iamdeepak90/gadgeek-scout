import sys
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

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
    request_with_retry,
    OPENROUTER_CHAT_URL,
    directus_get,
)

# Single LLM call with one fallback (budget-friendly + good formatting compliance)
PRIMARY_MODEL  = "meta-llama/llama-3.1-8b-instruct"
FALLBACK_MODEL = "google/gemma-3-27b-it"

SNIPPET_CHARS = 300
ENTRIES_PER_FEED = 25
RECENT_HOURS = 168          # last 7 days
RECENT_LIMIT = 200          # how many prior leads to send as context

# To keep one LLM call reliable, don't send an unbounded number of candidates.
MAX_CANDIDATES_TO_LLM = 180

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}"
)


# ─────────────────────────────────────────────────────────────────────────────
# DIRECTUS: last 7 days leads (title + category id) for LLM context
# ─────────────────────────────────────────────────────────────────────────────

def _utc_cutoff_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")


def get_recent_leads_titles_with_category(
    categories_by_id: Dict[str, Dict[str, Any]],
    hours: int = RECENT_HOURS,
    limit: int = RECENT_LIMIT,
) -> List[str]:
    """
    Fetch recent items from news_leads for LLM context.
    Returns lines like: [CategoryName] Title
    """
    cutoff = _utc_cutoff_iso(hours)
    params = (
        f"/items/news_leads"
        f"?filter[date_created][_gte]={cutoff}"
        f"&fields=title,category"
        f"&sort=-date_created"
        f"&limit={limit}"
    )

    lines: List[str] = []
    try:
        data = directus_get(params)
        for item in (data.get("data") or []):
            title = (item.get("title") or "").strip()
            if not title:
                continue
            cat_id = str(item.get("category") or "").strip()
            cat_name = (categories_by_id.get(cat_id, {}) or {}).get("name") or "Unknown"
            lines.append(f"[{cat_name}] {title}")
    except Exception as exc:
        LOG.warning("Failed to fetch recent news_leads for LLM context: %s", exc)

    # de-dupe by title (case-insensitive) to keep prompt compact
    seen = set()
    uniq: List[str] = []
    for line in lines:
        title_part = line.split("] ", 1)[-1].strip().lower()
        if title_part and title_part not in seen:
            seen.add(title_part)
            uniq.append(line)
    return uniq


# ─────────────────────────────────────────────────────────────────────────────
# OPENROUTER + LLM (single call: dedup by same news + category mapping)
# ─────────────────────────────────────────────────────────────────────────────

def _cap(s: str, n: int) -> str:
    s = (s or "").strip()
    return s[:n] if len(s) > n else s


def _call_openrouter(messages: List[Dict[str, str]], model: str) -> str:
    key = get_setting("openrouter_api_key")
    if not key:
        raise RuntimeError("OpenRouter API key not configured.")

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://bot.gadgeek.in",
        "X-Title": "Gadgeek Tech News",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 900,
    }

    resp = request_with_retry(
        "POST",
        OPENROUTER_CHAT_URL,
        headers=headers,
        json_body=payload,
        timeout=60,
        max_attempts=2,
    )
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenRouter returned no choices: {data}")

    return ((choices[0].get("message") or {}).get("content") or "").strip()


def llm_dedup_and_map_categories(
    candidates: List[Dict[str, str]],
    recent_lines: List[str],
    categories: List[Dict[str, Any]],
) -> Dict[int, str]:
    """
    Returns mapping: {candidate_index: category_uuid} for KEPT candidates only.

    Dedup criterion: only "same news event".
    If unsure about category, LLM should omit that candidate (we skip it).
    """
    if not candidates:
        return {}

    valid_ids = {str(c.get("id") or "").strip() for c in categories if c.get("id")}
    cat_ref = "\n".join(
        f"{c.get('id')} | {(c.get('slug') or '').strip()} | {(c.get('name') or '').strip()}"
        for c in categories
        if c.get("id") and (c.get("name") or "").strip()
    )

    recent_block = "\n".join(f"- {line}" for line in (recent_lines[:RECENT_LIMIT] if recent_lines else []))

    cand_lines: List[str] = []
    for i, c in enumerate(candidates, start=1):
        title = _cap(c.get("title", ""), 200)
        snippet = _cap(c.get("snippet", ""), SNIPPET_CHARS)
        source = _cap(c.get("source", ""), 40)
        if snippet:
            cand_lines.append(f"{i}. {title} — {snippet} (source: {source})")
        else:
            cand_lines.append(f"{i}. {title} (source: {source})")

    system = (
        "You are a tech news deduplication and classification assistant.\n"
        "You must:\n"
        "1) Remove duplicates that refer to the SAME news event.\n"
        "2) For each remaining (non-duplicate) candidate, choose exactly ONE category UUID from the list.\n"
        "Do NOT filter by quality. Only deduplicate by 'same news'.\n"
        "Follow the required output format exactly."
    )

    user = (
        "CATEGORIES (UUID | slug | name):\n"
        f"{cat_ref}\n\n"
        "ALREADY POSTED (last 7 days):\n"
        f"{recent_block if recent_block else '- (none)'}\n\n"
        "CANDIDATES:\n"
        + "\n".join(cand_lines)
        + "\n\n"
        "RULES:\n"
        "- A duplicate means the SAME event/story, even if different outlet wording.\n"
        "- Not a duplicate if it is a different event/angle (e.g., leak vs launch, price drop vs review).\n"
        "- If a candidate matches an ALREADY POSTED story (same event), drop it.\n"
        "- Only output lines for NON-DUPLICATE candidates.\n"
        "- If you cannot confidently assign a category UUID, omit that candidate.\n\n"
        "OUTPUT FORMAT (strict):\n"
        "One line per kept candidate:\n"
        "n: UUID\n"
        "Example:\n"
        "2: 11111111-1111-1111-1111-111111111111\n"
        "7: 22222222-2222-2222-2222-222222222222\n"
        "Return ONLY these lines. No extra text."
    )

    last_exc: Optional[Exception] = None
    for model in (PRIMARY_MODEL, FALLBACK_MODEL):
        try:
            content = _call_openrouter(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                model=model,
            )

            mapping: Dict[int, str] = {}
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                m = re.match(r"^(\d{1,4})\s*:\s*(" + _UUID_RE.pattern + r")\s*$", line)
                if not m:
                    continue
                n = int(m.group(1))
                cid = m.group(2)
                if 1 <= n <= len(candidates) and cid in valid_ids:
                    mapping[n - 1] = cid

            if mapping:
                return mapping

            LOG.warning("LLM produced no valid kept mappings (model=%s).", model)

        except Exception as exc:
            last_exc = exc
            LOG.warning("LLM failed (model=%s): %s", model, exc)

    if last_exc:
        LOG.warning("LLM failed for both models; nothing will be posted this run.")
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# Candidate limiting (single LLM call safety)
# ─────────────────────────────────────────────────────────────────────────────

def _round_robin_limit(per_feed_items: Dict[str, List[Dict[str, str]]], limit: int) -> List[Dict[str, str]]:
    """
    Take items round-robin across feeds to keep list under a safe limit for one LLM call.
    """
    keys = list(per_feed_items.keys())
    idx = {k: 0 for k in keys}
    out: List[Dict[str, str]] = []
    added = True
    while len(out) < limit and added:
        added = False
        for k in keys:
            i = idx[k]
            if i < len(per_feed_items[k]) and len(out) < limit:
                out.append(per_feed_items[k][i])
                idx[k] = i + 1
                added = True
    return out


# ─────────────────────────────────────────────────────────────────────────────
# SCOUT
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

    if not get_setting("openrouter_api_key"):
        LOG.error("OpenRouter API key not set. Can't do semantic dedup/category mapping.")
        return 0

    categories_by_id = {str(c.get("id") or "").strip(): c for c in categories if c.get("id")}
    if not categories_by_id:
        LOG.error("No valid category IDs found.")
        return 0

    LOG.info("Scout started: feeds=%d, entries_per_feed=%d", len(feeds), ENTRIES_PER_FEED)

    # Phase 1 + 2: parse feeds, exact Directus skip by source_url, build candidates
    seen_links = set()
    per_feed_candidates: Dict[str, List[Dict[str, str]]] = {}
    stats = {"missing": 0, "directus_skip": 0, "parse_fail": 0, "kept": 0}

    for feed_cfg in feeds:
        feed_url = (feed_cfg.get("url") or "").strip()
        feed_name = (feed_cfg.get("name") or feed_cfg.get("title") or feed_url).strip() or "feed"

        try:
            parsed = parse_feed(feed_url)
        except Exception as exc:
            stats["parse_fail"] += 1
            LOG.warning("Feed parse failed: %s (%s)", feed_name, exc)
            continue

        entries = parsed.entries or []
        bucket: List[Dict[str, str]] = []

        for ent in entries[:ENTRIES_PER_FEED]:
            fields = extract_entry_fields(ent, feed_cfg)
            title = (fields.get("title") or "").strip()
            link = (fields.get("link") or "").strip()

            if not title or not link:
                stats["missing"] += 1
                continue

            if link in seen_links:
                continue
            seen_links.add(link)

            # Exact match skip against news_leads.source_url
            try:
                if lead_exists_by_url(link):
                    stats["directus_skip"] += 1
                    continue
            except Exception as exc:
                # Fail-closed to avoid accidental duplicates if Directus is down
                LOG.warning("Directus dedupe check failed; skipping item. (%s)", exc)
                continue

            desc = (fields.get("description") or "").strip()
            content = (fields.get("content") or "").strip()
            snippet = (desc if desc else content)[:SNIPPET_CHARS].strip()

            bucket.append(
                {
                    "title": title,
                    "link": link,
                    "snippet": snippet,
                    "source": feed_name,
                }
            )
            stats["kept"] += 1

        if bucket:
            per_feed_candidates[feed_name] = bucket

    if not per_feed_candidates:
        LOG.info("No new candidates after Directus skip. Stats=%s", stats)
        return 0

    # Flatten with safety limit
    candidates = _round_robin_limit(per_feed_candidates, MAX_CANDIDATES_TO_LLM)

    LOG.info("Candidates ready: %d (skipped_by_directus=%d)", len(candidates), stats["directus_skip"])

    # Phase 3: load last 7 days titles+category for context
    recent_lines = get_recent_leads_titles_with_category(categories_by_id, hours=RECENT_HOURS, limit=RECENT_LIMIT)

    # Phase 4: One LLM call for semantic dedup + category mapping (only kept items)
    keep_map = llm_dedup_and_map_categories(candidates, recent_lines, categories)
    if not keep_map:
        LOG.info("LLM kept nothing (or failed). Nothing to post.")
        return 0

    # Phase 5: create leads + Slack (no caps)
    created = 0
    for idx in sorted(keep_map.keys()):
        cat_id = keep_map[idx]
        c = candidates[idx]
        try:
            lead_id = create_lead(title=c["title"], source_url=c["link"], category_id=cat_id)
        except Exception as exc:
            LOG.error("Failed to create lead: %s", exc)
            continue

        try:
            cat_name = (categories_by_id.get(cat_id) or {}).get("name") or ""
            slack_post_lead(title=c["title"], category_name=cat_name, lead_id=lead_id)
        except Exception as exc:
            LOG.error("Failed to post lead %s to Slack: %s", lead_id, exc)

        created += 1

    LOG.info("Scout completed. Created %d leads.", created)
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