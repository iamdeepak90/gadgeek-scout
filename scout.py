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
    OPENROUTER_CHAT_URL,
)

# ─────────────────────────────────────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────────────────────────────────────
# Kept same as your original code to avoid bigger changes.
LLM_PRIMARY_MODEL  = "meta-llama/llama-3.3-70b-instruct:free"
LLM_FALLBACK_MODEL = "google/gemma-3-27b-it:free"

# Batch size for category classification (hardcoded to keep config minimal)
_BATCH_SIZE = 20


# ─────────────────────────────────────────────────────────────────────────────
# LLM CATEGORY MATCHING (BATCHED)
# ─────────────────────────────────────────────────────────────────────────────

def _build_category_list(categories: List[Dict[str, Any]]) -> str:
    """
    Token-lean category reference for LLM prompt.
    Format: slug1, slug2, slug3, ...
    """
    slugs: List[str] = []
    for c in categories:
        slug = (c.get("slug") or "").strip().lower()
        if slug:
            slugs.append(slug)
    slugs = sorted(set(slugs))
    return ", ".join(slugs)


def _build_article_snippet(fields: Dict[str, str]) -> str:
    """
    Keep snippet compact to reduce tokens.
    Uses description (preferred) else content. Caps at 160 chars.
    """
    title       = (fields.get("title") or "").strip()
    description = (fields.get("description") or "").strip()
    content     = (fields.get("content") or "").strip()
    rss_cat     = (fields.get("category") or "").strip()

    snippet_src = description if description else content
    snippet = (snippet_src or "").strip()
    if len(snippet) > 160:
        snippet = snippet[:160] + "…"

    out = title
    if snippet:
        out += f"\n{snippet}"
    if rss_cat:
        out += f"\nRSS:{rss_cat[:40]}"
    return out.strip()


def _call_llm_batch(articles_block: str, category_list: str, model: str) -> str:
    """
    Single LLM call to OpenRouter for a batch. Returns raw text.
    """
    key = get_setting("openrouter_api_key")
    if not key:
        raise RuntimeError("OpenRouter API key not configured.")

    system = (
        "Pick the best category slug for each article from the provided slug list.\n"
        "Rules:\n"
        "- Output exactly one line per article: n: slug OR n: SKIP\n"
        "- slug MUST be one of the provided slugs.\n"
        "- If you cannot confidently pick a slug, output SKIP for that item.\n"
        "- No extra text."
    )

    user = (
        f"AVAILABLE SLUGS:\n{category_list}\n\n"
        f"ARTICLES:\n{articles_block}\n\n"
        "Return one line per article as: n: slug OR n: SKIP"
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
        "max_tokens":  220,
    }

    resp = request_with_retry(
        "POST", OPENROUTER_CHAT_URL,
        headers=headers,
        json_body=payload,
        timeout=45,
        max_attempts=2,
    )
    data    = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenRouter returned no choices: {data}")

    return ((choices[0].get("message") or {}).get("content") or "").strip()


def _resolve_slug(raw: str, categories: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    slug = raw.strip().strip('"').strip("'").strip(".").lower()
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    if not slug:
        return None
    for c in categories:
        if (c.get("slug") or "").strip().lower() == slug:
            return c
    return None


def llm_match_categories_batch(
    fields_list: List[Dict[str, str]],
    categories: List[Dict[str, Any]],
) -> List[Optional[Dict[str, Any]]]:
    """
    Batch classify many articles into categories via LLM.

    Returns list aligned to fields_list: matched category dict or None.
    SKIP/invalid/missing => None.
    """
    if not fields_list:
        return []

    category_list = _build_category_list(categories)

    snippets = [_build_article_snippet(f) for f in fields_list]
    articles_block = "\n\n".join(f"{i+1}. {snip}" for i, snip in enumerate(snippets))

    last_exc: Optional[Exception] = None
    raw_out: Optional[str] = None
    for model in (LLM_PRIMARY_MODEL, LLM_FALLBACK_MODEL):
        try:
            raw_out = _call_llm_batch(articles_block, category_list, model)
            break
        except Exception as exc:
            last_exc = exc
            LOG.warning("LLM batch [%s] failed: %s — trying next model.", model, exc)

    if raw_out is None:
        LOG.warning("All LLM models failed for batch; skipping all items.")
        return [None] * len(fields_list)

    results: List[Optional[Dict[str, Any]]] = [None] * len(fields_list)
    valid_slugs = {((c.get("slug") or "").strip().lower()) for c in categories if (c.get("slug") or "").strip()}

    for line in raw_out.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d{1,3})\s*:\s*([A-Za-z0-9-]+)\s*$", line)
        if not m:
            continue
        idx = int(m.group(1)) - 1
        if idx < 0 or idx >= len(fields_list):
            continue

        slug = m.group(2).strip().lower()
        if slug == "skip":
            results[idx] = None
            continue
        if slug not in valid_slugs:
            results[idx] = None
            continue
        results[idx] = _resolve_slug(slug, categories)

    return results


# ─────────────────────────────────────────────────────────────────────────────
# SCOUT (MINIMAL CHANGES)
# ─────────────────────────────────────────────────────────────────────────────

def _process_batch(
    fields_batch: List[Dict[str, str]],
    meta_batch: List[Tuple[str, str]],
    categories: List[Dict[str, Any]],
    per_cat_cap: Dict[str, int],
    picked_per_cat: Dict[str, int],
) -> int:
    """
    Process one batch:
    - Run LLM category match in one call
    - If LLM returns no valid category => SKIP (requested)
    - Apply category caps (existing behavior)
    - Create lead + post to Slack
    """
    matched_list = llm_match_categories_batch(fields_batch, categories)
    created = 0

    for (title, link), matched in zip(meta_batch, matched_list):
        if not matched:
            continue  # ← skip if LLM didn't return a valid category

        cat_id = str(matched.get("id") or "")
        cat_name = matched.get("name") or ""

        if per_cat_cap.get(cat_id, 0) <= 0:
            continue
        if picked_per_cat.get(cat_id, 0) >= per_cat_cap.get(cat_id, 0):
            continue

        try:
            lead_id = create_lead(title=title, source_url=link, category_id=cat_id)
        except Exception as e:
            LOG.error("Failed to create lead: %s", e)
            continue

        try:
            slack_post_lead(title=title, category_name=cat_name, lead_id=lead_id)
        except Exception as e:
            LOG.error("Failed to post lead %s to Slack: %s", lead_id, e)

        picked_per_cat[cat_id] = picked_per_cat.get(cat_id, 0) + 1
        created += 1

    return created


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
        LOG.warning("OpenRouter API key not set. Skipping all items (category required).")
        return 0

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

        # Collect NOT-in-Directus items, then classify in batch
        pending_fields: List[Dict[str, str]] = []
        pending_meta: List[Tuple[str, str]] = []

        for ent in entries[:50]:
            fields = extract_entry_fields(ent, feed_cfg)
            title  = (fields.get("title") or "").strip()
            link   = (fields.get("link") or "").strip()

            if not title or not link:
                continue

            # Dedup FIRST (as requested)
            try:
                if lead_exists_by_url(link):
                    continue
            except Exception as e:
                LOG.error("Directus dedupe check failed: %s", e)
                continue

            pending_fields.append(fields)
            pending_meta.append((title, link))

            if len(pending_fields) >= _BATCH_SIZE:
                created += _process_batch(pending_fields, pending_meta, categories, per_cat_cap, picked_per_cat)
                pending_fields = []
                pending_meta = []

        if pending_fields:
            created += _process_batch(pending_fields, pending_meta, categories, per_cat_cap, picked_per_cat)

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