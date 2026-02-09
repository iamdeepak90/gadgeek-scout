import base64
import datetime as _dt
import functools
import hmac
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse

import feedparser
import redis
import requests

from config import REDIS_HOST, REDIS_PORT, REDIS_DB, REDIS_USERNAME, REDIS_PASSWORD

LOG = logging.getLogger("technews")

HTTP_TIMEOUT = 60
USER_AGENT = "Mozilla/5.0 (compatible; GadgeekBot/2.0; +https://bot.gadgeek.in)"

DEFAULT_CATEGORY_UUID = "3229ec20-3076-4a32-9fa2-88b65dacfedf"

# -------------------------
# Strict category prompt templating
# -------------------------
ALLOWED_PROMPT_VARS = {"title", "category", "sources_block"}

DEFAULT_GENERATION_TEMPLATE = """You are a professional tech journalist writing for the {category} section.

Write a tech news article about: {title}

Rules:
- Use ONLY the sources below. Do not invent facts, numbers, dates, or quotes.
- If something is uncertain, say so.
- Return ONLY HTML (no markdown).
- End with <h3>Sources</h3> listing source URLs.

Required structure:
1) <h3>Article Highlights</h3> with 2-3 <li> bullets
2) Hook paragraph 120-150 words
3) 4-5 <h2> sections, each 100-200 words
4) Add <h3> subheadings when useful
5) Include one simple HTML <table> when it helps

Sources:
{sources_block}
"""

def _extract_template_vars(template: str) -> List[str]:
    # Find {var} placeholders (ignores escaped braces like {{ or }})
    return re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", template or "")

def render_prompt_template_strict(template: str, *, title: str, category: str, sources_block: str) -> str:
    used = set(_extract_template_vars(template))
    unknown = used - ALLOWED_PROMPT_VARS
    if unknown:
        raise RuntimeError(f"Unknown prompt variable(s) in category prompt: {sorted(unknown)}")
    return (template or "").format(title=title, category=category, sources_block=sources_block)

# -------------------------
# Logging
# -------------------------
def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

# -------------------------
# Redis
# -------------------------
_REDIS_CLIENT: Optional[redis.Redis] = None

def _get_redis() -> redis.Redis:
    global _REDIS_CLIENT
    if _REDIS_CLIENT is None:
        _REDIS_CLIENT = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            db=REDIS_DB,
            username=REDIS_USERNAME or None,
            password=REDIS_PASSWORD or None,
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=10,
        )
        _REDIS_CLIENT.ping()
        LOG.info("✅ Redis connected successfully to %s:%s", REDIS_HOST, REDIS_PORT)
    return _REDIS_CLIENT

DEFAULTS_SETTINGS: Dict[str, str] = {
    "directus_url": "",
    "directus_token": "",
    "directus_leads_collection": "news_leads",
    "directus_articles_collection": "Articles",
    "directus_categories_collection": "categories",
    "slack_bot_token": "",
    "slack_signing_secret": "",
    "slack_channel_id": "",
    "tavily_api_key": "",
    "together_api_key": "",
    "openrouter_api_key": "",
    "publish_interval_minutes": "20",
    "scout_interval_minutes": "30",
    "prefer_extracted_image": "1",
}

DEFAULT_MODEL_ROUTES = {
    "generation": {"provider": "together", "model": "deepseek-ai/DeepSeek-V3.1", "temperature": 0.6, "max_tokens": 2200},
    "humanize":   {"provider": "together", "model": "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo", "temperature": 0.7, "max_tokens": 2200},
    "seo":        {"provider": "together", "model": "meta-llama/Llama-3.2-3B-Instruct-Turbo", "temperature": 0.4, "max_tokens": 900},
    "image":      {"provider": "together", "model": "black-forest-labs/FLUX.1-schnell", "width": 1024, "height": 768},
}

def _now_iso() -> str:
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def init_db() -> None:
    r = _get_redis()
    for k, v in DEFAULTS_SETTINGS.items():
        key = f"settings:{k}"
        if not r.exists(key):
            r.hset(key, mapping={"value": v, "updated_at": _now_iso()})
    for stage, cfg in DEFAULT_MODEL_ROUTES.items():
        key = f"model_routes:{stage}"
        if not r.exists(key):
            data = {"provider": cfg["provider"], "model": cfg["model"], "updated_at": _now_iso()}
            for opt in ("temperature", "max_tokens", "width", "height"):
                if opt in cfg:
                    data[opt] = str(cfg[opt])
            r.hset(key, mapping=data)
    LOG.info("Redis initialized with default settings")

def get_setting(key: str, default: Optional[str] = None) -> str:
    r = _get_redis()
    value = r.hget(f"settings:{key}", "value")
    return value if value else (default or "")

def set_setting(key: str, value: str) -> None:
    r = _get_redis()
    r.hset(f"settings:{key}", mapping={"value": value, "updated_at": _now_iso()})

def list_settings() -> Dict[str, str]:
    r = _get_redis()
    out: Dict[str, str] = {}
    for redis_key in r.scan_iter(match="settings:*"):
        key = redis_key.replace("settings:", "")
        val = r.hget(redis_key, "value")
        if val is not None:
            out[key] = val
    return out

# -------------------------
# Feeds (Redis)
# -------------------------
def list_feeds() -> List[Dict[str, Any]]:
    r = _get_redis()
    feeds: List[Dict[str, Any]] = []
    for redis_key in r.scan_iter(match="feed:*"):
        if redis_key == "feed:next_id":
            continue
        data = r.hgetall(redis_key)
        if not data:
            continue
        try:
            feed_id = int(redis_key.replace("feed:", ""))
        except ValueError:
            continue
        feeds.append({
            "id": feed_id,
            "url": data.get("url", ""),
            "enabled": data.get("enabled", "1") == "1",
            "category_hint": data.get("category_hint") or "",
            "title_key": data.get("title_key") or "",
            "description_key": data.get("description_key") or "",
            "content_key": data.get("content_key") or "",
            "category_key": data.get("category_key") or "",
        })
    feeds.sort(key=lambda x: x["id"], reverse=True)
    return feeds

def upsert_feed(feed: Dict[str, Any]) -> int:
    """Create or update an RSS feed in Redis; dedupe by URL."""
    r = _get_redis()
    url = (feed.get("url") or "").strip()
    if not url:
        raise ValueError("feed.url is required")

    # Find existing by URL
    feed_id: Optional[int] = None
    for redis_key in r.scan_iter(match="feed:*"):
        if redis_key == "feed:next_id":
            continue
        data = r.hgetall(redis_key)
        if data.get("url") == url:
            try:
                feed_id = int(redis_key.replace("feed:", ""))
                break
            except Exception:
                continue

    now = _now_iso()
    if feed_id is None:
        feed_id = int(r.incr("feed:next_id"))
        created_at = now
    else:
        existing = r.hgetall(f"feed:{feed_id}")
        created_at = existing.get("created_at", now)

    feed_data = {
        "url": url,
        "enabled": "1" if feed.get("enabled", True) else "0",
        "category_hint": feed.get("category_hint") or "",
        "title_key": feed.get("title_key") or "",
        "description_key": feed.get("description_key") or "",
        "content_key": feed.get("content_key") or "",
        "category_key": feed.get("category_key") or "",
        "created_at": created_at,
        "updated_at": now,
    }
    r.hset(f"feed:{feed_id}", mapping=feed_data)
    return feed_id

def delete_feed(feed_id: int) -> None:
    r = _get_redis()
    r.delete(f"feed:{int(feed_id)}")

# -------------------------
# Model routes
# -------------------------
def get_model_routes() -> Dict[str, Dict[str, Any]]:
    r = _get_redis()
    routes: Dict[str, Dict[str, Any]] = {}
    for redis_key in r.scan_iter(match="model_routes:*"):
        stage = redis_key.replace("model_routes:", "")
        if not stage:
            continue
        data = r.hgetall(redis_key)
        if not data:
            continue
        route: Dict[str, Any] = {"provider": data.get("provider", ""), "model": data.get("model", "")}
        for k, conv in (("temperature", float), ("max_tokens", int), ("width", int), ("height", int)):
            if data.get(k):
                try:
                    route[k] = conv(float(data[k])) if conv is int else conv(data[k])
                except Exception:
                    pass
        routes[stage] = route
    return routes

def set_model_route(stage: str, provider: str, model: str,
                    temperature: Optional[float] = None,
                    max_tokens: Optional[int] = None,
                    width: Optional[int] = None,
                    height: Optional[int] = None) -> None:
    r = _get_redis()
    data: Dict[str, str] = {"provider": provider, "model": model, "updated_at": _now_iso()}
    if temperature is not None:
        data["temperature"] = str(temperature)
    if max_tokens is not None:
        data["max_tokens"] = str(max_tokens)
    if width is not None:
        data["width"] = str(width)
    if height is not None:
        data["height"] = str(height)
    r.hset(f"model_routes:{stage}", mapping=data)

# -------------------------
# HTTP
# -------------------------
@dataclass
class Response:
    status_code: int
    text: str
    headers: Dict[str, str]
    def json(self):
        return json.loads(self.text)

def request_with_retry(method: str, url: str, headers: Optional[Dict[str, str]] = None,
                       json_body: Optional[Dict[str, Any]] = None, timeout: int = HTTP_TIMEOUT,
                       max_attempts: int = 3) -> Response:
    hdrs = dict(headers or {})
    hdrs.setdefault("User-Agent", USER_AGENT)
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.request(method, url, headers=hdrs, json=json_body, timeout=timeout)
            return Response(resp.status_code, resp.text, dict(resp.headers))
        except Exception as e:
            LOG.warning("HTTP attempt %d/%d failed: %s", attempt, max_attempts, e)
            if attempt == max_attempts:
                raise
            time.sleep(min(2 ** attempt, 10))
    raise RuntimeError("unreachable")

# -------------------------
# Basic Auth (settings UI)
# -------------------------
def require_basic_auth(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        from flask import request, Response as FlaskResponse
        auth = request.authorization
        if not auth or auth.username != "settings@gadgeek.in" or auth.password != "HelloGG@$44":
            return FlaskResponse("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm="Settings"'})
        return f(*args, **kwargs)
    return wrapper

# -------------------------
# Directus config
# -------------------------
def directus_url() -> str:
    return get_setting("directus_url", "").rstrip("/")

def directus_token() -> str:
    return get_setting("directus_token", "")

def leads_collection() -> str:
    return get_setting("directus_leads_collection", "news_leads")

def articles_collection() -> str:
    return get_setting("directus_articles_collection", "Articles")

def categories_collection() -> str:
    return get_setting("directus_categories_collection", "categories")

def _auth_headers() -> Dict[str, str]:
    tok = directus_token()
    return {"Authorization": f"Bearer {tok}"} if tok else {}

def directus_get(path: str) -> Dict[str, Any]:
    url = f"{directus_url()}{path}"
    resp = request_with_retry("GET", url, headers=_auth_headers())
    if resp.status_code >= 400:
        raise RuntimeError(f"Directus GET {path} failed: {resp.status_code} {resp.text}")
    return resp.json()

def directus_post(path: str, data: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{directus_url()}{path}"
    resp = request_with_retry("POST", url, headers=_auth_headers(), json_body=data)
    if resp.status_code >= 400:
        raise RuntimeError(f"Directus POST {path} failed: {resp.status_code} {resp.text}")
    return resp.json()

def directus_patch(path: str, data: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{directus_url()}{path}"
    resp = request_with_retry("PATCH", url, headers=_auth_headers(), json_body=data)
    if resp.status_code >= 400:
        raise RuntimeError(f"Directus PATCH {path} failed: {resp.status_code} {resp.text}")
    return resp.json()

def get_categories() -> List[Dict[str, Any]]:
    """Enabled categories, including prompt_generation."""
    col = categories_collection()
    params = urlencode({"filter[enabled][_eq]": "true", "sort": "priority", "limit": "-1"})
    data = directus_get(f"/items/{col}?{params}")
    cats = data.get("data") or []
    out: List[Dict[str, Any]] = []

    def safe_int(val, default):
        if val in (None, "", "null"):
            return default
        try:
            return int(float(val))
        except Exception:
            return default

    for c in cats:
        kw = c.get("keywords", [])
        if isinstance(kw, str):
            try:
                kw = json.loads(kw)
            except Exception:
                kw = []
        if not isinstance(kw, list):
            kw = []
        out.append({
            "id": str(c.get("id") or ""),
            "name": c.get("name", "") or "",
            "priority": safe_int(c.get("priority"), 999),
            "posts_per_scout": safe_int(c.get("posts_per_scout"), 0),
            "keywords": [str(x) for x in kw if str(x).strip()],
            "prompt_generation": (c.get("prompt_generation") or "").strip(),
        })
    return out

def lead_exists_by_url(source_url: str) -> bool:
    col = leads_collection()
    params = urlencode({"filter[source_url][_eq]": source_url, "limit": "1"})
    data = directus_get(f"/items/{col}?{params}")
    return bool(data.get("data"))

def create_lead(title: str, source_url: str, category_id: str) -> str:
    col = leads_collection()
    payload = {
        "title": title,
        "source_url": source_url,
        "category": category_id or DEFAULT_CATEGORY_UUID,
        "status": "pending",
    }
    data = directus_post(f"/items/{col}", payload)
    item = data.get("data") or {}
    lead_id = item.get("id")
    if not lead_id:
        raise RuntimeError(f"Directus create_lead returned no id (response data: {item})")
    return str(lead_id)

def get_lead(lead_id: str) -> Dict[str, Any]:
    col = leads_collection()
    data = directus_get(f"/items/{col}/{lead_id}")
    return data.get("data") or {}

def update_lead_status(lead_id: str, status: str) -> None:
    col = leads_collection()
    directus_patch(f"/items/{col}/{lead_id}", {"status": status})

def list_one_approved_lead_newest() -> Optional[Dict[str, Any]]:
    col = leads_collection()
    params = urlencode({"filter[status][_eq]": "approved", "sort": "-date_created", "limit": "1"})
    data = directus_get(f"/items/{col}?{params}")
    items = data.get("data") or []
    if items:
        return items[0]
    # fallback
    params = urlencode({"filter[status][_eq]": "approved", "sort": "-id", "limit": "1"})
    data = directus_get(f"/items/{col}?{params}")
    items = data.get("data") or []
    return items[0] if items else None

# -------------------------
# Slack
# -------------------------
def slack_token() -> str:
    return get_setting("slack_bot_token", "")

def slack_channel() -> str:
    return get_setting("slack_channel_id", "")

def slack_signing_secret() -> str:
    return get_setting("slack_signing_secret", "")

def verify_slack_signature(headers: Dict[str, str], body: bytes) -> bool:
    secret = slack_signing_secret()
    if not secret:
        LOG.warning("Slack signing secret not set; skipping signature verification")
        return True
    sig = headers.get("X-Slack-Signature") or headers.get("x-slack-signature") or ""
    ts = headers.get("X-Slack-Request-Timestamp") or headers.get("x-slack-request-timestamp") or ""
    if not sig or not ts:
        return False
    base = f"v0:{ts}:{body.decode('utf-8')}"
    expected = "v0=" + hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)

def slack_post_lead(title: str, category_name: str, lead_id: str) -> Dict[str, Any]:
    token = slack_token()
    channel = slack_channel()
    if not token or not channel:
        raise RuntimeError("Slack bot token or channel ID not configured")

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n_{category_name}_" }},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✅ Approve"}, "style": "primary", "action_id": "approve", "value": str(lead_id)},
            {"type": "button", "text": {"type": "plain_text", "text": "🚀 Urgent"}, "style": "danger", "action_id": "urgent", "value": str(lead_id)},
            {"type": "button", "text": {"type": "plain_text", "text": "❌ Reject"}, "action_id": "reject", "value": str(lead_id)},
        ]},
    ]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"channel": channel, "text": title, "blocks": blocks}
    resp = request_with_retry("POST", "https://slack.com/api/chat.postMessage", headers=headers, json_body=payload)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack post failed: {data.get('error')}")
    return data

def slack_update_published(channel: str, ts: str, title: str) -> None:
    token = slack_token()
    if not token:
        return
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"✅ *Published:* {title}"}}]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"channel": channel, "ts": ts, "text": f"Published: {title}", "blocks": blocks}
    request_with_retry("POST", "https://slack.com/api/chat.update", headers=headers, json_body=payload)

def slack_ephemeral(response_url: str, text: str) -> None:
    if not response_url:
        return
    payload = {"text": text, "response_type": "ephemeral", "replace_original": False}
    request_with_retry("POST", response_url, json_body=payload)

# -------------------------
# RSS parsing helpers
# -------------------------
def parse_feed(url: str):
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    return feedparser.parse(resp.content)

def _nested_get(d: Any, key: str) -> Any:
    if not key:
        return None
    parts = key.split(".")
    for p in parts:
        if isinstance(d, dict):
            d = d.get(p)
        elif isinstance(d, list):
            try:
                idx = int(p)
                d = d[idx] if 0 <= idx < len(d) else None
            except Exception:
                return None
        else:
            return None
        if d is None:
            return None
    return d

def extract_entry_fields(entry: Any, selectors: Dict[str, Optional[str]]) -> Dict[str, str]:
    title = _nested_get(entry, selectors.get("title_key") or "title") or getattr(entry, "title", "")
    desc = _nested_get(entry, selectors.get("description_key") or "summary") or getattr(entry, "summary", "")
    content_key = selectors.get("content_key")
    content = ""
    if content_key:
        content = _nested_get(entry, content_key) or ""
    else:
        c = getattr(entry, "content", [])
        if c and isinstance(c, list) and len(c) > 0:
            content = c[0].get("value", "")

    link = getattr(entry, "link", "")
    category_key = selectors.get("category_key")
    category = ""
    if category_key:
        category = _nested_get(entry, category_key) or ""
    else:
        tags = getattr(entry, "tags", [])
        if tags and isinstance(tags, list) and len(tags) > 0:
            category = tags[0].get("term", "")

    return {"title": str(title), "link": str(link), "description": str(desc), "content": str(content), "category": str(category)}

# -------------------------
# LLM routing (Together/OpenRouter)
# -------------------------
TOGETHER_CHAT_URL = "https://api.together.xyz/v1/chat/completions"
TOGETHER_IMAGES_URL = "https://api.together.xyz/v1/images/generations"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

def chat_stage(stage: str, messages: List[Dict[str, str]]) -> str:
    routes = get_model_routes()
    route = routes.get(stage)
    if not route:
        raise RuntimeError(f"No model route configured for stage: {stage}")
    provider = route["provider"]
    model = route["model"]
    temperature = route.get("temperature", 0.7)
    max_tokens = route.get("max_tokens", 2000)

    if provider == "together":
        return _chat_together(model, messages, temperature, max_tokens)
    if provider == "openrouter":
        return _chat_openrouter(model, messages, temperature, max_tokens)
    raise RuntimeError(f"Unknown provider: {provider}")

def _chat_together(model: str, messages: List[Dict[str, str]], temperature: float, max_tokens: int) -> str:
    key = get_setting("together_api_key")
    if not key:
        raise RuntimeError("Together API key not configured")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    resp = request_with_retry("POST", TOGETHER_CHAT_URL, headers=headers, json_body=payload, timeout=120, max_attempts=3)
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Together returned no choices: {data}")
    return (choices[0].get("message") or {}).get("content") or ""

def _chat_openrouter(model: str, messages: List[Dict[str, str]], temperature: float, max_tokens: int) -> str:
    key = get_setting("openrouter_api_key")
    if not key:
        raise RuntimeError("OpenRouter API key not configured")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "HTTP-Referer": "https://bot.gadgeek.in", "X-Title": "Gadgeek Tech News"}
    payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    resp = request_with_retry("POST", OPENROUTER_CHAT_URL, headers=headers, json_body=payload, timeout=120, max_attempts=3)
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenRouter returned no choices: {data}")
    return (choices[0].get("message") or {}).get("content") or ""

# -------------------------
# Tavily research
# -------------------------
def build_research_pack(title: str) -> Dict[str, Any]:
    key = get_setting("tavily_api_key")
    if not key:
        LOG.warning("Tavily API key not set; skipping research.")
        return {"extract": {"results": [], "images": []}}
    headers = {"Content-Type": "application/json"}
    payload = {"api_key": key, "query": title, "search_depth": "advanced", "max_results": 5,
               "include_raw_content": True, "include_images": True}
    try:
        resp = request_with_retry("POST", "https://api.tavily.com/search", headers=headers, json_body=payload, timeout=60, max_attempts=2)
        data = resp.json()
        return {"extract": data}
    except Exception as e:
        LOG.warning("Tavily research failed: %s", e)
        return {"extract": {"results": [], "images": []}}

def build_sources_block(pack: Dict[str, Any]) -> str:
    extract_results = (pack.get("extract") or {}).get("results") or []
    snippets = []
    for r in extract_results[:5]:
        url = r.get("url") or ""
        content = (r.get("content") or r.get("raw_content") or "").strip()
        if len(content) > 1500:
            content = content[:1500] + "…"
        snippets.append(f"SOURCE: {url}\n{content}")
    return "\n\n".join(snippets) if snippets else "No extracted sources available."

def pick_extracted_image(pack: Dict[str, Any]) -> Optional[Dict[str, str]]:
    extract = pack.get("extract") or {}
    results = extract.get("results") or []
    images = extract.get("images") or []
    for r in results:
        img_url = r.get("image")
        if img_url and isinstance(img_url, str) and img_url.startswith("http"):
            return {"url": img_url, "credit": r.get("url", "Source"), "caption": "Featured image"}
    if images and isinstance(images, list) and isinstance(images[0], str) and images[0].startswith("http"):
        return {"url": images[0], "credit": "Web", "caption": "Featured image"}
    return None

# -------------------------
# Image generation
# -------------------------
def generate_image(prompt: str) -> Optional[Dict[str, str]]:
    routes = get_model_routes()
    route = routes.get("image") or {}
    provider = route.get("provider") or "together"
    model = route.get("model") or "black-forest-labs/FLUX.1-schnell"
    width = int(route.get("width") or 1024)
    height = int(route.get("height") or 768)
    if provider == "together":
        return generate_image_together(prompt, width, height, model)
    if provider == "openrouter":
        return generate_image_openrouter(prompt, width, height, model)
    raise RuntimeError(f"Unknown image provider: {provider}")

def generate_image_together(prompt: str, width: int, height: int, model: str) -> Optional[Dict[str, str]]:
    key = get_setting("together_api_key")
    if not key:
        return None
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {"model": model, "prompt": prompt, "width": width, "height": height, "steps": 4, "n": 1,
               "response_format": "url", "output_format": "jpeg"}
    try:
        resp = request_with_retry("POST", TOGETHER_IMAGES_URL, headers=headers, json_body=payload, timeout=120, max_attempts=3)
        data = resp.json()
        item = (data.get("data") or [{}])[0]
        if item.get("url"):
            return {"url": item["url"], "credit": "AI-generated (Together)", "caption": "AI-generated illustration"}
        if item.get("b64_json"):
            return {"url": f"data:image/jpeg;base64,{item['b64_json']}", "credit": "AI-generated (Together)", "caption": "AI-generated illustration"}
    except Exception as e:
        LOG.warning("Together image generation failed: %s", e)
    return None

def generate_image_openrouter(prompt: str, width: int, height: int, model: str) -> Optional[Dict[str, str]]:
    key = get_setting("openrouter_api_key")
    if not key:
        return None
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "HTTP-Referer": "https://bot.gadgeek.in", "X-Title": "Gadgeek Tech News"}
    # models vary; best-effort URL extract from content
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    try:
        resp = request_with_retry("POST", OPENROUTER_CHAT_URL, headers=headers, json_body=payload, timeout=120, max_attempts=3)
        data = resp.json()
        choices = data.get("choices") or []
        if choices:
            content = (choices[0].get("message") or {}).get("content") or ""
            urls = re.findall(r'https?://[^\s<>"]+', content)
            if urls:
                return {"url": urls[0], "credit": "AI-generated (OpenRouter)", "caption": "AI-generated illustration"}
    except Exception as e:
        LOG.warning("OpenRouter image generation failed: %s", e)
    return None

# -------------------------
# Text utils
# -------------------------
def slugify(text: str, max_len: int = 80) -> str:
    t = (text or "").lower()
    t = re.sub(r"[^a-z0-9\s-]", "", t)
    t = re.sub(r"\s+", "-", t).strip("-")
    if len(t) > max_len:
        t = t[:max_len].rstrip("-")
    return t or "tech-news"

def _sanitize_json(s: str) -> str:
    s = s.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    return s

def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    t = _sanitize_json((text or "").strip())
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    start = t.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(t)):
        ch = t[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                block = _sanitize_json(t[start:i+1])
                try:
                    obj = json.loads(block)
                    return obj if isinstance(obj, dict) else None
                except Exception:
                    return None
    return None

# -------------------------
# Article pipeline
# -------------------------
def build_generation_messages(title: str, category_name: str, sources_block: str, category_template: str) -> List[Dict[str, str]]:
    template = (category_template or "").strip() or DEFAULT_GENERATION_TEMPLATE
    user_prompt = render_prompt_template_strict(template, title=title, category=category_name, sources_block=sources_block)
    system = (
        "You are a professional tech journalist. Follow the user's instructions. "
        "Use ONLY provided sources; do not invent facts. Return ONLY HTML."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user_prompt}]

def humanize_prompt(html: str) -> List[Dict[str, str]]:
    system = ("You are an editor. Improve readability and flow while keeping ALL facts identical. "
              "Do not add new facts. Do not remove the Sources list. Keep output as HTML only.")
    user = f"Rewrite the following HTML article to be more natural and user-friendly:\n\n{html}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]

def seo_prompt(title: str, category_name: str, html: str) -> List[Dict[str, str]]:
    system = "You are an SEO editor for a tech news site. Return STRICT JSON only."
    user = (
        f"Given the article HTML, produce SEO metadata.\n"
        f"Title: {title}\nCategory: {category_name}\n\n"
        "Return a JSON object with keys:\n"
        "- meta_title (max ~60 chars)\n"
        "- meta_description (max ~155 chars)\n"
        "- short_description (1-2 sentences)\n"
        "- tags (array of 5-10 short tags)\n"
        "- image_alt (short alt text)\n\n"
        "Article HTML:\n"
        f"{html}\n"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]

def build_image_prompt(title: str, category_name: str) -> str:
    return (
        f"High-quality realistic product-style hero image illustrating the tech topic: {title}. "
        f"Context category: {category_name}. "
        "No people, no brand logos, no text. Studio lighting, clean background. Looks like a magazine illustration."
    )

# -------------------------
# Directus file handling for featured_image
# -------------------------
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

def _is_uuid(s: str) -> bool:
    return bool(_UUID_RE.match((s or "").strip()))

def directus_import_file_url(file_url: str, title: Optional[str] = None) -> str:
    payload: Dict[str, Any] = {"url": file_url}
    if title:
        payload["data"] = {"title": title}
    data = directus_post("/files/import", payload)
    fid = (data.get("data") or {}).get("id")
    if not fid:
        raise RuntimeError(f"Directus file import returned no id: {data}")
    return str(fid)

def directus_upload_file_bytes(filename: str, content: bytes, mime: str, title: Optional[str] = None) -> str:
    url = f"{directus_url()}/files"
    headers = _auth_headers()
    files = {"file": (filename, content, mime)}
    data = {}
    if title:
        data["title"] = title
    resp = requests.post(url, headers=headers, files=files, data=data, timeout=HTTP_TIMEOUT)
    if resp.status_code >= 400:
        raise RuntimeError(f"Directus file upload failed: {resp.status_code} {resp.text}")
    payload = resp.json()
    fid = (payload.get("data") or {}).get("id")
    if not fid:
        raise RuntimeError(f"Directus file upload returned no id: {payload}")
    return str(fid)

def _download_url_bytes(file_url: str) -> Tuple[str, bytes, str]:
    r = requests.get(file_url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"Image download failed: {r.status_code} {r.text[:200]}")
    parsed = urlparse(file_url)
    name = (os.path.basename(parsed.path) or "image").split("?")[0] or "image"
    if "." not in name:
        name += ".jpg"
    mime = r.headers.get("Content-Type") or "image/jpeg"
    return name, r.content, mime

def upload_featured_image_anyhow(image_url_or_data: str, *, title: str) -> str:
    s = (image_url_or_data or "").strip()
    if not s:
        raise RuntimeError("No image provided")
    if _is_uuid(s):
        return s
    if s.startswith("data:image/") and "base64," in s:
        header, b64 = s.split("base64,", 1)
        mime = "image/jpeg"
        m = re.search(r"data:(image/[^;]+);", header)
        if m:
            mime = m.group(1)
        ext = mime.split("/")[-1].replace("jpeg", "jpg")
        content = base64.b64decode(b64.encode("utf-8"))
        filename = f"{slugify(title, 40)}.{ext}"
        return directus_upload_file_bytes(filename, content, mime, title=title)

    if s.lower().startswith(("http://", "https://")):
        try:
            return directus_import_file_url(s, title=title)
        except Exception as e:
            # external-file service may be down; fallback to download+upload
            LOG.warning("Directus /files/import failed; falling back to upload: %s", e)
            filename, content, mime = _download_url_bytes(s)
            return directus_upload_file_bytes(filename, content, mime, title=title)

    raise RuntimeError("Unsupported image format")

# -------------------------
# Article assembly
# -------------------------
def create_article_from_lead(title: str, category_name: str, category_id: str, category_prompt: str) -> Dict[str, Any]:
    pack = build_research_pack(title)
    sources_block = build_sources_block(pack)

    # Generation (category template or default)
    draft_html = chat_stage("generation", build_generation_messages(title, category_name, sources_block, category_prompt))
    if not draft_html.strip():
        raise RuntimeError("Generation returned empty content.")

    # Humanize
    human_html = chat_stage("humanize", humanize_prompt(draft_html))
    if not human_html.strip():
        human_html = draft_html

    # SEO
    seo_out = chat_stage("seo", seo_prompt(title, category_name, human_html))
    seo = extract_json_object(seo_out) or {}
    meta_title = seo.get("meta_title") or title[:60]
    meta_description = seo.get("meta_description") or (seo.get("short_description") or "")[:155]
    short_description = seo.get("short_description") or meta_description
    tags = seo.get("tags") if isinstance(seo.get("tags"), list) else []
    image_alt = seo.get("image_alt") or f"{title} — {category_name}"

    # Image: extracted first, fallback AI
    featured_image_url = ""
    credit_line = ""
    if get_setting("prefer_extracted_image", "1") == "1":
        img = pick_extracted_image(pack)
        if img and img.get("url"):
            featured_image_url = img["url"]
            credit_line = f"{img.get('caption','Featured image')} | Credit: {img.get('credit','')}".strip(" |")

    if not featured_image_url:
        gen = generate_image(build_image_prompt(title, category_name))
        if gen and gen.get("url"):
            featured_image_url = gen["url"]
            credit_line = f"{gen.get('caption','AI image')} | Credit: {gen.get('credit','')}".strip(" |")

    # slug + timestamps
    slug = slugify(title)
    published_at = _now_iso()

    return {
        "title": title,
        "slug": f"{slug}-{hashlib.md5(title.encode('utf-8')).hexdigest()[:6]}",
        "status": "published",
        "category": category_id,
        "short_description": short_description,
        "content": human_html,
        "featured_image": featured_image_url,  # URL or data URL; converted during publish
        "featured_image_credit": credit_line,
        "featured_image_alt": image_alt,
        "meta_title": meta_title,
        "meta_description": meta_description,
        "tags": tags,
        "published_at": published_at,
    }

def publish_article_to_directus(article: Dict[str, Any]) -> Dict[str, Any]:
    col = articles_collection()
    payload = dict(article)

    # Convert featured_image URL/data -> Directus file UUID
    fi = (payload.get("featured_image") or "").strip()
    if fi:
        try:
            payload["featured_image"] = upload_featured_image_anyhow(fi, title=payload.get("title") or "")
        except Exception as e:
            LOG.warning("Featured image failed; publishing without featured_image: %s", e)
            payload.pop("featured_image", None)

    # Clean payload
    required = {"title", "slug", "status", "category", "content"}
    clean: Dict[str, Any] = {}
    for k, v in payload.items():
        if k in required:
            clean[k] = v
            continue
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        if isinstance(v, list) and len(v) == 0:
            continue
        clean[k] = v

    data = directus_post(f"/items/{col}", clean)
    return data.get("data") or {}
