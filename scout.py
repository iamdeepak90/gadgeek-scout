import sys
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

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
    DEFAULT_CATEGORY_UUID,
    OPENROUTER_CHAT_URL,
)

# ─────────────────────────────────────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────────────────────────────────────

LLM_PRIMARY_MODEL  = "meta-llama/llama-3.3-70b-instruct:free"
LLM_FALLBACK_MODEL = "google/gemma-3-27b-it:free"


# ─────────────────────────────────────────────────────────────────────────────
# LLM CATEGORY MATCHING
# ─────────────────────────────────────────────────────────────────────────────

def _build_category_list(categories: List[Dict[str, Any]]) -> str:
    """
    Build category reference for LLM prompt.
    Format: slug | name  (one per line)
    """
    lines = []
    for c in categories:
        slug = (c.get("slug") or "").strip()
        name = (c.get("name") or "").strip()
        if slug and name:
            lines.append(f"{slug} | {name}")
    return "\n".join(lines)


def _build_article_snippet(fields: Dict[str, str]) -> str:
    title       = (fields.get("title") or "").strip()
    description = (fields.get("description") or "").strip()
    content     = (fields.get("content") or "").strip()
    rss_cat     = (fields.get("category") or "").strip()

    parts = []

    if title:
        parts.append(f"Title: {title}")

    # CAP BOTH at 200 chars — this is the fix
    if len(description) >= 50:
        parts.append(f"Description: {description[:200]}")   # ← was no cap
    elif content:
        parts.append(f"Content: {content[:200]}")
    elif description:
        parts.append(f"Description: {description[:200]}")   # ← was no cap

    # RSS category is short, no cap needed
    if rss_cat:
        parts.append(f"RSS category: {rss_cat[:60]}")

    return "\n".join(parts)


def _call_llm(article_snippet: str, category_list: str, model: str) -> str:
    """
    Single LLM call to OpenRouter. Returns raw text response.
    Raises on failure — caller handles retry and fallback.
    """
    key = get_setting("openrouter_api_key")
    if not key:
        raise RuntimeError("OpenRouter API key not configured.")

    system = (
        "You are a content classification assistant for a tech news website. "
        "Your only job: pick the single best matching category for the article "
        "from the provided list.\n\n"
        "Rules:\n"
        "- Return ONLY the slug. Nothing else.\n"
        "- No explanation. No punctuation. No quotes.\n"
        "- The slug must exactly match one from the list.\n"
        "- If nothing fits well, return the slug of the most general category."
    )

    user = (
        f"ARTICLE:\n{article_snippet}\n\n"
        f"AVAILABLE CATEGORIES (slug | name):\n{category_list}\n\n"
        "Return only the slug of the best matching category:"
    )

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://bot.gadgeek.in",
        "X-Title": "Gadgeek Tech News",
    }
    payload = {
        "model":       model,
        "messages":    [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "temperature": 0.0,
        "max_tokens":  20,
    }

    resp = request_with_retry(
        "POST", OPENROUTER_CHAT_URL,
        headers=headers,
        json_body=payload,
        timeout=30,
        max_attempts=2,
    )
    data    = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenRouter returned no choices: {data}")

    return ((choices[0].get("message") or {}).get("content") or "").strip()


def _resolve_slug(raw: str, categories: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Clean LLM response and match to a known category.
    Strips quotes, punctuation, whitespace the model might add.
    Returns matched category dict or None.
    """
    slug = raw.strip().strip('"').strip("'").strip(".").lower()
    slug = re.sub(r"[^a-z0-9-]", "", slug)

    if not slug:
        return None

    for c in categories:
        if (c.get("slug") or "").strip().lower() == slug:
            return c

    return None


def llm_match_category(
    fields: Dict[str, str],
    categories: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Classify article into best Directus category using LLM.

    Flow:
      1. Try Llama 3.1 8B (primary)
      2. Unknown slug or failure → try Llama 3.2 3B (fallback)
      3. Both fail → return None (caller uses DEFAULT_CATEGORY_UUID)
    """
    article_snippet = _build_article_snippet(fields)
    category_list   = _build_category_list(categories)

    if not article_snippet.strip():
        LOG.warning("LLM match skipped — article snippet is empty.")
        return None

    for model in (LLM_PRIMARY_MODEL, LLM_FALLBACK_MODEL):
        try:
            raw = _call_llm(article_snippet, category_list, model)
            LOG.debug("LLM [%s] raw response: %r", model, raw)

            matched = _resolve_slug(raw, categories)
            if matched:
                LOG.info(
                    "LLM matched '%s' (slug: %s) via %s",
                    matched.get("name"), matched.get("slug"), model,
                )
                return matched

            LOG.warning(
                "LLM [%s] returned unknown slug %r — trying next model.", model, raw,
            )

        except Exception as exc:
            LOG.warning("LLM [%s] failed: %s — trying next model.", model, exc)

    LOG.warning("All LLM models failed. Will use default category.")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# CATEGORY RESOLVER
# ─────────────────────────────────────────────────────────────────────────────

def resolve_category(
    fields: Dict[str, str],
    categories: List[Dict[str, Any]],
) -> Tuple[str, str]:
    """
    Resolve best category for an article.

    Chain:
      1. LLM (Llama 3.1 8B → Llama 3.2 3B fallback)
      2. DEFAULT_CATEGORY_UUID if both LLM models fail

    Returns (category_id, category_name).
    """
    matched = llm_match_category(fields, categories)
    if matched:
        return str(matched.get("id") or ""), matched.get("name") or ""

    # Both LLM models failed — use default
    for c in categories:
        if str(c.get("id") or "") == DEFAULT_CATEGORY_UUID:
            return DEFAULT_CATEGORY_UUID, c.get("name") or "General"

    return DEFAULT_CATEGORY_UUID, "General"


# ─────────────────────────────────────────────────────────────────────────────
# URL NORMALIZATION
# ─────────────────────────────────────────────────────────────────────────────

STRIP_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "fbclid", "gclid", "ref", "source", "ncid", "ocid",
    "sr_share", "dicbo", "soc_src", "soc_trk",
}


def normalize_url(url: str) -> str:
    """Normalize URL: lowercase, strip tracking params, www, trailing slash, fragment."""
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


# ─────────────────────────────────────────────────────────────────────────────
# LAYER 1: URL DEDUP (checks both news_leads AND articles)
# ─────────────────────────────────────────────────────────────────────────────

def url_exists_in_leads(url: str) -> bool:
    """Check if URL exists in news_leads table (raw + normalized)."""
    norm = normalize_url(url)
    urls_to_check = [url, norm] if norm != url else [url]

    for check_url in urls_to_check:
        try:
            params = urlencode({
                "filter[source_url][_eq]": check_url,
                "fields": "id",
                "limit": 1,
            })
            data = directus_get(f"/items/news_leads?{params}")
            if data and (data.get("data") or []):
                return True
        except Exception as e:
            LOG.debug("Lead URL check failed for %s: %s", check_url, e)

    return False


def url_exists_in_articles(url: str) -> bool:
    """Check if URL exists in articles table (raw + normalized)."""
    col = articles_collection()
    norm = normalize_url(url)
    urls_to_check = [url, norm] if norm != url else [url]

    for check_url in urls_to_check:
        try:
            params = urlencode({
                "filter[source_url][_eq]": check_url,
                "fields": "id",
                "limit": 1,
            })
            data = directus_get(f"/items/{col}?{params}")
            if data and (data.get("data") or []):
                return True
        except Exception as e:
            LOG.debug("Article URL check failed for %s: %s", check_url, e)

    return False


def url_exists_anywhere(url: str) -> bool:
    """Check if URL exists in either news_leads OR articles."""
    if url_exists_in_leads(url):
        LOG.debug("URL found in news_leads: %s", url[:80])
        return True
    if url_exists_in_articles(url):
        LOG.debug("URL found in articles: %s", url[:80])
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# LAYER 2: LLM SEMANTIC DEDUP + QUALITY FILTER
# ─────────────────────────────────────────────────────────────────────────────

def get_recent_titles(hours: int = 168) -> list:
    """
    Fetch titles from BOTH news_leads AND articles from last N hours.
    Default: 168 hours = 7 days.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    titles = []

    # ── From news_leads ─────────────────────────────────────────────
    try:
        params = urlencode({
            "filter[date_created][_gte]": cutoff,
            "fields": "title",
            "limit": 300,
            "sort": "-date_created",
        })
        data = directus_get(f"/items/news_leads?{params}")
        for item in (data.get("data") or []):
            t = (item.get("title") or "").strip()
            if t:
                titles.append(t)
        LOG.info("Loaded %d titles from news_leads (last %d hours).", len(titles), hours)
    except Exception as e:
        LOG.warning("Failed to fetch recent lead titles: %s", e)

    # ── From articles ───────────────────────────────────────────────
    article_count = 0
    try:
        col = articles_collection()
        params = urlencode({
            "filter[date_created][_gte]": cutoff,
            "fields": "title",
            "limit": 300,
            "sort": "-date_created",
        })
        data = directus_get(f"/items/{col}?{params}")
        for item in (data.get("data") or []):
            t = (item.get("title") or "").strip()
            if t:
                titles.append(t)
                article_count += 1
        LOG.info("Loaded %d titles from articles (last %d hours).", article_count, hours)
    except Exception as e:
        LOG.warning("Failed to fetch recent article titles: %s", e)

    # ── Deduplicate titles ──────────────────────────────────────────
    seen = set()
    unique_titles = []
    for t in titles:
        t_lower = t.lower().strip()
        if t_lower not in seen:
            seen.add(t_lower)
            unique_titles.append(t)

    LOG.info("Total unique existing titles for dedup: %d", len(unique_titles))
    return unique_titles


def llm_filter_leads(candidates: list, existing_titles: list, max_picks: int = 15) -> list:
    """
    Single LLM call that does BOTH:
    1. Semantic dedup against existing titles (news_leads + articles)
    2. Quality/relevance filtering

    Uses free model. Falls back gracefully if LLM is unavailable.
    """
    if not candidates:
        return []

    api_key = get_setting("openrouter_api_key")
    if not api_key:
        LOG.warning("No OpenRouter key — skipping LLM filter, returning first %d", max_picks)
        return candidates[:max_picks]

    # ── Build the prompt ────────────────────────────────────────────
    existing_section = ""
    if existing_titles:
        existing_list = "\n".join(f"  {i+1}. {t}" for i, t in enumerate(existing_titles[:150]))
        existing_section = (
            f"SECTION A — ALREADY PUBLISHED/SCOUTED ({len(existing_titles[:150])} titles from last 7 days):\n"
            f"{existing_list}\n\n"
        )

    candidates_list = "\n".join(
        f"  {i+1}. [{c['cat_name']}] {c['title']}"
        for i, c in enumerate(candidates)
    )

    prompt = f"""You are an editor for an Indian technology news website called Gadgeek.

    {existing_section}SECTION B — NEW CANDIDATES FROM RSS FEEDS ({len(candidates)} articles):
    {candidates_list}

    YOUR TASK:
    From Section B, pick up to {max_picks} articles to publish. Return ONLY their numbers as comma-separated values.

    DUPLICATE RULES (very important):
    - DUPLICATE (skip): Same news EVENT reported by different sources.
    Example: "iPhone 17e Launched at ₹49,999" and "Apple iPhone 17e India Price ₹49,999" = SAME event → keep only the better title
    - NOT DUPLICATE (keep both): Same PRODUCT but different news event.
    Example: "iPhone 17e Specs Leaked" and "iPhone 17e Officially Launched" = DIFFERENT events → keep both
    - NOT DUPLICATE (keep both): Follow-up stories.
    Example: "iPhone 17e Launched" and "iPhone 17e First Sale: 100K Units Sold" = follow-up → keep both

    CHECK AGAINST SECTION A:
    - If a candidate covers the SAME news event as any title in Section A → SKIP it (already covered)
    - If a candidate covers the same product but a NEW angle/event → KEEP it

    QUALITY RULES:
    - Prioritize: breaking news, product launches, major updates, Indian market relevance
    - Skip: PR fluff, generic listicles, sponsored content, outdated news
    - Skip: Vague opinion pieces with no new information

    OUTPUT FORMAT:
    Return ONLY comma-separated numbers from Section B. Example: 2,5,8,11,14
    Do not explain. Do not add any other text."""

    FREE_MODEL = "meta-llama/llama-4-scout"

    try:
        routes = get_model_routes()
        scout_route = routes.get("scout_filter") or {}
        model = scout_route.get("model") or FREE_MODEL

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 200,
            "temperature": 0.1,
        }

        LOG.info(
            "LLM filter: %d candidates, %d existing titles, model: %s",
            len(candidates), len(existing_titles[:150]), model,
        )

        resp = request_with_retry(
            "POST", OPENROUTER_CHAT_URL,
            headers=headers, json_body=payload,
            timeout=60, max_attempts=2,
        )
        data = resp.json()

        if data.get("error"):
            LOG.warning("LLM filter API error: %s", data["error"])
            return candidates[:max_picks]

        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        LOG.info("LLM filter response: %s", content[:300])

        # Parse numbers
        picked_nums = set()
        for token in re.findall(r'\d+', content):
            num = int(token)
            if 1 <= num <= len(candidates):
                picked_nums.add(num)

        if picked_nums:
            filtered = [candidates[n - 1] for n in sorted(picked_nums)]
            LOG.info(
                "LLM filter: picked %d/%d — %s",
                len(filtered), len(candidates),
                [c["title"][:50] for c in filtered],
            )
            return filtered
        else:
            LOG.warning("LLM returned no valid picks. Raw: %s", content[:300])
            return candidates[:max_picks]

    except Exception as e:
        LOG.warning("LLM filter failed: %s — returning first %d", e, max_picks)
        return candidates[:max_picks]


# ─────────────────────────────────────────────────────────────────────────────
# MAIN SCOUT FUNCTION
# ─────────────────────────────────────────────────────────────────────────────

GLOBAL_SCOUT_CAP = 25


def scout_once() -> int:
    init_db()

    feeds = [f for f in list_feeds() if f.get("enabled")]
    if not feeds:
        LOG.error("No RSS feeds configured. Go to /settings -> RSS Feeds.")
        return 0

    categories = get_categories()
    if not categories:
        LOG.error("No enabled categories found in Directus.")
        return 0

    if not get_setting("openrouter_api_key"):
        LOG.warning("OpenRouter API key not set. LLM filter will be skipped.")

    per_cat_cap = {
        str(c.get("id") or ""): int(c.get("posts_per_scout", 0))
        for c in categories
    }

    # ── Phase 1: Collect candidates (URL dedup only) ────────────────────
    LOG.info("=" * 60)
    LOG.info("PHASE 1: Collecting candidates from %d feeds...", len(feeds))

    candidates = []
    seen_urls = set()
    skip_stats = {"no_title_link": 0, "url_mem": 0, "url_leads": 0, "url_articles": 0, "no_cap": 0}

    for feed_cfg in feeds:
        url = feed_cfg["url"]
        try:
            parsed = parse_feed(url)
        except Exception as e:
            LOG.warning("Failed to parse feed %s: %s", url, e)
            continue

        entries = parsed.entries or []

        for ent in entries[:30]:
            fields = extract_entry_fields(ent, feed_cfg)
            title = fields.get("title") or ""
            link = fields.get("link") or ""

            if not title or not link:
                skip_stats["no_title_link"] += 1
                continue

            # ── URL dedup: in-memory (this run) ─────────────────────
            norm_url = normalize_url(link)
            if norm_url in seen_urls:
                skip_stats["url_mem"] += 1
                continue

            # ── URL dedup: news_leads table (all time) ──────────────
            if url_exists_in_leads(link):
                skip_stats["url_leads"] += 1
                seen_urls.add(norm_url)
                continue

            # ── URL dedup: articles table (all time) ────────────────
            if url_exists_in_articles(link):
                skip_stats["url_articles"] += 1
                seen_urls.add(norm_url)
                continue

            # ── Category check ──────────────────────────────────────
            cat_id, cat_name = resolve_category(fields, categories)
            if per_cat_cap.get(cat_id, 0) <= 0:
                skip_stats["no_cap"] += 1
                continue

            # ── Passed URL dedup → add to candidates ────────────────
            candidates.append({
                "title": title,
                "link": link,
                "norm_url": norm_url,
                "cat_id": cat_id,
                "cat_name": cat_name,
            })
            seen_urls.add(norm_url)

    LOG.info(
        "Phase 1 complete: %d candidates. Skipped — no_title_link: %d, "
        "url_mem: %d, url_leads: %d, url_articles: %d, no_cap: %d",
        len(candidates),
        skip_stats["no_title_link"], skip_stats["url_mem"],
        skip_stats["url_leads"], skip_stats["url_articles"],
        skip_stats["no_cap"],
    )

    if not candidates:
        LOG.info("No new candidates found. Scout complete.")
        LOG.info("=" * 60)
        return 0

    # ── Phase 2: LLM semantic dedup + quality filter ────────────────────
    LOG.info("PHASE 2: LLM semantic dedup + quality filter...")

    existing_titles = get_recent_titles(hours=168)  # 7 days
    filtered = llm_filter_leads(candidates, existing_titles, max_picks=GLOBAL_SCOUT_CAP)

    LOG.info("Phase 2 complete: %d candidates after LLM filter.", len(filtered))

    # ── Phase 3: Create leads with caps ─────────────────────────────────
    LOG.info("PHASE 3: Creating leads and posting to Slack...")

    picked_per_cat = {str(c.get("id") or ""): 0 for c in categories}
    created = 0

    for cand in filtered:
        cat_id = cand["cat_id"]

        # Per-category cap
        if picked_per_cat.get(cat_id, 0) >= per_cat_cap.get(cat_id, 0):
            LOG.debug("Category cap reached for %s. Skipping: %s", cand["cat_name"], cand["title"][:60])
            continue

        # Global cap
        if created >= GLOBAL_SCOUT_CAP:
            LOG.info("Global cap (%d) reached. Stopping.", GLOBAL_SCOUT_CAP)
            break

        # Create lead in Directus
        try:
            lead_id = create_lead(title=cand["title"], source_url=cand["link"], category_id=cat_id)
        except Exception as e:
            LOG.error("Failed to create lead: %s", e)
            continue

        # Post to Slack
        try:
            slack_post_lead(title=cand["title"], category_name=cand["cat_name"], lead_id=lead_id)
        except Exception as e:
            LOG.error("Failed to post lead %s to Slack: %s", lead_id, e)

        picked_per_cat[cat_id] = picked_per_cat.get(cat_id, 0) + 1
        created += 1

    LOG.info(
        "Phase 3 complete. Created %d leads (from %d filtered, %d total candidates).",
        created, len(filtered), len(candidates),
    )
    LOG.info("=" * 60)
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