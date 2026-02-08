# -*- coding: utf-8 -*-

import base64
import datetime as _dt
import functools
import hmac
import hashlib
import json
import logging
import mimetypes
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse

import feedparser
import redis
import requests

from config import REDIS_DB, REDIS_HOST, REDIS_PASSWORD, REDIS_PORT, REDIS_USERNAME

LOG = logging.getLogger("technews")

HTTP_TIMEOUT = 60
USER_AGENT = "Mozilla/5.0 (compatible; GadgeekBot/2.0; +https://bot.gadgeek.in)"
DEFAULT_CATEGORY_UUID = "3229ec20-3076-4a32-9fa2-88b65dacfedf"

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

_HTTP = requests.Session()
_HTTP.headers.update({"User-Agent": USER_AGENT})


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


# -------------------------
# Redis
# -------------------------
_REDIS_CLIENT: Optional[redis.Redis] = None


def _now_iso() -> str:
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _get_redis() -> redis.Redis:
    global _REDIS_CLIENT
    if _REDIS_CLIENT is None:
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
        _REDIS_CLIENT.ping()
        LOG.info("Redis connected to %s:%s", REDIS_HOST, REDIS_PORT)
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
    "humanize": {"provider": "together", "model": "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo", "temperature": 0.7, "max_tokens": 2200},
    "seo": {"provider": "together", "model": "meta-llama/Llama-3.2-3B-Instruct-Turbo", "temperature": 0.4, "max_tokens": 900},
    "image": {"provider": "together", "model": "black-forest-labs/FLUX.1-schnell", "width": 1024, "height": 768},
}


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
    result: Dict[str, str] = {}
    for redis_key in r.scan_iter(match="settings:*"):
        key = redis_key.replace("settings:", "")
        value = r.hget(redis_key, "value")
        if value is not None:
            result[key] = value
    return result


# -------------------------
# RSS feeds
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
        feeds.append(
            {
                "id": feed_id,
                "url": data.get("url", ""),
                "enabled": data.get("enabled", "1") == "1",
                "category_hint": data.get("category_hint") or "",
                "title_key": data.get("title_key") or "",
                "description_key": data.get("description_key") or "",
                "content_key": data.get("content_key") or "",
                "category_key": data.get("category_key") or "",
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at", ""),
            }
        )
    feeds.sort(key=lambda x: x["id"], reverse=True)
    return feeds


def upsert_feed(feed: Dict[str, Any]) -> int:
    r = _get_redis()
    url = (feed.get("url") or "").strip()
    if not url:
        raise ValueError("url required")

    feed_id: Optional[int] = None
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
        feed_id = int(r.incr("feed:next_id"))
        created_at = now
    else:
        existing = r.hgetall(f"feed:{feed_id}")
        created_at = existing.get("created_at", now)

    feed_data = {
        "url": url,
        "enabled": "1" if bool(feed.get("enabled", True)) else "0",
        "category_hint": (feed.get("category_hint") or "").strip(),
        "title_key": (feed.get("title_key") or "").strip(),
        "description_key": (feed.get("description_key") or "").strip(),
        "content_key": (feed.get("content_key") or "").strip(),
        "category_key": (feed.get("category_key") or "").strip(),
        "created_at": created_at,
        "updated_at": now,
    }
    r.hset(f"feed:{feed_id}", mapping=feed_data)
    return feed_id


def delete_feed(feed_id: int) -> None:
    _get_redis().delete(f"feed:{feed_id}")


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
        if data.get("temperature"):
            try:
                route["temperature"] = float(data["temperature"])
            except Exception:
                pass
        if data.get("max_tokens"):
            try:
                route["max_tokens"] = int(float(data["max_tokens"]))
            except Exception:
                pass
        if data.get("width"):
            try:
                route["width"] = int(float(data["width"]))
            except Exception:
                pass
        if data.get("height"):
            try:
                route["height"] = int(float(data["height"]))
            except Exception:
                pass
        routes[stage] = route
    return routes


def set_model_route(
    stage: str,
    provider: str,
    model: str,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
) -> None:
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
# HTTP utilities
# -------------------------

@dataclass
class Response:
    status_code: int
    text: str
    headers: Dict[str, str]

    def json(self) -> Any:
        return json.loads(self.text or "{}")


def request_with_retry(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    timeout: int = HTTP_TIMEOUT,
    max_attempts: int = 3,
) -> Response:
    hdrs = dict(headers or {})
    hdrs.setdefault("User-Agent", USER_AGENT)
    last: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = _HTTP.request(method, url, headers=hdrs, json=json_body, timeout=timeout)
            return Response(status_code=resp.status_code, text=resp.text, headers=dict(resp.headers))
        except Exception as e:
            last = e
            LOG.warning("HTTP attempt %d/%d failed: %s", attempt, max_attempts, e)
            if attempt == max_attempts:
                raise
            time.sleep(min(2 ** attempt, 10))
    raise RuntimeError(f"Unreachable: {last}")


# -------------------------
# Basic Auth
# -------------------------

def require_basic_auth(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        from flask import Response as FlaskResponse
        from flask import request

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


def looks_like_uuid(v: Any) -> bool:
    return isinstance(v, str) and bool(_UUID_RE.match(v.strip()))


def directus_import_file_url(file_url: str, title: Optional[str] = None) -> str:
    payload: Dict[str, Any] = {"url": file_url}
    if title:
        payload["data"] = {"title": title}
    data = directus_post("/files/import", payload)
    file_id = (data.get("data") or {}).get("id")
    if not file_id:
        raise RuntimeError(f"Directus file import returned no id: {data}")
    return str(file_id)


def directus_upload_file_bytes(filename: str, content: bytes, mime: str, title: Optional[str] = None) -> str:
    url = f"{directus_url()}/files"
    headers = {"Authorization": f"Bearer {directus_token()}"}
    files = {"file": (filename, content, mime)}
    form: Dict[str, str] = {}
    if title:
        form["title"] = title
    resp = _HTTP.post(url, headers=headers, files=files, data=form, timeout=HTTP_TIMEOUT)
    if resp.status_code >= 400:
        raise RuntimeError(f"Directus file upload failed: {resp.status_code} {resp.text}")
    data = resp.json()
    file_id = (data.get("data") or {}).get("id")
    if not file_id:
        raise RuntimeError(f"Directus file upload returned no id: {data}")
    return str(file_id)


def _download_url_bytes(file_url: str) -> Tuple[str, bytes, str]:
    r = _HTTP.get(file_url, timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"Image download failed: {r.status_code}")
    parsed = urlparse(file_url)
    name = (os.path.basename(parsed.path) or "image").split("?")[0] or "image"
    if "." not in name:
        name += ".jpg"
    mime, _ = mimetypes.guess_type(name)
    mime = mime or (r.headers.get("Content-Type") or "application/octet-stream")
    return name, r.content, mime


def ensure_directus_file_id(image_ref: str, title: str) -> str:
    s = (image_ref or "").strip()
    if not s:
        raise RuntimeError("No image reference")
    if looks_like_uuid(s):
        return s

    if s.startswith("data:image/") and "base64," in s:
        head, b64 = s.split("base64,", 1)
        m = re.search(r"data:(image/[^;]+);", head)
        mime = m.group(1) if m else "image/jpeg"
        ext = (mime.split("/")[-1] or "jpg").replace("jpeg", "jpg")
        filename = f"{slugify(title, 40)}.{ext}"
        content = base64.b64decode(b64.encode("utf-8"))
        return directus_upload_file_bytes(filename, content, mime, title=title)

    if s.lower().startswith(("http://", "https://")):
        try:
            return directus_import_file_url(s, title=title)
        except Exception as e:
            LOG.warning("/files/import failed, falling back to upload: %s", e)
            filename, content, mime = _download_url_bytes(s)
            return directus_upload_file_bytes(filename, content, mime, title=title)

    raise RuntimeError("Unsupported image reference")


def get_categories() -> List[Dict[str, Any]]:
    col = categories_collection()
    params = urlencode({"filter[enabled][_eq]": "true", "sort": "priority", "limit": "-1"})
    data = directus_get(f"/items/{col}?{params}")
    cats = data.get("data") or []

    def safe_int(v: Any, default: int) -> int:
        try:
            return int(float(v))
        except Exception:
            return default

    out: List[Dict[str, Any]] = []
    for c in cats:
        cid = c.get("id")
        if not cid:
            continue
        kw = c.get("keywords", [])
        if isinstance(kw, str):
            try:
                kw2 = json.loads(kw)
                kw = kw2 if isinstance(kw2, list) else []
            except Exception:
                kw = [x.strip() for x in kw.split(",") if x.strip()]
        if not isinstance(kw, list):
            kw = []
        kw = [str(x).strip() for x in kw if str(x).strip()]
        out.append(
            {
                "id": str(cid),
                "slug": c.get("slug", "") or "",
                "name": c.get("name", "") or "",
                "priority": safe_int(c.get("priority"), 999),
                "posts_per_scout": safe_int(c.get("posts_per_scout"), 0),
                "keywords": kw,
            }
        )
    return out


def lead_exists_by_url(source_url: str) -> bool:
    col = leads_collection()
    params = urlencode({"filter[source_url][_eq]": source_url, "limit": "1"})
    data = directus_get(f"/items/{col}?{params}")
    return bool(data.get("data") or [])


def create_lead(title: str, source_url: str, category_id: str) -> str:
    col = leads_collection()
    payload = {"title": title, "source_url": source_url, "category": category_id or DEFAULT_CATEGORY_UUID, "status": "pending"}
    data = directus_post(f"/items/{col}", payload)
    item = data.get("data") or {}
    lead_id = item.get("id")
    if not lead_id:
        raise RuntimeError(f"Directus create_lead returned no id (data: {item})")
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
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n_{category_name}_"}},
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Approve"}, "style": "primary", "action_id": "approve", "value": str(lead_id)},
                {"type": "button", "text": {"type": "plain_text", "text": "Urgent"}, "style": "danger", "action_id": "urgent", "value": str(lead_id)},
                {"type": "button", "text": {"type": "plain_text", "text": "Reject"}, "action_id": "reject", "value": str(lead_id)},
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
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"Published: *{title}*"}}]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"channel": channel, "ts": ts, "text": f"Published: {title}", "blocks": blocks}
    request_with_retry("POST", "https://slack.com/api/chat.update", headers=headers, json_body=payload)


def slack_ephemeral(response_url: str, text: str) -> None:
    if not response_url:
        return
    payload = {"text": text, "response_type": "ephemeral", "replace_original": False}
    request_with_retry("POST", response_url, json_body=payload)


# -------------------------
# RSS parsing
# -------------------------

def parse_feed(url: str):
    resp = _HTTP.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
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
# LLM + image generation
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
        return _chat_together(model, messages, float(temperature), int(max_tokens))
    if provider == "openrouter":
        return _chat_openrouter(model, messages, float(temperature), int(max_tokens))
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

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://bot.gadgeek.in",
        "X-Title": "Gadgeek Tech News",
    }
    payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    resp = request_with_retry("POST", OPENROUTER_CHAT_URL, headers=headers, json_body=payload, timeout=120, max_attempts=3)
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenRouter returned no choices: {data}")
    return (choices[0].get("message") or {}).get("content") or ""


def build_research_pack(title: str) -> Dict[str, Any]:
    key = get_setting("tavily_api_key")
    if not key:
        LOG.warning("Tavily API key not set; skipping research")
        return {"extract": {"results": [], "images": []}}

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
        return {"extract": resp.json()}
    except Exception as e:
        LOG.warning("Tavily research failed: %s", e)
        return {"extract": {"results": [], "images": []}}


def pick_extracted_image(pack: Dict[str, Any]) -> Optional[Dict[str, str]]:
    extract = pack.get("extract") or {}
    results = extract.get("results") or []
    images = extract.get("images") or []

    for r in results:
        img_url = r.get("image")
        if img_url and str(img_url).startswith("http"):
            return {"url": str(img_url), "credit": r.get("url", "Source"), "caption": "Featured image"}

    if images and str(images[0]).startswith("http"):
        return {"url": str(images[0]), "credit": "Web", "caption": "Featured image"}

    return None


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
        LOG.warning("Together API key not set")
        return None

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


def generate_image_openrouter(prompt: str, width: int, height: int, model: str) -> Optional[Dict[str, str]]:
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

    messages = [{"role": "user", "content": prompt}]
    payload = {"model": model, "messages": messages}

    try:
        resp = request_with_retry("POST", OPENROUTER_CHAT_URL, headers=headers, json_body=payload, timeout=120, max_attempts=3)
        data = resp.json()
        choices = data.get("choices") or []
        if choices:
            content = (choices[0].get("message") or {}).get("content") or ""
            urls = re.findall(r"https?://[^\s<>\"]+", content)
            if urls:
                return {"url": urls[0], "credit": "AI-generated (OpenRouter)", "caption": "AI-generated illustration"}
    except Exception as e:
        LOG.warning("OpenRouter image generation failed: %s", e)

    return None


# -------------------------
# Text helpers
# -------------------------

def slugify(text: str, max_len: int = 80) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text).strip("-")
    if len(text) > max_len:
        text = text[:max_len].rstrip("-")
    return text or "tech-news"


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
                block = _sanitize_json(t[start : i + 1])
                try:
                    obj = json.loads(block)
                    return obj if isinstance(obj, dict) else None
                except Exception:
                    return None
    return None


# -------------------------
# Article pipeline
# -------------------------

def build_generation_prompt(title: str, category_name: str, pack: Dict[str, Any]) -> List[Dict[str, str]]:
    extract_results = (pack.get("extract") or {}).get("results") or []
    snippets: List[str] = []
    for r in extract_results[:5]:
        url = r.get("url") or ""
        content = (r.get("content") or r.get("raw_content") or "").strip()
        if len(content) > 1500:
            content = content[:1500] + "..."
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
    user = "Rewrite the following HTML article to be more natural and user-friendly:\n\n" + (html or "")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def seo_prompt(title: str, category_name: str, html: str) -> List[Dict[str, str]]:
    system = "You are an SEO editor for a tech news site. Return STRICT JSON only."
    user = (
        "Given the article HTML, produce SEO metadata.\n"
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
        "No people, no brand logos, no text. Studio lighting, clean background."
    )


def create_article_from_lead(title: str, category_name: str) -> Dict[str, Any]:
    pack = build_research_pack(title)
    extracted_img = pick_extracted_image(pack) if get_setting("prefer_extracted_image", "1") == "1" else None

    draft_html = chat_stage("generation", build_generation_prompt(title, category_name, pack))
    if not draft_html.strip():
        raise RuntimeError("Generation returned empty content")

    human_html = chat_stage("humanize", humanize_prompt(draft_html))
    if not human_html.strip():
        human_html = draft_html

    seo_out = chat_stage("seo", seo_prompt(title, category_name, human_html))
    seo = extract_json_object(seo_out) or {}

    meta_title = (seo.get("meta_title") or title)[:60]
    meta_description = (seo.get("meta_description") or (seo.get("short_description") or ""))[:155]
    short_description = seo.get("short_description") or meta_description
    tags = seo.get("tags") if isinstance(seo.get("tags"), list) else []
    image_alt = seo.get("image_alt") or f"{title} - {category_name}"

    img = extracted_img or generate_image(build_image_prompt(title))
    featured_image = (img or {}).get("url", "")
    caption = (img or {}).get("caption") or "Tech illustration"
    credit = (img or {}).get("credit") or ""
    featured_image_credit = f"{caption} | Credit: {credit}".strip(" |")

    slug = slugify(title)
    published_at = _now_iso()

    return {
        "title": title,
        "slug": f"{slug}-{hashlib.md5(title.encode('utf-8')).hexdigest()[:6]}",
        "status": "published",
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


def publish_article_to_directus(article: Dict[str, Any], category_id: str) -> Dict[str, Any]:
    col = articles_collection()
    payload = dict(article)
    payload["category"] = category_id or DEFAULT_CATEGORY_UUID

    title = payload.get("title") or ""

    # Convert featured_image URL/data-url to file UUID.
    fi = (payload.get("featured_image") or "").strip()
    if fi:
        try:
            payload["featured_image"] = ensure_directus_file_id(fi, title=title)
        except Exception as e1:
            LOG.warning("Featured image upload failed, trying AI fallback: %s", e1)
            try:
                gen = generate_image(build_image_prompt(title))
                if gen and gen.get("url"):
                    payload["featured_image"] = ensure_directus_file_id(gen["url"], title=title)
                else:
                    payload.pop("featured_image", None)
            except Exception as e2:
                LOG.warning("AI fallback failed, publishing without featured_image: %s", e2)
                payload.pop("featured_image", None)

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
