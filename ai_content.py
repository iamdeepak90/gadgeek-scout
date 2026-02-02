"""
ai_content.py — AI steps (Fact Pack → Article Draft → Humanization/Polish → SEO pack)

IMPORTANT RULES (production + safety)
- Do NOT generate JSON-LD (per your request).
- Do NOT invent facts. All writing must be grounded in the fact pack (which is grounded in sources).
- Keep the strict layout you requested.

Models:
- Primary: Gemini 2.5 Flash-Lite (fact pack + polish + SEO)
- Primary: Gemini 2.5 Flash (long-form writing)
- Optional fallback: OpenAI (if configured)

"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

import config
from common import log, word_count


# ---------------------------
# Gemini (google-generativeai)
# ---------------------------

def _gemini_client():
    try:
        import google.generativeai as genai
        genai.configure(api_key=config.GEMINI_API_KEY)
        return genai
    except Exception as e:
        raise RuntimeError("google-generativeai not installed or GEMINI_API_KEY missing") from e

def gemini_generate(
    prompt: str,
    *,
    model: str,
    temperature: float,
    max_output_tokens: int = 4096,
    response_mime_type: str | None = None,
) -> str:
    genai = _gemini_client()
    m = genai.GenerativeModel(model)
    # Keep it simple & stable
    gen_cfg = {
        "temperature": float(temperature),
        "max_output_tokens": int(max_output_tokens),
    }
    # Newer google-generativeai versions support response_mime_type (e.g., application/json).
    # If unsupported, it will be ignored by the SDK.
    if response_mime_type:
        gen_cfg["response_mime_type"] = response_mime_type

    resp = m.generate_content(prompt, generation_config=gen_cfg)
    text = getattr(resp, "text", "") or ""
    return text.strip()


# ---------------------------
# OpenAI fallback (direct HTTP; optional)
# ---------------------------

def openai_generate(prompt: str, *, model: str, temperature: float, max_output_tokens: int = 4096) -> str:
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set")
    url = "https://api.openai.com/v1/responses"
    headers = {"Authorization": f"Bearer {config.OPENAI_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "input": [
            {"role": "system", "content": "You are a helpful, factual tech editor."},
            {"role": "user", "content": prompt},
        ],
        "temperature": float(temperature),
        "max_output_tokens": int(max_output_tokens),
    }
    r = requests.post(url, headers=headers, json=body, timeout=45)
    if r.status_code >= 300:
        raise RuntimeError(f"OpenAI failed: {r.status_code} {r.text[:300]}")
    data = r.json()
    # Response API format: take first output_text
    try:
        out = data["output"][0]["content"][0]["text"]
    except Exception:
        out = ""
    return (out or "").strip()


def llm_generate(
    prompt: str,
    *,
    primary_model: str,
    primary_temp: float,
    fallback_model: str = "",
    fallback_temp: float = 0.7,
    max_output_tokens: int = 4096,
    expect_json: bool = False,
) -> str:
    """Generate text from LLM.

    If expect_json=True, we ask Gemini SDK for application/json when supported.
    This dramatically reduces invalid JSON responses.
    """
    # Gemini primary
    try:
        return gemini_generate(
            prompt,
            model=primary_model,
            temperature=primary_temp,
            max_output_tokens=max_output_tokens,
            response_mime_type=("application/json" if expect_json else None),
        )
    except Exception as e:
        log.warning(f"Gemini failed ({primary_model}): {e}")
        if config.OPENAI_API_KEY and fallback_model:
            return openai_generate(prompt, model=fallback_model, temperature=fallback_temp, max_output_tokens=max_output_tokens)
        raise


# ---------------------------
# Prompts
# ---------------------------

def prompt_fact_pack(title: str, category_slug: str, sources: List[Dict[str,Any]]) -> str:
    # Provide sources as compact JSON
    src_compact = []
    for s in sources:
        src_compact.append({
            "url": s.get("url",""),
            "domain": s.get("domain",""),
            "title": s.get("title",""),
            "text": (s.get("text","") or "")[:12000],  # cap to avoid huge prompts
        })
    payload = json.dumps(src_compact, ensure_ascii=False)

    return f"""
You are a careful tech researcher and editor.

TASK
Build a structured "Fact Pack" for the topic below, grounded ONLY in the provided sources.
Do not invent any facts. If something isn't in sources, mark it "unknown".

TOPIC
Title: {title}
Category slug: {category_slug}

SOURCES (JSON, each has url/domain/title/text):
{payload}

OUTPUT FORMAT (valid JSON only; no markdown):
{{
  "topic": "...",
  "category": "{category_slug}",
  "one_sentence_summary": "...",
  "highlights": ["...", "...", "..."],
  "key_facts": [{{"fact":"...", "sources":["url1","url2"]}}],
  "numbers_and_specs": [{{"label":"...", "value":"...", "sources":["url"]}}],
  "timeline": [{{"when":"...", "what":"...", "sources":["url"]}}],
  "confirmed": ["..."],
  "uncertain_or_conflicting": [{{"claim":"...", "notes":"...", "sources":["url1","url2"]}}],
  "what_it_means": ["...", "..."],
  "reader_takeaways": ["...", "..."],
  "seo": {{
    "primary_keyword": "...",
    "supporting_keywords": ["...", "...", "..."],
    "entities": ["...", "..."]
  }},
  "recommended_table": {{
    "title": "...",
    "columns": ["...", "...", "..."],
    "rows": [["...", "...", "..."]]
  }}
}}
""".strip()


def prompt_article(fact_pack: Dict[str,Any]) -> str:
    fp = json.dumps(fact_pack, ensure_ascii=False)

    return f"""
You are an experienced human tech journalist and editor.

RULES (must follow)
- Use ONLY the facts from the Fact Pack. Do NOT add new facts.
- Write an original, reader-friendly article (not a rewrite of any one source).
- Target length: {config.ARTICLE_WORD_TARGET_MIN}-{config.ARTICLE_WORD_TARGET_MAX} words.
- Keep paragraphs short (2–3 sentences).
- Use a neutral, professional newsroom tone.
- Avoid phrases like "as an AI" or "this article will".
- Do not include JSON-LD or schema markup.

STRUCTURE (exact)
1) H2: "Article Highlights" + 2–3 bullet points
2) H2: "Hook" + {config.HOOK_WORDS_MIN}-{config.HOOK_WORDS_MAX} words
3) 4–5 H2 headings (each 100–200 words) with optional H3 subheadings when helpful
4) Use at least:
   - one numbered list
   - one bullet list (highlights already counts)
   - exactly ONE table (if Fact Pack provides a table, use it)
   - include an image placeholder: <figure><img src="{{IMAGE_URL}}" alt="{{IMAGE_ALT}}"/><figcaption>{{IMAGE_CAPTION}}</figcaption></figure>
5) End with H2: "Sources" and list the source URLs used (bullets)

OUTPUT
Return HTML only (no markdown, no code fences).
Use <h2>, <h3>, <p>, <ul>, <ol>, <table>, <figure> tags.

FACT PACK JSON:
{fp}
""".strip()


def prompt_polish(html: str, fact_pack: Dict[str,Any]) -> str:
    fp = json.dumps({
        "highlights": fact_pack.get("highlights", []),
        "key_facts": fact_pack.get("key_facts", []),
        "numbers_and_specs": fact_pack.get("numbers_and_specs", []),
        "seo": fact_pack.get("seo", {}),
    }, ensure_ascii=False)

    return f"""
You are a senior tech editor.

TASK
Polish the article HTML to make it feel human-edited and user-friendly, while preserving facts.

HARD RULES
- Do NOT add new facts, numbers, dates, quotes, or claims.
- Do NOT change any numbers/specs.
- Keep the same overall structure (Highlights, Hook, 4–5 H2 sections, Sources).
- Improve readability: shorter sentences, better transitions, remove repetition, more scannable.
- Keep table count <= {config.TABLE_MAX}.
- Do not add JSON-LD.

Return HTML only.

FACT CONSTRAINTS (do not go beyond these):
{fp}

ARTICLE HTML:
{html}
""".strip()


def prompt_seo_pack(html: str, fact_pack: Dict[str,Any]) -> str:
    fp = json.dumps(fact_pack.get("seo", {}), ensure_ascii=False)
    return f"""
You are an SEO editor for a tech news site.

TASK
Create SEO fields for the article. Do NOT output JSON-LD.

Return valid JSON only (no markdown), with:
{{
  "meta_title": "...",
  "meta_description": "...",
  "focus_keyword": "...",
  "tags": ["...", "...", "..."],
  "short_description": "One short paragraph summary (max 300 chars)",
  "image_alt": "SEO-friendly descriptive alt text",
  "image_caption": "Short caption (max 140 chars)"
}}

Constraints:
- meta_title 55–65 chars target
- meta_description 150–160 chars target
- tags 5–10
- Keep it human and accurate, based on the article and the SEO hints.

SEO HINTS JSON:
{fp}

ARTICLE HTML:
{html}
""".strip()


# ---------------------------
# JSON extraction helpers
# ---------------------------

def _strip_code_fences(t: str) -> str:
    t = (t or "").strip()
    # Remove common markdown code fences.
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"```\s*$", "", t)
    return t.strip()


def _extract_first_json_object(t: str) -> str | None:
    """Extract the first top-level JSON object using brace balancing.

    This is safer than regex because model outputs may contain extra braces in text.
    """
    if not t:
        return None
    start = None
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(t):
        if start is None:
            if ch == "{":
                start = i
                depth = 1
            continue

        # inside object
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                return t[start : i + 1]
    return None


def _basic_json_sanitize(t: str) -> str:
    """Best-effort sanitization for near-JSON produced by LLMs."""
    if not t:
        return t
    # Normalize fancy quotes
    t = t.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    t = t.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    # Remove trailing commas before } or ]
    t = re.sub(r",\s*([}\]])", r"\1", t)
    return t


def _repair_json_with_llm(bad_json: str) -> Dict[str, Any]:
    """Ask the LLM to repair JSON syntax.

    This is the most reliable fix for missing commas / unescaped characters.
    """
    prompt = (
        "You are a strict JSON repair tool.\n"
        "TASK: Fix the following so it becomes valid JSON.\n"
        "RULES:\n"
        "- Preserve the same keys and values (do NOT add new facts).\n"
        "- Only fix syntax issues (commas, quotes, escapes).\n"
        "- Output ONLY the repaired JSON object. No markdown, no commentary.\n\n"
        f"BROKEN JSON:\n{bad_json}"
    )
    out = llm_generate(
        prompt,
        primary_model=config.GEMINI_MODEL_FACTPACK,
        primary_temp=0.0,
        fallback_model=config.OPENAI_MODEL_WRITER,
        fallback_temp=0.0,
        max_output_tokens=4096,
        expect_json=True,
    )
    cleaned = _strip_code_fences(out)
    block = _extract_first_json_object(cleaned) or cleaned
    return json.loads(_basic_json_sanitize(block))


def extract_json_object(text: str) -> Dict[str,Any]:
    """Parse a JSON object from model output robustly.

    Strategy:
      1) direct json.loads
      2) extract first balanced {...} block
      3) sanitize common issues and retry
      4) if still failing, use an LLM repair pass (most reliable)
    """
    t = _strip_code_fences(text)
    if not t:
        raise ValueError("Empty model output")

    # 1) direct
    try:
        return json.loads(t)
    except Exception:
        pass

    # 2) first balanced object
    block = _extract_first_json_object(t)
    if not block:
        raise ValueError("No JSON object found in model output")

    # 3) sanitize and parse
    block2 = _basic_json_sanitize(block)
    try:
        return json.loads(block2)
    except Exception as e:
        log.warning(f"JSON parse failed, attempting repair: {e}")

    # 4) LLM repair (requires Gemini or OpenAI key configured)
    try:
        return _repair_json_with_llm(block2)
    except Exception as e:
        # Raise the original parse error context
        raise ValueError(f"Failed to parse JSON even after repair attempt: {e}")


# ---------------------------
# Validation helpers
# ---------------------------

def validate_structure(html: str) -> List[str]:
    issues = []
    if "<h2" not in html.lower():
        issues.append("Missing H2 headings")
    if "Article Highlights" not in html:
        issues.append("Missing 'Article Highlights' section")
    if "Hook" not in html:
        issues.append("Missing 'Hook' section")
    if "<table" in html.lower():
        # Count tables
        tbl = len(re.findall(r"<table\b", html.lower()))
        if tbl > config.TABLE_MAX:
            issues.append(f"Too many tables ({tbl})")
    else:
        issues.append("Missing table")
    if "{IMAGE_URL}" in html or "{{IMAGE_URL}}" in html:
        issues.append("Image placeholders not filled")
    if "<h2" in html.lower():
        h2_count = len(re.findall(r"<h2\b", html.lower()))
        # Expected: Highlights + Hook + 4-5 sections + Sources => 7-9
        if h2_count < (2 + config.H2_MIN + 1):
            issues.append(f"Too few H2 sections ({h2_count})")
    wc = word_count(re.sub(r"<[^>]+>", " ", html))
    if wc < config.ARTICLE_WORD_TARGET_MIN:
        issues.append(f"Too short ({wc} words)")
    if wc > config.ARTICLE_WORD_TARGET_MAX + 250:
        issues.append(f"Too long ({wc} words)")
    if "Sources" not in html:
        issues.append("Missing Sources section")
    return issues


# ---------------------------
# Public orchestration
# ---------------------------

def build_fact_pack(title: str, category_slug: str, sources: List[Dict[str,Any]]) -> Dict[str,Any]:
    prompt = prompt_fact_pack(title, category_slug, sources)
    out = llm_generate(
        prompt,
        primary_model=config.GEMINI_MODEL_FACTPACK,
        primary_temp=config.GEMINI_TEMPERATURE_FACTPACK,
        fallback_model=config.OPENAI_MODEL_WRITER,
        fallback_temp=0.2,
        max_output_tokens=4096,
        expect_json=True,
    )
    fp = extract_json_object(out)
    return fp

def draft_article_html(fact_pack: Dict[str,Any]) -> str:
    prompt = prompt_article(fact_pack)
    html = llm_generate(
        prompt,
        primary_model=config.GEMINI_MODEL_WRITER,
        primary_temp=config.GEMINI_TEMPERATURE_WRITER,
        fallback_model=config.OPENAI_MODEL_WRITER,
        fallback_temp=0.7,
        max_output_tokens=8192
    )
    return html

def polish_article_html(html: str, fact_pack: Dict[str,Any]) -> str:
    prompt = prompt_polish(html, fact_pack)
    polished = llm_generate(
        prompt,
        primary_model=config.GEMINI_MODEL_FACTPACK,
        primary_temp=config.GEMINI_TEMPERATURE_POLISH,
        fallback_model=config.OPENAI_MODEL_WRITER,
        fallback_temp=0.6,
        max_output_tokens=8192
    )
    return polished

def seo_pack(html: str, fact_pack: Dict[str,Any]) -> Dict[str,Any]:
    prompt = prompt_seo_pack(html, fact_pack)
    out = llm_generate(
        prompt,
        primary_model=config.GEMINI_MODEL_FACTPACK,
        primary_temp=0.3,
        fallback_model=config.OPENAI_MODEL_WRITER,
        fallback_temp=0.3,
        max_output_tokens=1200,
        expect_json=True,
    )
    return extract_json_object(out)

def create_complete_article(*,
                            title: str,
                            seed_url: str,
                            category_slug: str,
                            sources: List[Dict[str,Any]],
                            image_url: str,
                            image_alt: str,
                            image_caption: str) -> Dict[str,Any]:
    """
    Returns:
      {
        "fact_pack": {...},
        "html": "...",
        "seo": {...},
        "word_count": int
      }
    """
    fp = build_fact_pack(title, category_slug, sources)

    html = draft_article_html(fp)

    # Fill image placeholders early
    html = html.replace("{IMAGE_URL}", image_url).replace("{IMAGE_ALT}", image_alt).replace("{IMAGE_CAPTION}", image_caption)
    html = html.replace("{{IMAGE_URL}}", image_url).replace("{{IMAGE_ALT}}", image_alt).replace("{{IMAGE_CAPTION}}", image_caption)

    # If structure issues, do one polish pass; then validate again
    html2 = polish_article_html(html, fp)

    seo = seo_pack(html2, fp)

    wc = word_count(re.sub(r"<[^>]+>", " ", html2))

    return {"fact_pack": fp, "html": html2, "seo": seo, "word_count": wc}
