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

from config import REDIS_HOST, REDIS_PORT, REDIS_DB, REDIS_USERNAME, REDIS_PASSWORD

LOG = logging.getLogger("technews")

# Default fallback category UUID when no keyword match is found.
DEFAULT_CATEGORY_UUID = "3229ec20-3076-4a32-9fa2-88b65dacfedf"

# Static configuration constants
HTTP_TIMEOUT = 60  # seconds
USER_AGENT = "Mozilla/5.0 (compatible; GadgeekBot/2.0; +https://bot.gadgeek.in)"

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
        try:
            _REDIS_CLIENT = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                db=REDIS_DB,
                username=REDIS_USERNAME,
                password=REDIS_PASSWORD,
                decode_responses=True,
                socket_connect_timeout=10,
                socket_timeout=10,
            )
            # Test the connection
            _REDIS_CLIENT.ping()
            LOG.info(f"✅ Redis connected successfully to {REDIS_HOST}:{REDIS_PORT}")
        except Exception as e:
            LOG.error(f"❌ Redis connection failed: {e}")
            LOG.error(f"Host: {REDIS_HOST}, Port: {REDIS_PORT}, Username: {REDIS_USERNAME}")
            raise RuntimeError(f"Cannot connect to Redis at {REDIS_HOST}:{REDIS_PORT}: {e}") from e
    return _REDIS_CLIENT


def get_redis_client() -> redis.Redis:
    """Public wrapper for the shared Redis client."""
    return _get_redis()

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
    try:
        r = _get_redis()
        
        # Initialize default settings if not present
        for k, v in DEFAULTS_SETTINGS.items():
            key = f"settings:{k}"
            try:
                if not r.exists(key):
                    r.hset(key, mapping={"value": v, "updated_at": _now_iso()})
            except Exception as e:
                LOG.warning(f"Failed to initialize setting {k}: {e}")
        
        # Initialize default model routes if not present
        for stage, cfg in DEFAULT_MODEL_ROUTES.items():
            key = f"model_routes:{stage}"
            try:
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
            except Exception as e:
                LOG.warning(f"Failed to initialize model route {stage}: {e}")
        
        LOG.info("Redis initialized with default settings")
    except Exception as e:
        LOG.error(f"Failed to initialize Redis: {e}")
        raise

def get_setting(key: str, default: Optional[str] = None) -> str:
    """Get a setting from Redis, returning default if empty or not found"""
    r = _get_redis()
    value = r.hget(f"settings:{key}", "value")
    return value if value else (default or "")

def set_setting(key: str, value: str) -> None:
    """Set a setting in Redis"""
    r = _get_redis()
    r.hset(f"settings:{key}", mapping={"value": value, "updated_at": _now_iso()})

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
        # Skip the counter key
        if redis_key == "feed:next_id":
            continue
        data = r.hgetall(redis_key)
        if data:
            try:
                feed_id = int(redis_key.replace("feed:", ""))
            except ValueError:
                # Skip non-numeric feed keys
                continue
            feeds.append({
                "id": feed_id,
                "url": data.get("url", ""),
                "enabled": data.get("enabled", "1") == "1",
                "title_key": data.get("title_key") or "",
                "description_key": data.get("description_key") or "",
                "content_key": data.get("content_key") or "",
                "category_key": data.get("category_key") or "",
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
        if redis_key == "feed:next_id":
            continue
        data = r.hgetall(redis_key)
        if data.get("url") == url:
            try:
                feed_id = int(redis_key.replace("feed:", ""))
                break
            except ValueError:
                continue
    
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
        if not stage:  # Skip empty stage names
            continue
        data = r.hgetall(redis_key)
        if data:
            route = {
                "provider": data.get("provider", ""),
                "model": data.get("model", ""),
            }
            if "temperature" in data and data["temperature"]:
                try:
                    route["temperature"] = float(data["temperature"])
                except (ValueError, TypeError):
                    pass
            if "max_tokens" in data and data["max_tokens"]:
                try:
                    route["max_tokens"] = int(data["max_tokens"])
                except (ValueError, TypeError):
                    pass
            if "width" in data and data["width"]:
                try:
                    route["width"] = int(data["width"])
                except (ValueError, TypeError):
                    pass
            if "height" in data and data["height"]:
                try:
                    route["height"] = int(data["height"])
                except (ValueError, TypeError):
                    pass
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
    timeout: int = HTTP_TIMEOUT,
    max_attempts: int = 3,
) -> Response:
    hdrs = headers or {}
    if "User-Agent" not in hdrs:
        hdrs["User-Agent"] = USER_AGENT
    
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
    return get_setting("directus_url", "").rstrip("/")

def directus_token() -> str:
    return get_setting("directus_token", "")

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


def import_image_to_directus(image_url: str, title: str = "") -> Optional[str]:
    """Import an image into Directus files and return the file UUID.

    Supports both:
    - HTTP(S) URLs: uses Directus /files/import endpoint
    - data:image base64 URIs: decodes and uploads via multipart form
    """
    if not image_url:
        return None

    base = directus_url()
    token = directus_token()
    if not base or not token:
        LOG.warning("Directus URL or token not configured; cannot import image.")
        return None

    try:
        # Handle base64 data URIs (from Together AI b64_json responses)
        if image_url.startswith("data:image"):
            return _upload_base64_image_to_directus(image_url, title, base, token)

        # Handle HTTP URLs (from Tavily extracted images or Together URL responses)
        if image_url.startswith("http"):
            return _import_url_image_to_directus(image_url, title, base, token)

        LOG.warning("Unsupported image format (not http or data:image): %s", image_url[:80])
        return None
    except Exception as e:
        LOG.warning("Directus image import error: %s", e)
        return None


def _import_url_image_to_directus(image_url: str, title: str, base: str, token: str) -> Optional[str]:
    """Import an image from URL into Directus via /files/import."""
    import_url = f"{base}/files/import"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "url": image_url,
        "data": {
            "title": title[:255] if title else "Featured image",
        },
    }

    resp = requests.post(import_url, headers=headers, json=payload, timeout=120)
    if resp.status_code >= 400:
        LOG.warning("Directus URL image import failed (%s): %s", resp.status_code, resp.text[:500])
        return None
    data = resp.json()
    file_id = (data.get("data") or {}).get("id")
    if file_id:
        LOG.info("Image imported to Directus via URL: %s -> %s", image_url[:80], file_id)
        return str(file_id)
    LOG.warning("Directus URL import returned no file ID: %s", resp.text[:300])
    return None


def _upload_base64_image_to_directus(data_uri: str, title: str, base: str, token: str) -> Optional[str]:
    """Upload a base64 data URI image to Directus via multipart /files upload."""
    # Parse data URI: data:image/jpeg;base64,/9j/4AAQ...
    match = re.match(r"data:image/(\w+);base64,(.+)", data_uri, re.DOTALL)
    if not match:
        LOG.warning("Could not parse base64 data URI")
        return None

    img_format = match.group(1)  # jpeg, png, etc.
    b64_data = match.group(2)
    img_bytes = base64.b64decode(b64_data)

    upload_url = f"{base}/files"
    headers = {"Authorization": f"Bearer {token}"}
    files = {
        "file": (f"featured-image.{img_format}", img_bytes, f"image/{img_format}"),
    }
    form_data = {
        "title": title[:255] if title else "Featured image",
    }

    resp = requests.post(upload_url, headers=headers, files=files, data=form_data, timeout=120)
    if resp.status_code >= 400:
        LOG.warning("Directus base64 image upload failed (%s): %s", resp.status_code, resp.text[:500])
        return None
    data = resp.json()
    file_id = (data.get("data") or {}).get("id")
    if file_id:
        LOG.info("Image uploaded to Directus via base64: %s", file_id)
        return str(file_id)
    LOG.warning("Directus base64 upload returned no file ID: %s", resp.text[:300])
    return None

def get_categories() -> List[Dict[str, Any]]:
    """Fetch enabled categories from Directus"""
    col = categories_collection()
    params = urlencode({"filter[enabled][_eq]": "true", "sort": "priority", "limit": "-1"})
    data = directus_get(f"/items/{col}?{params}")
    cats = data.get("data") or []
    
    if not cats:
        LOG.warning("No categories found in Directus")
        return []
    
    result = []
    for c in cats:
        try:
            
            # Safe int conversion helper
            def safe_int(val, default):
                if val in (None, "", "null"):
                    return default
                try:
                    return int(float(val))
                except (ValueError, TypeError):
                    return default
            
            result.append({
                "id": c.get("id"),
                "slug": c.get("slug", ""),
                "name": c.get("name", ""),
                "priority": safe_int(c.get("priority"), 999),
                "posts_per_scout": safe_int(c.get("posts_per_scout"), 0),
                "prompt_generation": c.get("prompt_generation") or "",
            })
        except Exception as e:
            LOG.error(f"Failed to process category {c.get('slug', 'unknown')}: {e}")
            continue
    
    return result

def lead_exists_by_url(source_url: str) -> bool:
    col = leads_collection()
    params = urlencode({"filter[source_url][_eq]": source_url, "limit": "1"})
    data = directus_get(f"/items/{col}?{params}")
    items = data.get("data") or []
    return len(items) > 0

def create_lead(title: str, source_url: str, category_id: str) -> str:
    col = leads_collection()
    payload = {"title": title, "source_url": source_url, "category": category_id, "status": "pending"}
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

def list_one_approved_lead_newest(status: str = "approved") -> Optional[Dict[str, Any]]:
    """Fetch one lead (newest) for a given status.

    UUID ids are not sortable chronologically, so we sort by date_created.
    """
    col = leads_collection()
    params = urlencode({"filter[status][_eq]": status, "sort": "-date_created", "limit": "1"})
    data = directus_get(f"/items/{col}?{params}")
    items = data.get("data") or []
    return items[0] if items else None

def get_category_by_id(category_id: str) -> Optional[Dict[str, Any]]:
    for c in get_categories():
        if str(c.get("id") or "") == str(category_id):
            return c
    return None

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

def delete_slack_message(channel_id: str, message_ts: str) -> bool:
    """Delete a Slack message after action"""
    token = slack_token()
    if not token:
        LOG.warning("Slack token not configured")
        return False
    
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"channel": channel_id, "ts": message_ts}
    
    try:
        resp = request_with_retry("POST", "https://slack.com/api/chat.delete", headers=headers, json_body=payload)
        data = resp.json()
        
        if data.get("ok"):
            print(f"   ✅ Deleted Slack message", flush=True)
            return True
        else:
            print(f"   ⚠️ Failed to delete: {data.get('error')}", flush=True)
            return False
            
    except Exception as e:
        print(f"   ⚠️ Failed to delete message: {e}", flush=True)
        return False
# -------------------------
# RSS feed parsing
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
# LLM chat routing (Together / OpenRouter)
# -------------------------

TOGETHER_CHAT_URL = "https://api.together.xyz/v1/chat/completions"
TOGETHER_IMAGES_URL = "https://api.together.xyz/v1/images/generations"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_IMAGES_URL = "https://openrouter.ai/api/v1/images/generations"

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
        "search_depth": "basic",
        "max_results": 7,
        "time_range": "week",
        "include_raw_content": True,
        "include_images": True,
        "include_answer": False,
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
        LOG.warning("No image route configured; trying OpenRouter then Together fallback")
        # Try OpenRouter first (user's preferred provider)
        result = generate_image_openrouter(prompt, 832, 448)
        if result:
            return result
        # Fallback to Together
        LOG.info("OpenRouter image failed, falling back to Together")
        return generate_image_together(prompt, 832, 448)

    provider = route["provider"]
    model = route["model"]
    width = route.get("width", 832)
    height = route.get("height", 448)

    if provider == "together":
        return generate_image_together(prompt, width, height, model)
    elif provider == "openrouter":
        return generate_image_openrouter(prompt, width, height, model)
    else:
        raise RuntimeError(f"Unknown image provider: {provider}")


def generate_image_together(
    prompt: str,
    width: int = 832,
    height: int = 448,
    model: Optional[str] = None,
) -> Optional[Dict[str, str]]:
    """Generate image using Together AI"""
    key = get_setting("together_api_key")
    if not key:
        LOG.warning("Together API key not set — skipping Together image generation")
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

    LOG.info("Together image request — model: %s, size: %dx%d", model, width, height)

    try:
        resp = request_with_retry(
            "POST", TOGETHER_IMAGES_URL,
            headers=headers, json_body=payload,
            timeout=120, max_attempts=3,
        )
        data = resp.json()
        LOG.debug("Together raw response keys: %s", list(data.keys()))

        item = (data.get("data") or [{}])[0]

        if item.get("url"):
            LOG.info("Together image generated successfully (url)")
            return {"url": item["url"], "credit": "AI-generated (Together)", "caption": "AI-generated illustration"}
        if item.get("b64_json"):
            LOG.info("Together image generated successfully (base64)")
            return {"url": f"data:image/jpeg;base64,{item['b64_json']}", "credit": "AI-generated (Together)", "caption": "AI-generated illustration"}

        LOG.warning("Together response had no url or b64_json. Item keys: %s", list(item.keys()))
    except Exception as e:
        LOG.warning("Together image generation failed: %s", e)

    return None


def generate_image_openrouter(
    prompt: str,
    width: int = 832,
    height: int = 448,
    model: str = "black-forest-labs/flux-1.1-pro",
) -> Optional[Dict[str, str]]:
    """Generate image using OpenRouter's images endpoint (OpenAI-compatible)"""
    key = get_setting("openrouter_api_key")
    if not key:
        LOG.warning("OpenRouter API key not set — skipping OpenRouter image generation")
        return None

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://bot.gadgeek.in",
        "X-Title": "Gadgeek Tech News",
    }

    payload = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": f"{width}x{height}",
    }

    LOG.info("OpenRouter image request — model: %s, size: %dx%d", model, width, height)

    try:
        resp = request_with_retry(
            "POST", OPENROUTER_IMAGES_URL,
            headers=headers, json_body=payload,
            timeout=120, max_attempts=3,
        )

        # Check for HTTP errors
        if resp.status_code != 200:
            LOG.warning(
                "OpenRouter image returned HTTP %d: %s",
                resp.status_code, resp.text[:500],
            )
            return None

        data = resp.json()
        LOG.debug("OpenRouter raw response keys: %s", list(data.keys()))

        # Check for API-level errors
        if data.get("error"):
            LOG.warning("OpenRouter image API error: %s", data["error"])
            return None

        # Standard OpenAI images response format: {"data": [{"url": "..."} or {"b64_json": "..."}]}
        items = data.get("data") or []
        if not items:
            LOG.warning("OpenRouter image response had empty data array. Full response: %s", str(data)[:500])
            return None

        item = items[0]

        if item.get("url"):
            LOG.info("OpenRouter image generated successfully (url)")
            return {"url": item["url"], "credit": "AI-generated (OpenRouter)", "caption": "AI-generated illustration"}
        if item.get("b64_json"):
            LOG.info("OpenRouter image generated successfully (base64)")
            return {"url": f"data:image/png;base64,{item['b64_json']}", "credit": "AI-generated (OpenRouter)", "caption": "AI-generated illustration"}

        LOG.warning("OpenRouter item had no url or b64_json. Item keys: %s", list(item.keys()))

    except Exception as e:
        LOG.warning("OpenRouter image generation failed: %s", e)
        import traceback
        LOG.debug(traceback.format_exc())

    return None

# -------------------------
# Text utilities
# -------------------------

def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences (```html ... ```) that LLMs sometimes wrap output in."""
    t = text.strip()
    t = re.sub(r"^```(?:html|HTML)?\s*\n?", "", t)
    t = re.sub(r"\n?```\s*$", "", t)
    return t.strip()

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

ALLOWED_PROMPT_VARS = {"title", "category", "sources_block"}

# ---------------------------------------------------------------------------
# DEFAULT GENERATION TEMPLATE
# Used when a Directus category has no custom prompt_generation field.
# Placeholders: {title}, {category}, {sources_block}
# ---------------------------------------------------------------------------
DEFAULT_GENERATION_TEMPLATE = """Write a comprehensive, well-researched tech news article for the "{category}" section.

TOPIC: {title}

═══════════════════════════════════════
GROUND RULES — read these carefully
═══════════════════════════════════════
• Base every claim on the SOURCES below. Never invent facts, stats, dates, quotes, or specs.
• When sources conflict or information is uncertain, say so explicitly.
• Output ONLY valid HTML. Allowed tags: h2, h3, h4, p, ul, ol, li, table, thead, tbody, tr, th, td, strong, em. No inline styles, no CSS, no markdown fences, no code blocks.
• Target length: 1200–1600 words of body text. This is non-negotiable — do NOT stop before all sections are complete.

═══════════════════════════════════════
ARTICLE STRUCTURE (follow this exact order)
═══════════════════════════════════════

1. HIGHLIGHTS
   <h3>Article Highlights</h3>
   <ul> with exactly 3 <li> items — each one sentence capturing a key takeaway. </ul>

2. HOOK PARAGRAPH
   A single <p> tag with 2-3 compelling sentences. Set the scene, state why this matters right now, hint at what the reader will learn. Do NOT use a heading here.

3. MAIN BODY — exactly 5 sections, each with an <h2> heading
   • Each section: 180–280 words.
   • Use <h3> or <h4> sub-headings inside a section when covering multiple sub-topics.
   • Choose section topics organically based on the news (e.g., specs & features, pricing & availability, competitive context, industry impact, what comes next). Do NOT use generic filler headings.
   • Weave in at least one <table> (for specs, pricing tiers, or comparisons) and at least two <ul> or <ol> lists across the body where they genuinely help.
   • Use <strong> to highlight key numbers: prices, dates, performance figures.

4. FAQ (include 3-4 questions)
   <h2>Frequently Asked Questions</h2>
   Format each as <h3>Question?</h3><p>Answer in 2-3 sentences.</p>
   Focus on questions a reader would actually search for.

5. CLOSING
   <h3>Final Thoughts</h3>
   2-3 sentences: summarize the key takeaway and end with a forward-looking statement.

6. SOURCES
   <h3>Sources</h3>
   <ul> listing each source domain only (e.g., <li>theverge.com</li>). No full URLs, no links. </ul>

═══════════════════════════════════════
WRITING VOICE
═══════════════════════════════════════
• Write like an experienced tech journalist — confident, clear, occasionally witty.
• Mix sentence lengths: some short and punchy (6-10 words), some longer and descriptive (20-30 words).
• Use contractions naturally (it's, doesn't, won't, here's, that's).
• Use active voice. Vary paragraph lengths (some 2 sentences, some 4-5).
• Explain jargon briefly when first introduced.
• Be balanced — mention both strengths and weaknesses.
• Avoid marketing fluff, hype words, and filler phrases.
• Label rumors, leaks, and unconfirmed info as such.

═══════════════════════════════════════
SEO NOTES
═══════════════════════════════════════
• Include the main topic keyword in at least 2 of the 5 <h2> headings.
• Use brand names, model numbers, and technical terms naturally in the text.
• Maintain heading hierarchy: H2 → H3 → H4.

═══════════════════════════════════════
SOURCES PROVIDED
═══════════════════════════════════════
{sources_block}

═══════════════════════════════════════
FINAL REMINDER: The article MUST contain all 6 structural sections listed above. Do NOT stop early. Write a minimum of 1200 words of body text. Output the COMPLETE article as valid HTML with no markdown wrapping.
"""


def _sources_block_from_pack(pack: Dict[str, Any]) -> str:
    extract_results = (pack.get("extract") or {}).get("results") or []
    snippets: List[str] = []
    for r in extract_results[:6]:
        url = r.get("url") or ""
        content = (r.get("content") or r.get("raw_content") or "").strip()
        if len(content) > 2500:
            content = content[:2500] + "…"
        snippets.append(f"SOURCE: {url}\n{content}")
    return "\n\n".join(snippets) if snippets else "No extracted sources available."


def render_prompt_template_strict(template: str, *, title: str, category: str, sources_block: str) -> str:
    """Render a prompt template, rejecting unknown placeholders."""
    used = set(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", template or ""))
    unknown = used - ALLOWED_PROMPT_VARS
    if unknown:
        raise RuntimeError(f"Unknown prompt variable(s) in category prompt: {sorted(unknown)}")
    return (template or "").format(title=title, category=category, sources_block=sources_block)

def _strip_document_wrapper(html: str) -> str:
    """
    Remove full HTML document tags and inline styles that slip
    through the humanization prompt despite instructions.
    """
    import re

    # Remove everything before first content tag
    # (strips <!DOCTYPE>, <html>, <head>...</head>, <body> openers)
    html = re.sub(
        r'^.*?(?=<(?:h[1-6]|p|ul|ol|table|blockquote|hr|strong|em)\b)',
        '', html, flags=re.DOTALL | re.IGNORECASE
    )

    # Remove closing document tags at the end
    html = re.sub(
        r'</?(html|head|body|header|footer|nav|section|article|div|span)'
        r'(\s[^>]*)?>',
        '', html, flags=re.IGNORECASE
    )

    # Remove inline style attributes
    html = re.sub(r'\s+style=["\'][^"\']*["\']', '', html, flags=re.IGNORECASE)

    # Remove <title>, <script>, <style> blocks entirely with content
    html = re.sub(
        r'<(title|script|style)\b[^>]*>.*?</\1>',
        '', html, flags=re.DOTALL | re.IGNORECASE
    )

    return html.strip()


def _resolve_article_title(seo: dict, raw_title: str) -> str:
    """
    Pick the best article title from SEO output.
    Priority: article_title → meta_title → raw_title (truncated).
    Always returns 60-90 characters.
    """
    # Try dedicated article_title field first (added to seo_prompt below)
    candidate = (seo.get("article_title") or "").strip().strip('"').strip("'")

    # Fall back to meta_title if article_title missing
    if not candidate:
        candidate = (seo.get("meta_title") or "").strip().strip('"').strip("'")

    # Fall back to raw title if both missing
    if not candidate:
        candidate = raw_title

    # Enforce 90 character hard limit at word boundary
    if len(candidate) > 90:
        candidate = candidate[:87].rsplit(" ", 1)[0].rstrip(".,;:-") + "..."

    # Enforce minimum — if somehow empty, use raw
    return candidate if len(candidate) >= 10 else raw_title


# ---------------------------------------------------------------------------
# HUMANIZE PROMPT
# This is the most critical prompt for passing AI-detection tools.
# ---------------------------------------------------------------------------
def humanize_prompt(html: str) -> List[Dict[str, str]]:
    system = (
        "You are a senior tech journalist who has written for The Verge, Wired, and Ars Technica. "
        "A junior writer drafted the article below. Rewrite it so it sounds like you wrote it "
        "from scratch — opinionated, specific, and unmistakably human.\n\n"

        "PRESERVE WITHOUT EXCEPTION:\n"
        "- Every fact, number, spec, price, date, and source attribution\n"
        "- HTML structure, tags, sections, and tables\n"
        "- Article length — same or longer, never shorter\n\n"

        "BANNED CHARACTERS:\n"
        "- Em-dash (—), en-dash (–), ellipsis (… or ...)\n"
        "- Replace with a comma, period, or parenthesis\n\n"

        "BANNED WORDS — hunt every instance and replace:\n"
        "Furthermore, Moreover, Additionally, However (sentence-start), "
        "In conclusion, It is worth noting, It is important to note, "
        "That being said, Having said that, At the end of the day, "
        "When it comes to, In today's world, In the realm of, "
        "landscape, paradigm, leverage, robust, comprehensive, delve, "
        "seamlessly, cutting-edge, game-changer, groundbreaking, pivotal, "
        "notably, impressive, significant, powerful, ultimately, essentially, "
        "overall, indeed, certainly, absolutely, exciting, streamline, "
        "it could be argued, may or may not, might suggest.\n\n"

        "BANNED PATTERNS — these expose AI writing instantly:\n"
        "- Rhetorical filler questions: 'What does this mean for users?' "
        "  'How does this affect the industry?' — delete them entirely\n"
        "- Closing paragraphs that summarize what was already said — rewrite "
        "  the closing as a bold takeaway, prediction, or lingering thought\n"
        "- Three-part lists in every paragraph (X, Y, and Z structure)\n"
        "- Pairs of sentences that follow identical structure back to back\n"
        "- Academic hedging: 'might suggest', 'could potentially', 'may indicate'\n\n"

        "THE CAFÉ TEST — apply to every sentence:\n"
        "Would a real journalist say this while explaining the article to a friend "
        "over coffee? If it sounds stiff, overly formal, or like a textbook, rewrite it. "
        "Read each paragraph aloud mentally. Robotic rhythm means rewrite.\n\n"

        "WRITE LIKE THIS:\n"
        "- Contractions everywhere: it's, doesn't, won't, can't, isn't, here's, that's\n"
        "- Sentence rhythm: Short. Then one that builds to a point with more detail. "
        "  Then short again. Vary constantly — monotone rhythm is the #1 AI tell\n"
        "- Start sentences with: And, But, So, Or — real writers do this constantly\n"
        "- Fragments for punch. Not often. Just when it lands.\n"
        "- Natural bridges: 'Here's the thing,' 'But look,' 'That said,' 'Now,' "
        "  'The problem is,' 'And yet' — not 'Furthermore' or 'Moreover'\n"
        "- Specificity over vagueness: don't write 'it performs well' — write "
        "  what specifically performs well and by how much\n"
        "- Opinions with a spine: not 'this could be beneficial for users' "
        "  but 'this is genuinely good for anyone who shoots video at night'\n"
        "- Talk to the reader: 'you', 'your'\n"
        "- Zero identical sentence structures within the same paragraph\n"
        "- Zero identical phrases of 4+ words from the original draft\n\n"

        "OUTPUT RULES:\n"
        "- Start at the first HTML tag. Nothing before it.\n"
        "- End at the last closing tag. Nothing after it.\n"
        "- ALLOWED TAGS ONLY: h2, h3, h4, p, ul, ol, li, table, thead, tbody, "
        "tr, th, td, strong, em, a, blockquote, hr\n"
        "- BANNED TAGS: html, head, body, title, header, footer, nav, "
        "section, article, div, span, script, style\n"
        "- BANNED ATTRIBUTES: style='...' or style=\"...\" on any tag\n"
        "- No markdown. No preamble. No closing remarks."
    )

    user = (
        "Rewrite the full article. Apply every rule in the system prompt.\n\n"
        "Priority reminders:\n"
        "- No em-dashes anywhere\n"
        "- No banned words — every instance replaced\n"
        "- No rhetorical filler questions — delete them\n"
        "- No closing paragraph that repeats what was said — end with a real takeaway\n"
        "- Contractions throughout, not just occasionally\n"
        "- Sentence rhythm varied constantly — read it aloud mentally\n"
        "- Genuine restructuring — no sentence structure from the original survives intact\n\n"
        "Complete HTML output, start to finish. Do not stop early.\n\n"
        f"{html}"
    )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def seo_prompt(title: str, category_name: str, html: str) -> List[Dict[str, str]]:
    system = (
        "You are an SEO specialist for a tech news website. "
        "Return ONLY a valid JSON object. No markdown, no explanation, no extra text. "
        "Character limits are hard limits — count every character before you output."
    )

    user = (
        f"Article title: {title}\n"
        f"Category: {category_name}\n\n"

        "Return a JSON object with exactly these keys:\n\n"

        "{\n"
        '  "article_title": "...",\n'
        '  "slug": "...",\n'
        '  "meta_title": "...",\n'
        '  "meta_description": "...",\n'
        '  "short_description": "...",\n'
        '  "tags": ["...", "..."],\n'
        '  "image_alt": "..."\n'
        "}\n\n"

        "FIELD RULES:\n\n"

        "article_title — HARD LIMIT: 60 to 90 characters (count every character):\n"
        "  This is the H1 headline shown on the article page.\n"
        "  Rule: Brand or product name first. One key fact. No filler words. "
        "No em-dashes. No ellipsis.\n"
        "  GOOD (58): 'Samsung Galaxy S25 Ultra Launched With 200MP Camera in India'\n"
        "  GOOD (61): 'OnePlus 13 Review: Best Android Phone Under Rs 70,000 Right Now'\n"
        "  BAD  (94): 'Samsung Officially Announces the Galaxy S25 Ultra With a New "
        "200MP Camera System and Snapdragon 8 Elite Chip'\n\n"

        "slug — 3 to 6 words, lowercase, hyphens only:\n"
        "  Must include brand or product name. No stop words "
        "(the, a, an, of, in, for, on, with, and).\n"
        "  GOOD: 'samsung-galaxy-s25-ultra-review'\n"
        "  GOOD: 'oneplus-13-india-price-specs'\n"
        "  BAD:  'a-full-review-of-the-samsung-galaxy-s25-ultra-camera-and-battery'\n\n"

        "meta_title — HARD LIMIT: 60 characters maximum (count every character):\n"
        "  Shown in browser tab and Google search results. Front-load the keyword.\n"
        "  GOOD (53): 'Samsung Galaxy S25 Ultra Review: Best Camera Yet?'\n"
        "  GOOD (49): 'iOS 18.3 Update: What's New and Who Gets It'\n"
        "  BAD  (72): 'Samsung Galaxy S25 Ultra Full Review With Camera And Battery Test'\n\n"

        "meta_description — HARD LIMIT: 155 characters maximum:\n"
        "  Include the primary keyword. End with a reason to click.\n"
        "  GOOD (148): 'The Samsung Galaxy S25 Ultra brings a 200MP camera and "
        "Snapdragon 8 Elite to India. Here is everything you need to know about "
        "price and availability.'\n\n"

        "short_description — HARD LIMIT: 150 characters maximum:\n"
        "  1 sentence only. Used in article cards and preview snippets.\n"
        "  Must summarise the article's single most important point.\n"
        "  GOOD: 'Samsung Galaxy S25 Ultra goes official in India at Rs 1,29,999 "
        "with a new 200MP camera and Snapdragon 8 Elite chip.'\n"
        "  BAD: 'This article covers the Samsung Galaxy S25 Ultra launch, specs, "
        "price, availability, and camera details in India.'\n\n"

        "tags — 5 to 8 items, all lowercase, no duplicates:\n"
        "  Use specific tags only: brand names, exact model names, specific tech "
        "terms, category names.\n"
        "  GOOD: ['samsung galaxy s25 ultra', 'snapdragon 8 elite', '200mp camera', "
        "'samsung india', 'android flagship 2025']\n"
        "  BAD:  ['technology', 'news', 'india', 'mobile', 'smartphone']\n"
        "  Rule: If a tag could apply to any tech article, it's too generic. Cut it.\n\n"

        "image_alt — 8 to 15 words:\n"
        "  Describe what the featured image would show based on the article subject.\n"
        "  Include the brand and product name.\n"
        "  GOOD: 'Samsung Galaxy S25 Ultra smartphone front and back view in titanium black'\n"
        "  BAD:  'Featured image for the article'\n\n"

        "Article HTML:\n"
        f"{html}\n"
    )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# IMAGE PROMPT
# ---------------------------------------------------------------------------
def build_image_prompt(title: str, category_name: str) -> str:
    return (
        f"Editorial photo for a tech news article: {title}. "
        f"Category: {category_name}. "
        "Clean modern product photography or conceptual tech illustration. "
        "Soft studio lighting, subtle gradient background, shallow depth of field. "
        "Minimalist composition, centered subject, vibrant but natural colors. "
        "No text, no watermarks, no logos, no faces, no hands, no fingers."
    )


# ---------------------------------------------------------------------------
# ARTICLE CREATION PIPELINE
# ---------------------------------------------------------------------------
def create_article_from_lead(
    title: str,
    category_name: str,
    source_url: str = "",
    category_prompt_generation: str = "",
) -> Dict[str, Any]:

    # ── Step 1: Research ─────────────────────────────────────────────────────
    pack = build_research_pack(title)
    extracted_img = (
        pick_extracted_image(pack)
        if get_setting("prefer_extracted_image", "1") == "1"
        else None
    )

    # ── Step 2: Build generation prompt ──────────────────────────────────────
    sources_block = _sources_block_from_pack(pack)
    custom_prompt = (category_prompt_generation or "").strip()
    template = custom_prompt or DEFAULT_GENERATION_TEMPLATE

    rendered = render_prompt_template_strict(
        template,
        title=title,
        category=category_name,
        sources_block=sources_block,
    )

    generation_messages = [
        {"role": "user", "content": rendered},
    ]

    # ── Step 3: Generate draft ────────────────────────────────────────────────
    draft_html = chat_stage("generation", generation_messages)
    if not draft_html.strip():
        raise RuntimeError("Generation returned empty content.")

    draft_html = _strip_code_fences(draft_html)

    # ── Step 3b: Retry if too short ───────────────────────────────────────────
    word_count = len(draft_html.split())
    if word_count < 700:
        LOG.warning(
            "Generation too short (%d words). Retrying with continuation prompt.",
            word_count,
        )
        retry_messages = generation_messages + [
            {"role": "assistant", "content": draft_html},
            {
                "role": "user",
                "content": (
                    "The article above is incomplete — it has only ~{wc} words. "
                    "I need the COMPLETE article with ALL sections (Highlights, Hook, "
                    "5 body sections, FAQ, Final Thoughts, Sources). "
                    "Target minimum 1200 words. "
                    "Please output the full article from the beginning as complete HTML."
                ).format(wc=word_count),
            },
        ]
        retry_html = chat_stage("generation", retry_messages)
        retry_html = _strip_code_fences(retry_html)
        if len(retry_html.split()) > word_count:
            draft_html = retry_html
            LOG.info(
                "Retry produced %d words (was %d).",
                len(retry_html.split()),
                word_count,
            )
        else:
            LOG.warning(
                "Retry did not improve length. Using original %d-word draft.",
                word_count,
            )

    # ── Step 4: Humanize ─────────────────────────────────────────────────────
    human_html = chat_stage("humanize", humanize_prompt(draft_html))
    human_html = _strip_code_fences(human_html)
    human_html = _strip_document_wrapper(human_html)

    # Guard: fall back to draft if humanize truncated badly
    draft_words = len(draft_html.split())
    human_words = len(human_html.split()) if human_html.strip() else 0
    if human_words < draft_words * 0.6:
        LOG.warning(
            "Humanize output too short (%d words vs draft %d). Falling back to draft.",
            human_words,
            draft_words,
        )
        human_html = draft_html

    # ── Step 5: SEO metadata + article title ─────────────────────────────────
    seo_out = chat_stage("seo", seo_prompt(title, category_name, human_html))
    seo_out = _strip_code_fences(seo_out)
    seo = extract_json_object(seo_out) or {}

    # ── Article title: use SEO-generated, fall back to raw title ─────────────
    article_title = _resolve_article_title(seo, title)

    # ── SEO fields ────────────────────────────────────────────────────────────
    meta_title       = seo.get("meta_title") or article_title[:60]
    meta_description = (seo.get("meta_description") or seo.get("short_description") or "")[:155]
    short_description = seo.get("short_description") or meta_description
    tags             = seo.get("tags") if isinstance(seo.get("tags"), list) else []
    image_alt        = seo.get("image_alt") or f"{article_title} - {category_name}"

    # ── Slug: prefer LLM slug, fall back to title-based ──────────────────────
    seo_slug = (seo.get("slug") or "").strip().lower()
    seo_slug = re.sub(r"[^a-z0-9-]", "", seo_slug).strip("-")
    slug = seo_slug if (5 < len(seo_slug) < 80) else slugify(article_title)
    unique_slug = f"{slug}-{hashlib.md5(title.encode('utf-8')).hexdigest()[:6]}"

    # ── Step 6: Featured image ────────────────────────────────────────────────
    featured_image       = ""
    caption              = ""
    credit               = ""

    if extracted_img and extracted_img.get("url"):
        LOG.info("Attempting to import extracted image: %s", extracted_img["url"][:80])
        file_uuid = import_image_to_directus(extracted_img["url"], title=article_title)
        if file_uuid:
            featured_image = file_uuid
            caption        = extracted_img.get("caption") or "Featured image"
            credit         = extracted_img.get("credit") or ""
            LOG.info("Extracted image imported successfully: %s", file_uuid)
        else:
            LOG.warning("Extracted image failed to import.")

    featured_image_credit = f"{caption} | Credit: {credit}".strip(" |")

    # ── Step 7: Assemble final payload ────────────────────────────────────────
    return {
        "title":                 article_title,
        "slug":                  unique_slug,
        "status":                "published",
        "short_description":     short_description,
        "content":               human_html,
        "featured_image":        featured_image,
        "featured_image_credit": featured_image_credit,
        "featured_image_alt":    image_alt,
        "meta_title":            meta_title,
        "meta_description":      meta_description,
        "tags":                  tags,
        "published_at":          _now_iso(),
    }

def publish_article_to_directus(article: Dict[str, Any], category_id: str) -> Dict[str, Any]:
    col = articles_collection()
    payload = dict(article)
    payload["category"] = category_id
    
    required = {"title", "slug", "status", "category", "content"}
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