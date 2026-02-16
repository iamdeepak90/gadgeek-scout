import sys
import re
from typing import Any, Dict, List, Optional, Tuple

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

LLM_PRIMARY_MODEL  = "meta-llama/llama-3.1-8b-instruct"
LLM_FALLBACK_MODEL = "meta-llama/llama-3.2-3b-instruct"


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
    """
    Build article context for LLM.
    Always sends title + description.
    Adds first 500 chars of content if description is missing or under 50 chars.
    Includes RSS category tag if present.
    """
    title       = (fields.get("title") or "").strip()
    description = (fields.get("description") or "").strip()
    content     = (fields.get("content") or "").strip()
    rss_cat     = (fields.get("category") or "").strip()

    parts = []

    if title:
        parts.append(f"Title: {title}")

    if len(description) >= 50:
        parts.append(f"Description: {description}")
    elif content:
        parts.append(f"Content: {content[:500]}")
    elif description:
        parts.append(f"Description: {description}")

    if rss_cat:
        parts.append(f"RSS category: {rss_cat}")

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
# SCOUT
# ─────────────────────────────────────────────────────────────────────────────

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
        LOG.warning(
            "OpenRouter API key not set. "
            "All articles will fall back to default category."
        )

    per_cat_cap    = {str(c.get("id") or ""): int(c.get("posts_per_scout", 0)) for c in categories}
    picked_per_cat = {str(c.get("id") or ""): 0 for c in categories}

    created = 0

    for feed_cfg in feeds:
        url = feed_cfg["url"]

        try:
            parsed = parse_feed(url)
        except Exception as e:
            LOG.warning("Failed to parse feed %s: %s", url, e)
            continue

        entries = parsed.entries or []

        for ent in entries[:50]:
            fields = extract_entry_fields(ent, feed_cfg)
            title  = fields.get("title") or ""
            link   = fields.get("link") or ""

            if not title or not link:
                continue

            # ── Category resolution ─────────────────────────────────────────
            cat_id, cat_name = resolve_category(fields, categories)

            # ── posts_per_scout cap ─────────────────────────────────────────
            if per_cat_cap.get(cat_id, 0) <= 0:
                continue
            if picked_per_cat.get(cat_id, 0) >= per_cat_cap.get(cat_id, 0):
                continue

            # ── Dedup ───────────────────────────────────────────────────────
            try:
                if lead_exists_by_url(link):
                    continue
            except Exception as e:
                LOG.error("Directus dedupe check failed: %s", e)
                continue

            # ── Create lead ─────────────────────────────────────────────────
            try:
                lead_id = create_lead(title=title, source_url=link, category_id=cat_id)
            except Exception as e:
                LOG.error("Failed to create lead: %s", e)
                continue

            # ── Post to Slack ───────────────────────────────────────────────
            try:
                slack_post_lead(title=title, category_name=cat_name, lead_id=lead_id)
            except Exception as e:
                LOG.error("Failed to post lead %s to Slack: %s", lead_id, e)

            picked_per_cat[cat_id] = picked_per_cat.get(cat_id, 0) + 1
            created += 1

    LOG.info("Scout completed. Created %d leads.", created)
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