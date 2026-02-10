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
from llm_manager import chat_stage, generate_image_logic, strip_code_fences

LOG = logging.getLogger("technews")
DEFAULT_CATEGORY_UUID = "3229ec20-3076-4a32-9fa2-88b65dacfedf"
HTTP_TIMEOUT = 60
USER_AGENT = "Mozilla/5.0 (compatible; GadgeekBot/2.0; +[https://bot.gadgeek.in](https://bot.gadgeek.in))"

def setup_logging(level: str = "INFO"):
    logging.basicConfig(level=getattr(logging, level.upper()), format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

_REDIS_CLIENT = None
def _get_redis():
    global _REDIS_CLIENT
    if _REDIS_CLIENT is None:
        _REDIS_CLIENT = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, username=REDIS_USERNAME, password=REDIS_PASSWORD, decode_responses=True)
    return _REDIS_CLIENT

def get_redis_client(): return _get_redis()

DEFAULTS_SETTINGS = {
    "directus_url": "", "directus_token": "", "directus_leads_collection": "news_leads",
    "directus_articles_collection": "Articles", "directus_categories_collection": "categories",
    "slack_bot_token": "", "slack_signing_secret": "", "slack_channel_id": "",
    "tavily_api_key": "", "together_api_key": "", "openrouter_api_key": "",
    "publish_interval_minutes": "20", "scout_interval_minutes": "30", "prefer_extracted_image": "1"
}

DEFAULT_MODEL_ROUTES = {
    "generation": {"provider": "together", "model": "deepseek-ai/DeepSeek-V3", "temperature": 0.6, "max_tokens": 2200},
    "humanize":   {"provider": "together", "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo", "temperature": 0.8, "max_tokens": 2200},
    "seo":        {"provider": "together", "model": "meta-llama/Llama-3.2-3B-Instruct-Turbo", "temperature": 0.4, "max_tokens": 900},
    "image":      {"provider": "together", "model": "black-forest-labs/FLUX.1-schnell", "width": 1024, "height": 768},
}

def init_db():
    r = _get_redis()
    for k, v in DEFAULTS_SETTINGS.items():
        if not r.exists(f"settings:{k}"): r.hset(f"settings:{k}", mapping={"value": v, "updated_at": _dt.datetime.utcnow().isoformat() + "Z"})
    for stage, cfg in DEFAULT_MODEL_ROUTES.items():
        if not r.exists(f"model_routes:{stage}"): r.hset(f"model_routes:{stage}", mapping={**cfg, "updated_at": _dt.datetime.utcnow().isoformat() + "Z"})

def get_setting(key, default=""):
    val = _get_redis().hget(f"settings:{key}", "value")
    return val if val else default

def set_setting(key, value): _get_redis().hset(f"settings:{key}", mapping={"value": value, "updated_at": _dt.datetime.utcnow().isoformat() + "Z"})

def list_settings():
    r, res = _get_redis(), {}
    for rk in r.scan_iter(match="settings:*"): res[rk.replace("settings:", "")] = r.hget(rk, "value")
    return res

def list_feeds():
    r, feeds = _get_redis(), []
    for rk in r.scan_iter(match="feed:*"):
        if rk == "feed:next_id": continue
        data = r.hgetall(rk)
        if data: feeds.append({"id": int(rk.replace("feed:", "")), **data, "enabled": data.get("enabled") == "1"})
    return sorted(feeds, key=lambda x: x["id"], reverse=True)

def upsert_feed(feed):
    r, url = _get_redis(), feed["url"]
    fid = next((int(rk.replace("feed:", "")) for rk in r.scan_iter("feed:*") if rk != "feed:next_id" and r.hget(rk, "url") == url), None)
    if fid is None: fid = r.incr("feed:next_id")
    r.hset(f"feed:{fid}", mapping={**feed, "enabled": "1" if feed.get("enabled", True) else "0", "updated_at": _dt.datetime.utcnow().isoformat() + "Z"})
    return fid

def delete_feed(fid): _get_redis().delete(f"feed:{fid}")

def get_model_routes():
    r, routes = _get_redis(), {}
    for rk in r.scan_iter("model_routes:*"):
        stage, data = rk.replace("model_routes:", ""), r.hgetall(rk)
        if data: routes[stage] = {k: (float(v) if k == "temperature" else int(v) if k in ["max_tokens", "width", "height"] else v) for k, v in data.items()}
    return routes

def set_model_route(stage, provider, model, **kwargs):
    data = {"provider": provider, "model": model, "updated_at": _dt.datetime.utcnow().isoformat() + "Z", **{k: str(v) for k, v in kwargs.items() if v is not None}}
    _get_redis().hset(f"model_routes:{stage}", mapping=data)

@dataclass
class Response:
    status_code: int; text: str; headers: Dict[str, str]
    def json(self): return json.loads(self.text)

def request_with_retry(method, url, headers=None, json_body=None, timeout=HTTP_TIMEOUT, max_attempts=3):
    hdrs = headers or {}; hdrs.setdefault("User-Agent", USER_AGENT)
    for i in range(max_attempts):
        try:
            r = requests.request(method, url, headers=hdrs, json=json_body, timeout=timeout)
            return Response(r.status_code, r.text, dict(r.headers))
        except Exception:
            if i == max_attempts - 1: raise
            time.sleep(min(2**i, 10))

def require_basic_auth(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        from flask import request, Response as FR
        a = request.authorization
        if not a or a.username != "settings@gadgeek.in" or a.password != "HelloGG@$44":
            return FR("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm="Settings"'})
        return f(*args, **kwargs)
    return wrapper

def directus_api(method, path, data=None):
    url = f"{get_setting('directus_url').rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {get_setting('directus_token')}"}
    r = request_with_retry(method, url, headers=headers, json_body=data)
    if r.status_code >= 400: raise RuntimeError(f"Directus {method} {path} failed: {r.status_code}")
    return r.json()

def import_image_to_directus(url, title=""):
    if not url.startswith("http"): return None
    try:
        res = requests.post(f"{get_setting('directus_url').rstrip('/')}/files/import", headers={"Authorization": f"Bearer {get_setting('directus_token')}"}, json={"url": url, "data": {"title": title[:255]}}, timeout=120)
        return str(res.json().get("data", {}).get("id")) if res.status_code < 400 else None
    except Exception: return None

def get_categories():
    data = directus_api("GET", f"/items/{get_setting('directus_categories_collection')}?filter[enabled][_eq]=true&limit=-1")
    return [{"id": c["id"], "slug": c["slug"], "name": c["name"], "keywords": json.loads(c["keywords"]) if isinstance(c.get("keywords"), str) else c.get("keywords", []), "prompt_generation": c.get("prompt_generation") or "", "priority": int(c.get("priority") or 999), "posts_per_scout": int(c.get("posts_per_scout") or 0)} for c in data.get("data", [])]

def lead_exists_by_url(url): return len(directus_api("GET", f"/items/{get_setting('directus_leads_collection')}?filter[source_url][_eq]={url}&limit=1").get("data", [])) > 0

def create_lead(title, source_url, cat_id): return str(directus_api("POST", f"/items/{get_setting('directus_leads_collection')}", {"title": title, "source_url": source_url, "category": cat_id, "status": "pending"}).get("data", {}).get("id"))

def get_lead(lid): return directus_api("GET", f"/items/{get_setting('directus_leads_collection')}/{lid}").get("data", {})

def update_lead_status(lid, status): directus_api("PATCH", f"/items/{get_setting('directus_leads_collection')}/{lid}", {"status": status})

def list_one_approved_lead_newest(status="approved"):
    items = directus_api("GET", f"/items/{get_setting('directus_leads_collection')}?filter[status][_eq]={status}&sort=-date_created&limit=1").get("data", [])
    return items[0] if items else None

def get_category_by_id(cid): return next((c for c in get_categories() if str(c["id"]) == str(cid)), None)

def verify_slack_signature(headers, body):
    secret = get_setting("slack_signing_secret")
    if not secret: return True
    sig, ts = headers.get("X-Slack-Signature", ""), headers.get("X-Slack-Request-Timestamp", "")
    if not sig or not ts: return False
    expected = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:{body.decode()}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)

def slack_post_lead(title, cat_name, lid):
    token, channel = get_setting("slack_bot_token"), get_setting("slack_channel_id")
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n_{cat_name}_"}}, {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "✅ Approve"}, "style": "primary", "action_id": "approve", "value": str(lid)}, {"type": "button", "text": {"type": "plain_text", "text": "🚀 Urgent"}, "style": "danger", "action_id": "urgent", "value": str(lid)}, {"type": "button", "text": {"type": "plain_text", "text": "❌ Reject"}, "action_id": "reject", "value": str(lid)}]}]
    return request_with_retry("POST", "[https://slack.com/api/chat.postMessage](https://slack.com/api/chat.postMessage)", headers={"Authorization": f"Bearer {token}"}, json_body={"channel": channel, "blocks": blocks}).json()

def slack_update_published(ch, ts, title): request_with_retry("POST", "[https://slack.com/api/chat.update](https://slack.com/api/chat.update)", headers={"Authorization": f"Bearer {get_setting('slack_bot_token')}"}, json_body={"channel": ch, "ts": ts, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": f"✅ *Published:* {title}"}}]})

def slack_ephemeral(url, text): request_with_retry("POST", url, json_body={"text": text, "response_type": "ephemeral"})

def parse_feed(url): return feedparser.parse(requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT).content)

def _nested_get(d, key):
    if not key: return None
    for p in key.split("."):
        try: d = d[int(p)] if isinstance(d, list) else d.get(p)
        except: return None
        if d is None: break
    return d

def extract_entry_fields(ent, sel):
    return {
        "title": str(_nested_get(ent, sel.get("title_key") or "title") or getattr(ent, "title", "")),
        "link": str(getattr(ent, "link", "")),
        "description": str(_nested_get(ent, sel.get("description_key") or "summary") or getattr(ent, "summary", "")),
        "content": str(_nested_get(ent, sel.get("content_key")) or (ent.content[0].value if hasattr(ent, "content") else "")),
        "category": str(_nested_get(ent, sel.get("category_key")) or (ent.tags[0].term if hasattr(ent, "tags") else ""))
    }

def keyword_score(text: str, keywords: List[str]) -> int:
    """Robust Regex matching with word boundaries."""
    t = (text or "").lower(); score = 0
    for kw in (k.strip().lower() for k in keywords if k.strip()):
        if re.search(rf'\b{re.escape(kw)}\b', t): score += 1
    return score

def build_research_pack(title):
    key = get_setting("tavily_api_key")
    if not key: return {"extract": {"results": []}}
    try:
        r = request_with_retry("POST", "[https://api.tavily.com/search](https://api.tavily.com/search)", json_body={"api_key": key, "query": title, "search_depth": "advanced", "max_results": 5, "include_raw_content": True, "include_images": True})
        return {"extract": r.json()}
    except: return {"extract": {"results": []}}

def _sources_block_from_pack(pack):
    res = (pack.get("extract") or {}).get("results") or []
    return "\n\n".join([f"SOURCE: {r['url']}\n{r.get('content', r.get('raw_content', ''))[:2000]}" for r in res[:6]]) or "No sources available."

def pick_extracted_image(pack):
    res = (pack.get("extract") or {}).get("results") or []
    for r in res:
        if r.get("image") and r["image"].startswith("http"): return {"url": r["image"], "credit": r.get("url", "Source"), "caption": "Featured image"}
    img = (pack.get("extract") or {}).get("images") or []
    return {"url": img[0], "credit": "Web", "caption": "Featured image"} if img and img[0].startswith("http") else None

def slugify(text):
    text = re.sub(r"[^a-z0-9\s-]", "", text.lower())
    return re.sub(r"\s+", "-", text).strip("-")[:80] or "tech-news"

def extract_json_object(text):
    try: return json.loads(strip_code_fences(text))
    except:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        try: return json.loads(m.group()) if m else None
        except: return None

DEFAULT_GENERATION_TEMPLATE = """You are a professional tech journalist writing for {category}.
Article Title: {title}

Use ONLY these sources:
{sources_block}

Structure:
1. <h3>Article Highlights</h3> (2-3 bullets)
2. One compelling hook sentence (no heading).
3. 5-6 sections using <h2> with 150-250 words each.
4. Use <table> for specs or comparisons.
5. <h3>Sources</h3> (domain names only).

Output ONLY semantic HTML. No markdown."""

def humanize_prompt(html):
    return [{"role": "system", "content": "You are a senior tech editor. Rewrite this HTML to be human-grade. Use contractions, vary sentence length (burstiness), use first-person 'We', and insert professional skepticism. Avoid AI cliches. Return ONLY HTML."}, {"role": "user", "content": f"Humanize this article while preserving facts and HTML:\n\n{html}"}]

def seo_prompt(title, cat, html):
    return [{"role": "system", "content": "Return JSON: {meta_title, meta_description, short_description, tags: [], image_alt}"}, {"role": "user", "content": f"Produce SEO for: {title}\nCategory: {cat}\n\n{html}"}]

def create_article_from_lead(title, cat_name, source_url="", cat_prompt=""):
    pack = build_research_pack(title)
    sources = _sources_block_from_pack(pack)
    rendered = (cat_prompt or DEFAULT_GENERATION_TEMPLATE).format(title=title, category=cat_name, sources_block=sources)
    
    draft = strip_code_fences(chat_stage("generation", [{"role": "system", "content": "Professional tech journalist. ONLY HTML."}, {"role": "user", "content": rendered}]))
    human = strip_code_fences(chat_stage("humanize", humanize_prompt(draft)))
    if len(human.split()) < len(draft.split()) * 0.7: human = draft
    
    seo = extract_json_object(chat_stage("seo", seo_prompt(title, cat_name, human))) or {}
    img = pick_extracted_image(pack) or generate_image_logic(f"High-quality tech hero image: {title}")
    f_img = import_image_to_directus(img["url"], title=title) if img else ""

    return {
        "title": title, "slug": f"{slugify(title)}-{hashlib.md5(title.encode()).hexdigest()[:6]}",
        "status": "published", "content": human, "short_description": seo.get("short_description", title),
        "featured_image": f_img, "featured_image_credit": f"{img.get('caption', 'AI')} | {img.get('credit', 'Source')}" if img else "",
        "featured_image_alt": seo.get("image_alt", title), "meta_title": seo.get("meta_title", title[:60]),
        "meta_description": seo.get("meta_description", ""), "tags": seo.get("tags", []), "published_at": _dt.datetime.utcnow().isoformat() + "Z"
    }

def publish_article_to_directus(art, cid):
    art["category"] = cid
    return directus_api("POST", f"/items/{get_setting('directus_articles_collection')}", art).get("data", {})