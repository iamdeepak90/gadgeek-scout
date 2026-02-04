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

import requests
import feedparser
import redis

from config import REDIS_HOST, REDIS_PORT, REDIS_DB, REDIS_PASSWORD

LOG = logging.getLogger("technews")

# -------------------------
# Logging
# -------------------------
def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

# -------------------------
# Redis client (shared settings store)
# -------------------------

_REDIS_CLIENT = None

def _get_redis() -> redis.Redis:
    global _REDIS_CLIENT
    if _REDIS_CLIENT is None:
        _REDIS_CLIENT = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            db=REDIS_DB,
            password=REDIS_PASSWORD if REDIS_PASSWORD else None,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
    return _REDIS_CLIENT

DEFAULTS_SETTINGS: Dict[str, str] = {
    # endpoints
    "directus_url": "",
    "directus_token": "",
    "directus_leads_collection": "news_leads",
    "directus_articles_collection": "Articles",
    "directus_categories_collection": "categories",
    # slack
    "slack_bot_token": "",
    "slack_signing_secret": "",
    "slack_channel_id": "",
    # api keys
    "tavily_api_key": "",
    "together_api_key": "",
    "openrouter_api_key": "",
    # runtime
    "http_timeout": "30",
    "user_agent": "Mozilla/5.0 (compatible; TechNewsAutomation/1.0; +https://bot.gadgeek.in/settings)",
    "publish_interval_minutes": "20",
    "scout_interval_minutes": "30",
    # pipeline options
    "prefer_extracted_image": "1",  # 1=true
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
    """Initialize Redis with default settings and model routes"""
    r = _get_redis()
    
    # Initialize default settings if not present
    for k, v in DEFAULTS_SETTINGS.items():
        key = f"settings:{k}"
        if not r.exists(key):
            r.hset(key, mapping={"value": v, "updated_at": _now_iso()})
    
    # Initialize default model routes if not present
    for stage, cfg in DEFAULT_MODEL_ROUTES.items():
        key = f"model_routes:{stage}"
        if not r.exists(key):
            data = {
                "provider": cfg["provider"],
                "model": cfg["model"],
                "updated_at": _now_iso()
            }
            if "temperature" in cfg:
                data["temperature"] = str(cfg["temperature"])
            if "max_tokens" in cfg:
                data["max_tokens"] = str(cfg["max_tokens"])
            if "width" in cfg:
                data["width"] = str(cfg["width"])
            if "height" in cfg:
                data["height"] = str(cfg["height"])
            r.hset(key, mapping=data)
    
    LOG.info("Redis initialized with default settings")

def get_setting(key: str, default: Optional[str] = None) -> str:
    """Get a setting from Redis"""
    r = _get_redis()
    redis_key = f"settings:{key}"
    value = r.hget(redis_key, "value")
    if value is not None:
        return value
    return default if default is not None else ""

def set_setting(key: str, value: str) -> None:
    """Set a setting in Redis"""
    r = _get_redis()
    redis_key = f"settings:{key}"
    r.hset(redis_key, mapping={"value": value, "updated_at": _now_iso()})

def list_settings() -> Dict[str, str]:
    """List all settings from Redis"""
    r = _get_redis()
    result = {}
    for redis_key in r.scan_iter(match="settings:*"):
        key = redis_key.replace("settings:", "")
        value = r.hget(redis_key, "value")
        if value is not None:
            result[key] = value
    return result

# -------------------------
# RSS Feeds (Redis-based)
# -------------------------

def list_feeds() -> List[Dict[str, Any]]:
    """List all RSS feeds from Redis"""
    r = _get_redis()
    feeds = []
    for redis_key in r.scan_iter(match="feed:*"):
        data = r.hgetall(redis_key)
        if data:
            feed_id = int(redis_key.replace("feed:", ""))
            feeds.append({
                "id": feed_id,
                "url": data.get("url", ""),
                "enabled": data.get("enabled", "1") == "1",
                "category_hint": data.get("category_hint"),
                "title_key": data.get("title_key"),
                "description_key": data.get("description_key"),
                "content_key": data.get("content_key"),
                "category_key": data.get("category_key"),
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at", ""),
            })
    # Sort by ID descending
    feeds.sort(key=lambda x: x["id"], reverse=True)
    return feeds

def upsert_feed(feed: Dict[str, Any]) -> int:
    """Create or update an RSS feed in Redis"""
    r = _get_redis()
    url = feed["url"]
    
    # Check if feed exists by URL
    feed_id = None
    for redis_key in r.scan_iter(match="feed:*"):
        data = r.hgetall(redis_key)
        if data.get("url") == url:
            feed_id = int(redis_key.replace("feed:", ""))
            break
    
    now = _now_iso()
    
    if feed_id is None:
        # Create new feed - get next ID
        feed_id = r.incr("feed:next_id")
        created_at = now
    else:
        # Update existing
        existing = r.hgetall(f"feed:{feed_id}")
        created_at = existing.get("created_at", now)
    
    # Store feed
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
    """Delete an RSS feed from Redis"""
    r = _get_redis()
    r.delete(f"feed:{feed_id}")

# -------------------------
# Model Routes (Redis-based)
# -------------------------

def get_model_routes() -> Dict[str, Dict[str, Any]]:
    """Get all model routing configurations"""
    r = _get_redis()
    routes = {}
    for redis_key in r.scan_iter(match="model_routes:*"):
        stage = redis_key.replace("model_routes:", "")
        data = r.hgetall(redis_key)
        if data:
            route = {
                "provider": data.get("provider", ""),
                "model": data.get("model", ""),
            }
            if "temperature" in data and data["temperature"]:
                route["temperature"] = float(data["temperature"])
            if "max_tokens" in data and data["max_tokens"]:
                route["max_tokens"] = int(data["max_tokens"])
            if "width" in data and data["width"]:
                route["width"] = int(data["width"])
            if "height" in data and data["height"]:
                route["height"] = int(data["height"])
            routes[stage] = route
    return routes

def set_model_route(stage: str, provider: str, model: str, temperature: Optional[float] = None, 
                    max_tokens: Optional[int] = None, width: Optional[int] = None, 
                    height: Optional[int] = None) -> None:
    """Set model routing for a specific stage"""
    r = _get_redis()
    data = {
        "provider": provider,
        "model": model,
        "updated_at": _now_iso(),
    }
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
# HTTP utilities
# -------------------------

@dataclass
class Response:
    status_code: int
    text: str
    headers: Dict[str, str]

    def json(self):
        return json.loads(self.text)

def request_with_retry(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
    max_attempts: int = 3,
) -> Response:
    timeout = int(get_setting("http_timeout", str(timeout)))
    ua = get_setting("user_agent", "TechNewsBot/1.0")
    hdrs = headers or {}
    if "User-Agent" not in hdrs:
        hdrs["User-Agent"] = ua
    
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.request(method, url, headers=hdrs, json=json_body, timeout=timeout)
            return Response(status_code=resp.status_code, text=resp.text, headers=dict(resp.headers))
        except Exception as e:
            LOG.warning("HTTP request attempt %d/%d failed: %s", attempt, max_attempts, e)
            if attempt == max_attempts:
                raise
            time.sleep(min(2 ** attempt, 10))
    raise RuntimeError("Unreachable")

# -------------------------
# Basic Auth
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
# Directus
# -------------------------

def directus_url() -> str:
    return (get_setting("directus_url") or "").rstrip("/")

def directus_token() -> str:
    return get_setting("directus_token") or ""

def leads_collection() -> str:
    return get_setting("directus_leads_collection", "news_leads")

def articles_collection() -> str:
    return get_setting("directus_articles_collection", "Articles")

def categories_collection() -> str:
    return get_setting("directus_categories_collection", "categories")

def directus_get(path: str) -> Dict[str, Any]:
    url = f"{directus_url()}{path}"
    headers = {"Authorization": f"Bearer {directus_token()}"}
    resp = request_with_retry("GET", url, headers=headers)
    if resp.status_code >= 400:
        raise RuntimeError(f"Directus GET {path} failed: {resp.status_code} {resp.text}")
    return resp.json()

def directus_post(path: str, data: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{directus_url()}{path}"
    headers = {"Authorization": f"Bearer {directus_token()}"}
    resp = request_with_retry("POST", url, headers=headers, json_body=data)
    if resp.status_code >= 400:
        raise RuntimeError(f"Directus POST {path} failed: {resp.status_code} {resp.text}")
    return resp.json()

def directus_patch(path: str, data: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{directus_url()}{path}"
    headers = {"Authorization": f"Bearer {directus_token()}"}
    resp = request_with_retry("PATCH", url, headers=headers, json_body=data)
    if resp.status_code >= 400:
        raise RuntimeError(f"Directus PATCH {path} failed: {resp.status_code} {resp.text}")
    return resp.json()

def get_categories() -> List[Dict[str, Any]]:
    col = categories_collection()
    params = urlencode({"filter[enabled][_eq]": "true", "sort": "priority", "limit": "-1"})
    data = directus_get(f"/items/{col}?{params}")
    cats = data.get("data") or []
    out = []
    for c in cats:
        kw = c.get("keywords")
        if isinstance(kw, str):
            try:
                kw = json.loads(kw)
            except Exception:
                kw = []
        if not isinstance(kw, list):
            kw = []
        out.append({
            "slug": c.get("slug") or "",
            "name": c.get("name") or "",
            "priority": int(c.get("priority") or 999),
            "posts_per_scout": int(c.get("posts_per_scout") or 0),
            "keywords": kw,
        })
    return out

def lead_exists_by_url(source_url: str) -> bool:
    col = leads_collection()
    params = urlencode({"filter[source_url][_eq]": source_url, "limit": "1"})
    data = directus_get(f"/items/{col}?{params}")
    items = data.get("data") or []
    return len(items) > 0

def create_lead(title: str, source_url: str, category_slug: str) -> int:
    col = leads_collection()
    payload = {"title": title, "source_url": source_url, "category_slug": category_slug, "status": "pending"}
    data = directus_post(f"/items/{col}", payload)
    item = data.get("data") or {}
    return int(item.get("id") or 0)

def get_lead(lead_id: int) -> Dict[str, Any]:
    col = leads_collection()
    data = directus_get(f"/items/{col}/{lead_id}")
    return data.get("data") or {}

def update_lead_status(lead_id: int, status: str) -> None:
    col = leads_collection()
    directus_patch(f"/items/{col}/{lead_id}", {"status": status})

def list_one_approved_lead_newest() -> Optional[Dict[str, Any]]:
    col = leads_collection()
    params = urlencode({"filter[status][_eq]": "approved", "sort": "-id", "limit": "1"})
    data = directus_get(f"/items/{col}?{params}")
    items = data.get("data") or []
    return items[0] if items else None

# -------------------------
# Slack
# -------------------------

def slack_token() -> str:
    return get_setting("slack_bot_token") or ""

def slack_channel() -> str:
    return get_setting("slack_channel_id") or ""

def slack_signing_secret() -> str:
    return get_setting("slack_signing_secret") or ""

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

def slack_post_lead(title: str, category_name: str, lead_id: int) -> Dict[str, Any]:
    token = slack_token()
    channel = slack_channel()
    if not token or not channel:
        raise RuntimeError("Slack bot token or channel ID not configured")
    
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n_{category_name}_"}},
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ Approve"}, "style": "primary", "action_id": "approve", "value": str(lead_id)},
                {"type": "button", "text": {"type": "plain_text", "text": "🚀 Urgent"}, "style": "danger", "action_id": "urgent", "value": str(lead_id)},
                {"type": "button", "text": {"type": "plain_text", "text": "❌ Reject"}, "action_id": "reject", "value": str(lead_id)},
            ],
        },
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
    
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"✅ *Published:* {title}"}},
    ]
    
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"channel": channel, "ts": ts, "text": f"Published: {title}", "blocks": blocks}
    request_with_retry("POST", "https://slack.com/api/chat.update", headers=headers, json_body=payload)

def slack_ephemeral(response_url: str, text: str) -> None:
    if not response_url:
        return
    payload = {"text": text, "response_type": "ephemeral", "replace_original": False}
    request_with_retry("POST", response_url, json_body=payload)

# -------------------------
# RSS feed parsing
# -------------------------

def parse_feed(url: str):
    ua = get_setting("user_agent", "TechNewsBot/1.0")
    timeout = int(get_setting("http_timeout", "30"))
    resp = requests.get(url, headers={"User-Agent": ua}, timeout=timeout)
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
# LLM chat routing (Together / OpenRouter)
# -------------------------

TOGETHER_CHAT_URL = "https://api.together.xyz/v1/chat/completions"
TOGETHER_IMAGES_URL = "https://api.together.xyz/v1/images/generations"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

def chat_stage(stage: str, messages: List[Dict[str, str]]) -> str:
    """Call LLM for a specific stage using configured routing"""
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
    elif provider == "openrouter":
        return _chat_openrouter(model, messages, temperature, max_tokens)
    else:
        raise RuntimeError(f"Unknown provider: {provider}")

def _chat_together(model: str, messages: List[Dict[str, str]], temperature: float, max_tokens: int) -> str:
    key = get_setting("together_api_key")
    if not key:
        raise RuntimeError("Together API key not configured")
    
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    
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
    
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://bot.gadgeek.in",
        "X-Title": "Gadgeek Tech News",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    
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
        return {"extract": {"results": []}}
    
    headers = {"Content-Type": "application/json"}
    payload = {
        "api_key": key,
        "query": title,
        "search_depth": "advanced",
        "max_results": 5,
        "include_raw_content": True,
        "include_images": True,
    }
    
    try:
        resp = request_with_retry("POST", "https://api.tavily.com/search", headers=headers, json_body=payload, timeout=60, max_attempts=2)
        data = resp.json()
        return {"extract": data}
    except Exception as e:
        LOG.warning("Tavily research failed: %s", e)
        return {"extract": {"results": []}}

def pick_extracted_image(pack: Dict[str, Any]) -> Optional[Dict[str, str]]:
    results = (pack.get("extract") or {}).get("results") or []
    images = (pack.get("extract") or {}).get("images") or []
    
    # Prefer images from results
    for r in results:
        img_url = r.get("image")
        if img_url and img_url.startswith("http"):
            return {"url": img_url, "credit": r.get("url", "Source"), "caption": "Featured image"}
    
    # Fallback to general images
    if images and images[0].startswith("http"):
        return {"url": images[0], "credit": "Web", "caption": "Featured image"}
    
    return None

# -------------------------
# Image generation (routing-based)
# -------------------------

def generate_image(prompt: str) -> Optional[Dict[str, str]]:
    """Generate image using configured routing"""
    routes = get_model_routes()
    route = routes.get("image")
    if not route:
        LOG.warning("No image route configured; using default Together")
        return generate_image_together(prompt, 1024, 768)
    
    provider = route["provider"]
    model = route["model"]
    width = route.get("width", 1024)
    height = route.get("height", 768)
    
    if provider == "together":
        return generate_image_together(prompt, width, height, model)
    elif provider == "openrouter":
        return generate_image_openrouter(prompt, width, height, model)
    else:
        raise RuntimeError(f"Unknown image provider: {provider}")

def generate_image_together(prompt: str, width: int = 1024, height: int = 768, model: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Generate image using Together AI"""
    key = get_setting("together_api_key")
    if not key:
        LOG.warning("Together API key not set")
        return None
    
    if model is None:
        model = "black-forest-labs/FLUX.1-schnell"
    
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "prompt": prompt,
        "width": width,
        "height": height,
        "steps": 4,
        "n": 1,
        "response_format": "url",
        "output_format": "jpeg",
    }
    
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

def generate_image_openrouter(prompt: str, width: int = 1024, height: int = 768, model: str = "openai/dall-e-3") -> Optional[Dict[str, str]]:
    """Generate image using OpenRouter (requires image-capable model)"""
    key = get_setting("openrouter_api_key")
    if not key:
        LOG.warning("OpenRouter API key not set")
        return None
    
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://bot.gadgeek.in",
        "X-Title": "Gadgeek Tech News",
    }
    
    # OpenRouter uses chat completions format for DALL-E
    messages = [{"role": "user", "content": prompt}]
    payload = {
        "model": model,
        "messages": messages,
    }
    
    try:
        resp = request_with_retry("POST", OPENROUTER_CHAT_URL, headers=headers, json_body=payload, timeout=120, max_attempts=3)
        data = resp.json()
        
        # Extract image URL from response
        choices = data.get("choices") or []
        if choices:
            content = (choices[0].get("message") or {}).get("content") or ""
            # Try to extract URL from content (format varies by model)
            import re
            urls = re.findall(r'https?://[^\s<>"]+', content)
            if urls:
                return {"url": urls[0], "credit": "AI-generated (OpenRouter)", "caption": "AI-generated illustration"}
    except Exception as e:
        LOG.warning("OpenRouter image generation failed: %s", e)
    
    return None

# -------------------------
# Text utilities
# -------------------------

def slugify(text: str, max_len: int = 80) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text).strip("-")
    if len(text) > max_len:
        text = text[:max_len].rstrip("-")
    if not text:
        text = "tech-news"
    return text

def _sanitize_json(s: str) -> str:
    s = s.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    return s

def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    t = text.strip()
    t = _sanitize_json(t)
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
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
                block = t[start:i+1]
                block = _sanitize_json(block)
                try:
                    obj = json.loads(block)
                    if isinstance(obj, dict):
                        return obj
                except Exception:
                    return None
    return None

# -------------------------
# Article pipeline
# -------------------------

def build_generation_prompt(title: str, category_name: str, pack: Dict[str, Any]) -> List[Dict[str, str]]:
    extract_results = (pack.get("extract") or {}).get("results") or []
    snippets = []
    for r in extract_results[:5]:
        url = r.get("url") or ""
        content = (r.get("content") or r.get("raw_content") or "").strip()
        if len(content) > 1500:
            content = content[:1500] + "…"
        snippets.append(f"SOURCE: {url}\n{content}")
    sources_block = "\n\n".join(snippets) if snippets else "No extracted sources available."

    system = (
        "You are a professional tech journalist. Write accurate, reader-friendly articles grounded ONLY in the provided sources. "
        "Do not invent facts, numbers, dates, or quotes. If something is uncertain, say so.\n\n"
        "Return ONLY HTML (no markdown)."
    )
    user = (
        f"Write a tech news article about: {title}\n"
        f"Category: {category_name}\n\n"
        "Required structure:\n"
        "1) <h3>Article Highlights</h3> with 2-3 <li> bullets\n"
        "2) <p>Hook</p> 120-150 words\n"
        "3) 4-5 <h2> sections, each 100-200 words. Add <h3> subheadings when useful.\n"
        "4) Use bullet lists or numbering where helpful\n"
        "5) Include one simple HTML <table> when it helps (specs, timeline, comparison).\n"
        "6) End with a <h3>Sources</h3> list of source URLs.\n\n"
        "Sources (use as the only truth):\n"
        f"{sources_block}\n"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]

def humanize_prompt(html: str) -> List[Dict[str, str]]:
    system = (
        "You are an editor. Improve readability and flow while keeping ALL facts identical. "
        "Do not add new facts. Do not remove citations list. Keep output as HTML only."
    )
    user = (
        "Rewrite the following HTML article to be more natural and user-friendly:\n\n"
        f"{html}"
    )
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

def build_image_prompt(title: str) -> str:
    return (
        f"High-quality realistic product-style hero image illustrating the tech topic: {title}. "
        "No people, no brand logos, no text. Studio lighting, clean background. Looks like a magazine illustration."
    )

def create_article_from_lead(title: str, category_name: str) -> Dict[str, Any]:
    pack = build_research_pack(title)
    extracted_img = pick_extracted_image(pack) if get_setting("prefer_extracted_image", "1") == "1" else None

    # Draft
    draft_html = chat_stage("generation", build_generation_prompt(title, category_name, pack))
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

    # Image selection
    img = extracted_img
    if not img:
        gen_img = generate_image(build_image_prompt(title))
        img = gen_img

    featured_image = img["url"] if img else ""
    caption = (img.get("caption") or "Tech illustration") if img else ""
    credit = img.get("credit") or "" if img else ""
    featured_image_credit = f"{caption} | Credit: {credit}".strip(" |")

    slug = slugify(title)
    published_at = _now_iso()

    return {
        "title": title,
        "slug": f"{slug}-{hashlib.md5(title.encode('utf-8')).hexdigest()[:6]}",
        "status": "published",
        "category_slug": category_name,
        "short_description": short_description,
        "content": human_html,
        "featured_image": featured_image,
        "featured_image_credit": featured_image_credit,
        "featured_image_alt": image_alt,
        "meta_title": meta_title,
        "meta_description": meta_description,
        "tags": tags,
        "published_at": published_at,
    }

def publish_article_to_directus(article: Dict[str, Any], category_slug: str) -> Dict[str, Any]:
    col = articles_collection()
    payload = dict(article)
    payload["category_slug"] = category_slug
    
    required = {"title", "slug", "status", "category_slug", "content"}
    clean = {}
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