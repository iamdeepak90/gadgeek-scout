import base64
import datetime as _dt
import functools
import hmac
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse

import requests
import feedparser

from config import SETTINGS_DB_PATH

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
# SQLite settings store
# -------------------------

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rss_feeds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 1,
  category_hint TEXT,
  title_key TEXT,
  description_key TEXT,
  content_key TEXT,
  category_key TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_routes (
  stage TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  temperature REAL,
  max_tokens INTEGER,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rss_enabled ON rss_feeds(enabled);
"""

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
    "image_model": "black-forest-labs/FLUX.1-schnell",
    "image_response_format": "url",  # url or base64
}

DEFAULT_MODEL_ROUTES = {
    "generation": {"provider": "together", "model": "deepseek-ai/DeepSeek-V3.1", "temperature": 0.6, "max_tokens": 2200},
    "humanize":   {"provider": "together", "model": "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo", "temperature": 0.7, "max_tokens": 2200},
    "seo":        {"provider": "together", "model": "meta-llama/Llama-3.2-3B-Instruct-Turbo", "temperature": 0.4, "max_tokens": 900},
}

def _now_iso() -> str:
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def _db_path() -> str:
    # Ensure parent dir exists
    p = os.path.join(os.getcwd(), SETTINGS_DB_PATH)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p

def init_db() -> None:
    path = _db_path()
    con = sqlite3.connect(path)
    try:
        con.executescript(DB_SCHEMA)
        # defaults settings
        cur = con.cursor()
        for k, v in DEFAULTS_SETTINGS.items():
            cur.execute("INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES (?, ?, ?)", (k, v, _now_iso()))
        # defaults model routes
        for stage, cfg in DEFAULT_MODEL_ROUTES.items():
            cur.execute(
                "INSERT OR IGNORE INTO model_routes(stage, provider, model, temperature, max_tokens, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (stage, cfg["provider"], cfg["model"], cfg.get("temperature"), cfg.get("max_tokens"), _now_iso()),
            )
        con.commit()
    finally:
        con.close()

def _with_con():
    return sqlite3.connect(_db_path())

def get_setting(key: str, default: Optional[str] = None) -> str:
    con = _with_con()
    try:
        cur = con.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
        if row:
            return row[0]
        return default if default is not None else ""
    finally:
        con.close()

def set_setting(key: str, value: str) -> None:
    con = _with_con()
    try:
        con.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, _now_iso()),
        )
        con.commit()
    finally:
        con.close()

def list_settings() -> Dict[str, str]:
    con = _with_con()
    try:
        rows = con.execute("SELECT key, value FROM settings").fetchall()
        return {k: v for k, v in rows}
    finally:
        con.close()

def list_feeds() -> List[Dict[str, Any]]:
    con = _with_con()
    try:
        rows = con.execute(
            "SELECT id, url, enabled, category_hint, title_key, description_key, content_key, category_key, created_at, updated_at "
            "FROM rss_feeds ORDER BY id DESC"
        ).fetchall()
        keys = ["id","url","enabled","category_hint","title_key","description_key","content_key","category_key","created_at","updated_at"]
        out = []
        for r in rows:
            d = dict(zip(keys, r))
            d["enabled"] = bool(d["enabled"])
            out.append(d)
        return out
    finally:
        con.close()

def upsert_feed(feed: Dict[str, Any]) -> int:
    # feed must contain url
    con = _with_con()
    try:
        now = _now_iso()
        cur = con.cursor()
        cur.execute("SELECT id FROM rss_feeds WHERE url = ?", (feed["url"],))
        row = cur.fetchone()
        if row:
            feed_id = row[0]
            cur.execute(
                "UPDATE rss_feeds SET enabled=?, category_hint=?, title_key=?, description_key=?, content_key=?, category_key=?, updated_at=? WHERE id=?",
                (
                    1 if feed.get("enabled", True) else 0,
                    feed.get("category_hint"),
                    feed.get("title_key"),
                    feed.get("description_key"),
                    feed.get("content_key"),
                    feed.get("category_key"),
                    now,
                    feed_id,
                ),
            )
        else:
            cur.execute(
                "INSERT INTO rss_feeds(url, enabled, category_hint, title_key, description_key, content_key, category_key, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    feed["url"],
                    1 if feed.get("enabled", True) else 0,
                    feed.get("category_hint"),
                    feed.get("title_key"),
                    feed.get("description_key"),
                    feed.get("content_key"),
                    feed.get("category_key"),
                    now,
                    now,
                ),
            )
            feed_id = cur.lastrowid
        con.commit()
        return int(feed_id)
    finally:
        con.close()

def delete_feed(feed_id: int) -> None:
    con = _with_con()
    try:
        con.execute("DELETE FROM rss_feeds WHERE id = ?", (feed_id,))
        con.commit()
    finally:
        con.close()

def get_model_routes() -> Dict[str, Dict[str, Any]]:
    con = _with_con()
    try:
        rows = con.execute("SELECT stage, provider, model, temperature, max_tokens FROM model_routes").fetchall()
        out = {}
        for stage, provider, model, temp, max_tokens in rows:
            out[stage] = {"provider": provider, "model": model, "temperature": temp, "max_tokens": max_tokens}
        return out
    finally:
        con.close()

def set_model_route(stage: str, provider: str, model: str, temperature: Optional[float], max_tokens: Optional[int]) -> None:
    con = _with_con()
    try:
        con.execute(
            "INSERT INTO model_routes(stage, provider, model, temperature, max_tokens, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(stage) DO UPDATE SET provider=excluded.provider, model=excluded.model, temperature=excluded.temperature, max_tokens=excluded.max_tokens, updated_at=excluded.updated_at",
            (stage, provider, model, temperature, max_tokens, _now_iso()),
        )
        con.commit()
    finally:
        con.close()

# -------------------------
# HTTP helpers
# -------------------------

class TransientHTTPError(RuntimeError):
    pass

def http_timeout() -> int:
    try:
        return int(float(get_setting("http_timeout", "30")))
    except Exception:
        return 30

def default_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    h = {
        "User-Agent": get_setting("user_agent", DEFAULTS_SETTINGS["user_agent"]),
        "Accept": "application/json, text/plain, */*",
    }
    if extra:
        h.update(extra)
    return h

def request_with_retry(method: str, url: str, headers: Optional[Dict[str, str]] = None, json_body: Any = None, data: Any = None, params: Dict[str, Any] = None, timeout: Optional[int] = None, max_attempts: int = 4) -> requests.Response:
    timeout = timeout or http_timeout()
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.request(
                method=method,
                url=url,
                headers=headers or default_headers(),
                json=json_body,
                data=data,
                params=params,
                timeout=timeout,
            )
            if resp.status_code in (429, 500, 502, 503, 504):
                raise TransientHTTPError(f"HTTP {resp.status_code} for {url}")
            return resp
        except (requests.RequestException, TransientHTTPError) as e:
            last_exc = e
            sleep_s = min(2 ** attempt, 12) + (0.1 * attempt)
            LOG.warning("HTTP retry %s/%s for %s due to %s; sleeping %.1fs", attempt, max_attempts, url, e, sleep_s)
            time.sleep(sleep_s)
    raise RuntimeError(f"HTTP request failed after retries: {url} ({last_exc})")

# -------------------------
# Basic Auth for /settings
# -------------------------

BASIC_AUTH_USER = "settings@gadgeek.in"
BASIC_AUTH_PASS = "HelloGG@$44"

def _basic_auth_ok(auth_header: str) -> bool:
    if not auth_header or not auth_header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(auth_header.split(" ", 1)[1]).decode("utf-8")
        user, pw = raw.split(":", 1)
        return user == BASIC_AUTH_USER and pw == BASIC_AUTH_PASS
    except Exception:
        return False

def require_basic_auth(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        from flask import request, Response
        if _basic_auth_ok(request.headers.get("Authorization", "")):
            return fn(*args, **kwargs)
        return Response("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm="settings"'})
    return wrapper

# -------------------------
# Slack
# -------------------------

def slack_signing_secret() -> str:
    return get_setting("slack_signing_secret", "")

def slack_bot_token() -> str:
    return get_setting("slack_bot_token", "")

def slack_channel_id() -> str:
    return get_setting("slack_channel_id", "")

def verify_slack_signature(headers: Dict[str, str], body: bytes) -> bool:
    secret = slack_signing_secret()
    if not secret:
        # If not configured yet, allow (but log).
        LOG.warning("Slack signing secret not set; skipping signature verification.")
        return True
    timestamp = headers.get("X-Slack-Request-Timestamp", "")
    sig = headers.get("X-Slack-Signature", "")
    if not timestamp or not sig:
        return False
    try:
        ts = int(timestamp)
        if abs(time.time() - ts) > 60 * 5:
            return False
    except Exception:
        return False
    basestring = b"v0:" + timestamp.encode("utf-8") + b":" + body
    my_sig = "v0=" + hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(my_sig, sig)

def slack_api(method: str, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    token = slack_bot_token()
    if not token:
        raise RuntimeError("Slack bot token not configured in /settings.")
    url = f"https://slack.com/api/{endpoint}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    resp = request_with_retry(method, url, headers=headers, json_body=payload, timeout=20)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack API error: {data}")
    return data

def slack_post_lead(title: str, category_name: str, lead_id: int) -> Tuple[str, str]:
    """Post to default channel. Returns (channel, ts)."""
    channel = slack_channel_id()
    if not channel:
        raise RuntimeError("Slack channel id not configured in /settings.")
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n_{category_name}_"}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Approve ✅"}, "style": "primary", "action_id": "approve", "value": str(lead_id)},
            {"type": "button", "text": {"type": "plain_text", "text": "Urgent ⚡"}, "style": "danger", "action_id": "urgent", "value": str(lead_id)},
            {"type": "button", "text": {"type": "plain_text", "text": "Reject ❌"}, "action_id": "reject", "value": str(lead_id)},
        ]},
    ]
    data = slack_api("POST", "chat.postMessage", {"channel": channel, "text": title, "blocks": blocks})
    return data["channel"], data["ts"]

def slack_update_published(channel: str, ts: str, title: str) -> None:
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n_Published ✅_"}}]
    slack_api("POST", "chat.update", {"channel": channel, "ts": ts, "text": f"{title} (Published)", "blocks": blocks})

def slack_ephemeral(response_url: str, text: str) -> None:
    try:
        request_with_retry("POST", response_url, headers={"Content-Type": "application/json"}, json_body={"response_type": "ephemeral", "text": text}, timeout=10, max_attempts=2)
    except Exception as e:
        LOG.warning("Failed to send ephemeral response to Slack: %s", e)

# -------------------------
# Directus
# -------------------------

def directus_url(path: str) -> str:
    base = get_setting("directus_url", "").rstrip("/")
    if not base:
        raise RuntimeError("Directus URL not configured in /settings.")
    if not path.startswith("/"):
        path = "/" + path
    return base + path

def directus_headers() -> Dict[str, str]:
    token = get_setting("directus_token", "")
    if not token:
        raise RuntimeError("Directus token not configured in /settings.")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def directus_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    resp = request_with_retry("GET", directus_url(path), headers=directus_headers(), params=params, timeout=30)
    return resp.json()

def directus_post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    resp = request_with_retry("POST", directus_url(path), headers=directus_headers(), json_body=body, timeout=45)
    return resp.json()

def directus_patch(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    resp = request_with_retry("PATCH", directus_url(path), headers=directus_headers(), json_body=body, timeout=45)
    return resp.json()

def leads_collection() -> str:
    return get_setting("directus_leads_collection", "news_leads")

def articles_collection() -> str:
    return get_setting("directus_articles_collection", "Articles")

def categories_collection() -> str:
    return get_setting("directus_categories_collection", "categories")

def lead_exists_by_url(source_url: str) -> bool:
    col = leads_collection()
    params = {"filter[source_url][_eq]": source_url, "limit": 1, "fields": "id"}
    data = directus_get(f"/items/{col}", params=params)
    return bool(data.get("data"))

def create_lead(title: str, source_url: str, category_slug: str) -> int:
    col = leads_collection()
    body = {"title": title, "source_url": source_url, "category_slug": category_slug, "status": "pending"}
    data = directus_post(f"/items/{col}", body)
    return int(data["data"]["id"])

def update_lead_status(lead_id: int, status: str) -> None:
    col = leads_collection()
    directus_patch(f"/items/{col}/{lead_id}", {"status": status})

def get_lead(lead_id: int) -> Dict[str, Any]:
    col = leads_collection()
    data = directus_get(f"/items/{col}/{lead_id}")
    return data["data"]

def list_one_approved_lead_newest() -> Optional[Dict[str, Any]]:
    col = leads_collection()
    params = {
        "filter[status][_eq]": "approved",
        "limit": 1,
        "sort": "-id",
    }
    data = directus_get(f"/items/{col}", params=params)
    items = data.get("data") or []
    return items[0] if items else None

def get_categories() -> List[Dict[str, Any]]:
    col = categories_collection()
    params = {"limit": 200, "sort": "priority", "filter[enabled][_neq]": False}
    data = directus_get(f"/items/{col}", params=params)
    items = data.get("data") or []
    # normalize
    out = []
    for c in items:
        out.append({
            "id": c.get("id"),
            "slug": c.get("slug") or c.get("category_slug") or c.get("key"),
            "name": c.get("name") or c.get("title") or (c.get("slug") or ""),
            "priority": int(c.get("priority") or 999),
            "keywords": c.get("keywords") or [],
            "posts_per_scout": int(c.get("posts_per_scout") or c.get("posts_per_category") or 0),
            "enabled": bool(c.get("enabled", True)),
        })
    # keep enabled and with slug
    out = [c for c in out if c["slug"] and c["enabled"] and c["posts_per_scout"] > 0]
    # sort by priority then name
    out.sort(key=lambda x: (x["priority"], x["name"]))
    return out

# -------------------------
# RSS parsing with selectors
# -------------------------

def _get_by_path(obj: Any, path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, (list, tuple)):
            try:
                idx = int(part)
            except ValueError:
                return None
            if idx < 0 or idx >= len(cur):
                return None
            cur = cur[idx]
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            # feedparser objects allow attribute access via dict-like
            try:
                cur = cur.get(part)  # type: ignore
            except Exception:
                cur = getattr(cur, part, None)
    if cur is None:
        return None
    if isinstance(cur, (dict, list)):
        try:
            return json.dumps(cur, ensure_ascii=False)
        except Exception:
            return str(cur)
    return str(cur)

def parse_feed(url: str) -> feedparser.FeedParserDict:
    # feedparser fetches itself; set UA via requests? Not easily. We'll fetch with requests for consistency.
    resp = request_with_retry("GET", url, headers=default_headers(), timeout=http_timeout())
    return feedparser.parse(resp.content)

def extract_entry_fields(entry: Any, feed_cfg: Dict[str, Any]) -> Dict[str, str]:
    # auto extraction
    title = _get_by_path(entry, feed_cfg.get("title_key")) or getattr(entry, "title", None) or entry.get("title", "")
    link = entry.get("link") or getattr(entry, "link", "")
    # description / summary
    desc = _get_by_path(entry, feed_cfg.get("description_key")) or entry.get("summary") or entry.get("description") or ""
    # content
    content = _get_by_path(entry, feed_cfg.get("content_key"))
    if not content:
        if entry.get("content") and isinstance(entry["content"], list):
            content = entry["content"][0].get("value") or entry["content"][0].get("content")
    if not content:
        content = ""
    # category
    cat = _get_by_path(entry, feed_cfg.get("category_key"))
    if not cat:
        # try tags
        tags = entry.get("tags")
        if tags and isinstance(tags, list):
            cat = tags[0].get("term") or tags[0].get("label")
    return {"title": str(title).strip(), "link": str(link).strip(), "description": str(desc).strip(), "content": str(content).strip(), "category": (cat or "").strip()}

def domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""

# -------------------------
# Tavily
# -------------------------

def tavily_search(query: str, max_results: int = 6) -> Dict[str, Any]:
    key = get_setting("tavily_api_key", "")
    if not key:
        raise RuntimeError("Tavily API key not configured in /settings.")
    url = "https://api.tavily.com/search"
    payload = {
        "api_key": key,
        "query": query,
        "search_depth": "basic",  # keep costs predictable
        "max_results": max_results,
        "include_answer": False,
        "include_raw_content": False,
    }
    resp = request_with_retry("POST", url, headers={"Content-Type": "application/json"}, json_body=payload, timeout=30)
    return resp.json()

def tavily_extract(urls: List[str]) -> Dict[str, Any]:
    key = get_setting("tavily_api_key", "")
    if not key:
        raise RuntimeError("Tavily API key not configured in /settings.")
    url = "https://api.tavily.com/extract"
    payload = {
        "api_key": key,
        "urls": urls,
        "extract_depth": "basic",
        "include_images": True,
    }
    resp = request_with_retry("POST", url, headers={"Content-Type": "application/json"}, json_body=payload, timeout=60)
    return resp.json()

def build_research_pack(title: str) -> Dict[str, Any]:
    s = tavily_search(title, max_results=6)
    results = s.get("results") or []
    urls = []
    for r in results:
        u = r.get("url")
        if u and u not in urls:
            urls.append(u)
        if len(urls) >= 5:
            break
    e = tavily_extract(urls) if urls else {"results": []}
    return {"query": title, "search": s, "extract": e, "urls": urls}

def pick_extracted_image(pack: Dict[str, Any]) -> Optional[Dict[str, str]]:
    # Tavily extract returns results with possibly "images"
    extract = pack.get("extract") or {}
    results = extract.get("results") or []
    for r in results:
        imgs = r.get("images") or []
        if imgs and isinstance(imgs, list):
            img = imgs[0]
            img_url = img.get("url") if isinstance(img, dict) else None
            if img_url:
                src_url = r.get("url") or ""
                dom = domain_of(src_url)
                caption = img.get("caption") if isinstance(img, dict) else ""
                credit = f"{dom}" if dom else "source"
                return {"url": img_url, "credit": credit, "caption": caption or ""}
    return None

# -------------------------
# LLM Providers: Together / OpenRouter
# -------------------------

TOGETHER_CHAT_URL = "https://api.together.xyz/v1/chat/completions"
TOGETHER_IMAGES_URL = "https://api.together.xyz/v1/images/generations"

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

def _chat(provider: str, model: str, messages: List[Dict[str, str]], temperature: Optional[float], max_tokens: Optional[int]) -> str:
    provider = (provider or "").lower().strip()
    if provider == "together":
        key = get_setting("together_api_key", "")
        if not key:
            raise RuntimeError("Together API key not configured in /settings.")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        payload: Dict[str, Any] = {"model": model, "messages": messages}
        if temperature is not None:
            payload["temperature"] = float(temperature)
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
        resp = request_with_retry("POST", TOGETHER_CHAT_URL, headers=headers, json_body=payload, timeout=90, max_attempts=4)
        data = resp.json()
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    elif provider == "openrouter":
        key = get_setting("openrouter_api_key", "")
        if not key:
            raise RuntimeError("OpenRouter API key not configured in /settings.")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": messages}
        if temperature is not None:
            payload["temperature"] = float(temperature)
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
        # Optional headers for OpenRouter ranking (safe defaults)
        # Not adding extra headers that may break if absent.
        resp = request_with_retry("POST", OPENROUTER_CHAT_URL, headers=headers, json_body=payload, timeout=90, max_attempts=4)
        data = resp.json()
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    else:
        raise RuntimeError(f"Unknown provider: {provider}")

def get_route(stage: str) -> Dict[str, Any]:
    routes = get_model_routes()
    if stage not in routes:
        # fallback
        return {"provider": "together", "model": "deepseek-ai/DeepSeek-V3.1", "temperature": 0.6, "max_tokens": 1800}
    return routes[stage]

def chat_stage(stage: str, messages: List[Dict[str, str]]) -> str:
    route = get_route(stage)
    return _chat(route["provider"], route["model"], messages, route.get("temperature"), route.get("max_tokens"))

# -------------------------
# Image generation (Together)
# -------------------------

def generate_image_together(prompt: str, width: int = 1024, height: int = 1024) -> Optional[Dict[str, str]]:
    key = get_setting("together_api_key", "")
    if not key:
        return None
    model = get_setting("image_model", DEFAULTS_SETTINGS["image_model"])
    response_format = get_setting("image_response_format", "url")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "prompt": prompt,
        "width": width,
        "height": height,
        "steps": 4,  # flux schnell
        "n": 1,
        "response_format": response_format,
        "output_format": "jpeg",
    }
    resp = request_with_retry("POST", TOGETHER_IMAGES_URL, headers=headers, json_body=payload, timeout=120, max_attempts=3)
    data = resp.json()
    item = (data.get("data") or [{}])[0]
    if response_format == "url" and item.get("url"):
        return {"url": item["url"], "credit": "AI-generated (Together)", "caption": "AI-generated illustration"}
    if response_format == "base64" and item.get("b64_json"):
        # fallback: data URI
        return {"url": f"data:image/jpeg;base64,{item['b64_json']}", "credit": "AI-generated (Together)", "caption": "AI-generated illustration"}
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
    # replace smart quotes, remove trailing commas
    s = s.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    return s

def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    # Try direct parse
    t = text.strip()
    t = _sanitize_json(t)
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    # brace-balance extraction
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
    # Keep inputs short: include extracted content summaries, not raw full pages if enormous.
    extract_results = (pack.get("extract") or {}).get("results") or []
    snippets = []
    for r in extract_results[:5]:
        url = r.get("url") or ""
        content = (r.get("content") or r.get("raw_content") or "").strip()
        # limit length per source
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
    # For news, avoid pretending it's a real event photo.
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

    # Humanize (always)
    human_html = chat_stage("humanize", humanize_prompt(draft_html))
    if not human_html.strip():
        human_html = draft_html

    # SEO JSON
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
        gen_img = generate_image_together(build_image_prompt(title))
        img = gen_img

    featured_image = img["url"] if img else ""
    caption = (img.get("caption") or "Tech illustration") if img else ""
    credit = img.get("credit") or "" if img else ""
    featured_image_credit = f"{caption} | Credit: {credit}".strip(" |")

    slug = slugify(title)
    published_at = _now_iso()

    return {
        "title": title,
        "slug": f"{slug}-{hashlib.md5(title.encode('utf-8')).hexdigest()[:6]}",  # collision-safe without reads
        "status": "published",
        "category_slug": category_name,  # will be overwritten by caller with slug; keep name for safety
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
    # category stored as slug string
    payload = dict(article)
    payload["category_slug"] = category_slug
    # Remove empty fields (Directus validations vary; keep required ones)
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

