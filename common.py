"""
common.py — All shared/common functions & helpers live here.

This module intentionally centralizes:
- logging
- HTTP + retry
- URL normalization + hashing
- RSS parsing helpers
- Directus REST helpers
- Slack message helpers
- SQLite state store (dedupe + caches)
- Search helpers (LangSearch, Brave, NewsData)
- Text extraction
- Image selection (Unsplash/Pexels/Pixabay/Wikimedia/Openverse)

Per project requirement:
- credentials are in config.py
- common/shared code is here
"""

from __future__ import annotations

import json
import os
import re
import time
import math
import hashlib
import logging
import sqlite3
import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from cachetools import TTLCache

import feedparser

try:
    import trafilatura
except Exception:
    trafilatura = None

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

import config


# ---------------------------
# Logging
# ---------------------------

def setup_logger(name: str = "technews", level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    # Optional file log
    os.makedirs(config.DATA_DIR, exist_ok=True)
    fh = logging.FileHandler(os.path.join(config.DATA_DIR, "app.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


log = setup_logger()


# ---------------------------
# State Store (SQLite)
# ---------------------------

class StateStore:
    """
    Small SQLite-based store for:
      - fingerprint dedupe (seen items, published, rejected)
      - HTML/text cache for URLs
      - publish counters and last publish time
    """

    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self._init()

    def _conn(self):
        return sqlite3.connect(self.db_path, timeout=30)

    def _init(self):
        with self._conn() as con:
            cur = con.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS fingerprints(
                    fp TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS url_cache(
                    url TEXT PRIMARY KEY,
                    fetched_at TEXT NOT NULL,
                    text TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS kv(
                    k TEXT PRIMARY KEY,
                    v TEXT NOT NULL
                )
            """)
            con.commit()

    def has_fp(self, fp: str) -> bool:
        with self._conn() as con:
            cur = con.cursor()
            cur.execute("SELECT 1 FROM fingerprints WHERE fp=? LIMIT 1", (fp,))
            return cur.fetchone() is not None

    def put_fp(self, fp: str, kind: str):
        with self._conn() as con:
            cur = con.cursor()
            cur.execute(
                "INSERT OR REPLACE INTO fingerprints(fp, kind, created_at) VALUES (?,?,?)",
                (fp, kind, utcnow_iso()),
            )
            con.commit()

    def get_cached_text(self, url: str, max_age_hours: int = 72) -> Optional[str]:
        with self._conn() as con:
            cur = con.cursor()
            cur.execute("SELECT fetched_at, text FROM url_cache WHERE url=? LIMIT 1", (url,))
            row = cur.fetchone()
            if not row:
                return None
            fetched_at, text = row
            try:
                t = dt.datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
            except Exception:
                return None
            age = (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 3600.0
            if age > max_age_hours:
                return None
            return text

    def set_cached_text(self, url: str, text: str):
        with self._conn() as con:
            cur = con.cursor()
            cur.execute(
                "INSERT OR REPLACE INTO url_cache(url, fetched_at, text) VALUES (?,?,?)",
                (url, utcnow_iso(), text),
            )
            con.commit()

    def kv_get(self, k: str) -> Optional[str]:
        with self._conn() as con:
            cur = con.cursor()
            cur.execute("SELECT v FROM kv WHERE k=? LIMIT 1", (k,))
            row = cur.fetchone()
            return row[0] if row else None

    def kv_set(self, k: str, v: str):
        with self._conn() as con:
            cur = con.cursor()
            cur.execute("INSERT OR REPLACE INTO kv(k, v) VALUES (?,?)", (k, v))
            con.commit()


state = StateStore(config.STATE_DB_PATH)


# ---------------------------
# Time / formatting
# ---------------------------

def utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def parse_time_hhmm(s: str) -> Tuple[int, int]:
    m = re.match(r"^(\d{1,2}):(\d{2})$", s.strip())
    if not m:
        raise ValueError(f"Invalid time string: {s}")
    return int(m.group(1)), int(m.group(2))

def in_publish_window(now_local: dt.datetime, start_hhmm: str, end_hhmm: str) -> bool:
    sh, sm = parse_time_hhmm(start_hhmm)
    eh, em = parse_time_hhmm(end_hhmm)
    start = now_local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now_local.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= now_local <= end

def word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text or ""))


# ---------------------------
# URL normalization / hashing
# ---------------------------

_TRACKING_KEYS = {
    "utm_source","utm_medium","utm_campaign","utm_term","utm_content",
    "gclid","fbclid","mc_cid","mc_eid","ref","source"
}

def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    url = url.strip()
    try:
        p = urlparse(url)
    except Exception:
        return url
    # strip fragments
    p = p._replace(fragment="")
    # normalize netloc to lowercase
    netloc = p.netloc.lower()
    # remove default ports
    netloc = re.sub(r":(80|443)$", "", netloc)
    # strip tracking query params
    q = []
    for k, v in parse_qsl(p.query, keep_blank_values=True):
        if k.lower() in _TRACKING_KEYS:
            continue
        q.append((k, v))
    query = urlencode(q, doseq=True)
    # normalize path (remove trailing slash except root)
    path = p.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunparse((p.scheme or "https", netloc, path, p.params, query, ""))

def normalize_title(title: str) -> str:
    t = (title or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t

def sha256_hex(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

def compute_fingerprint(title: str, url: str) -> str:
    return sha256_hex(f"{normalize_title(title)}|{canonicalize_url(url)}")

def slugify(text: str, max_len: int = 80) -> str:
    s = (text or "").lower().strip()
    s = re.sub(r"[’']", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or sha256_hex(text)[:12]

def domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


# ---------------------------
# HTTP with retry
# ---------------------------

_session = requests.Session()
_session.headers.update({"User-Agent": config.USER_AGENT})

class TransientHTTPError(Exception):
    pass

def _is_retryable_status(code: int) -> bool:
    return code in {408, 429, 500, 502, 503, 504}

@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=12),
    retry=retry_if_exception_type((requests.RequestException, TransientHTTPError)),
)
def http_request(method: str, url: str, *,
                 headers: Optional[Dict[str,str]] = None,
                 params: Optional[Dict[str,Any]] = None,
                 json_body: Optional[Dict[str,Any]] = None,
                 data: Any = None,
                 timeout: int = config.HTTP_TIMEOUT) -> requests.Response:
    h = dict(_session.headers)
    if headers:
        h.update(headers)
    resp = _session.request(method=method, url=url, headers=h, params=params, json=json_body, data=data, timeout=timeout)
    if _is_retryable_status(resp.status_code):
        raise TransientHTTPError(f"HTTP {resp.status_code} for {url}")
    return resp


# ---------------------------
# RSS ingestion
# ---------------------------

def fetch_rss_entries(feed_url: str, limit: int = 50) -> List[Dict[str,Any]]:
    parsed = feedparser.parse(feed_url)
    out: List[Dict[str,Any]] = []
    for e in parsed.entries[:limit]:
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        summary = (e.get("summary") or e.get("description") or "").strip()
        # strip HTML from summary
        if summary and ("<" in summary and ">" in summary):
            summary = BeautifulSoup(summary, "html.parser").get_text(" ", strip=True)
        published = e.get("published") or e.get("updated") or ""
        out.append({
            "title": title,
            "url": link,
            "snippet": summary[:400],
            "published_raw": published,
            "source_feed": feed_url
        })
    return out


# ---------------------------
# Category classification
# ---------------------------

def selected_categories() -> Dict[str,Dict[str,Any]]:
    cats = config.FEATURED_CATEGORIES.copy()
    if not config.INCLUDE_TIER3:
        cats = {k:v for k,v in cats.items() if v.get("priority", 9) <= 2}
    if config.SELECTED_CATEGORY_SLUGS:
        keep = set(config.SELECTED_CATEGORY_SLUGS)
        cats = {k:v for k,v in cats.items() if k in keep}
    return cats

def score_category(text: str, category_keywords: List[str]) -> Tuple[int,List[str]]:
    t = " " + (text or "").lower() + " "
    score = 0
    hits: List[str] = []
    for kw in category_keywords:
        kw_norm = kw.lower()
        if kw_norm.strip() == "":
            continue
        if kw_norm in t:
            # longer keywords get slightly higher weight
            w = 2 if len(kw_norm) >= 10 else 1
            score += w
            if len(hits) < 12:
                hits.append(kw_norm.strip())
    return score, hits

def classify_item(title: str, snippet: str) -> Tuple[Optional[str], int, List[str]]:
    cats = selected_categories()
    base_text = f"{title} {snippet}".strip()
    best_slug = None
    best_score = 0
    best_hits: List[str] = []
    for slug, meta in cats.items():
        s, hits = score_category(base_text, meta.get("keywords", []))
        # priority boost
        pri = int(meta.get("priority", 3))
        if pri == 1:
            s = int(s * 1.25)
        elif pri == 2:
            s = int(s * 1.10)
        if s > best_score:
            best_slug, best_score, best_hits = slug, s, hits
    # threshold: require at least a small match
    if best_score < 2:
        return None, best_score, best_hits
    return best_slug, best_score, best_hits


# ---------------------------
# Directus API
# ---------------------------

def directus_headers() -> Dict[str,str]:
    return {"Authorization": f"Bearer {config.DIRECTUS_TOKEN}", "Content-Type": "application/json"}

def directus_url(path: str) -> str:
    base = config.DIRECTUS_URL.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path

def directus_get(path: str, params: Optional[Dict[str,Any]]=None) -> Dict[str,Any]:
    resp = http_request("GET", directus_url(path), headers=directus_headers(), params=params)
    if resp.status_code >= 300:
        raise RuntimeError(f"Directus GET failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()

def directus_post(path: str, body: Dict[str,Any]) -> Dict[str,Any]:
    resp = http_request("POST", directus_url(path), headers=directus_headers(), json_body=body)
    if resp.status_code >= 300:
        raise RuntimeError(f"Directus POST failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()

def directus_patch(path: str, body: Dict[str,Any]) -> Dict[str,Any]:
    resp = http_request("PATCH", directus_url(path), headers=directus_headers(), json_body=body)
    if resp.status_code >= 300:
        raise RuntimeError(f"Directus PATCH failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()

def _directus_filter_params(filters: Dict[str,Any]) -> Dict[str,Any]:
    """
    Converts a nested filter dict into Directus query params.

    Example:
      {"status":{"_eq":"queued"}, "id":{"_eq":123}}
    -> {"filter[status][_eq]":"queued", "filter[id][_eq]":123}
    """
    out: Dict[str,Any] = {}

    def rec(prefix: str, obj: Any):
        if isinstance(obj, dict):
            for k, v in obj.items():
                rec(f"{prefix}[{k}]", v)
        else:
            out[prefix] = obj

    for field, expr in (filters or {}).items():
        rec(f"filter[{field}]", expr)
    return out

def directus_find_items(collection: str, filters: Dict[str,Any], fields: Optional[List[str]]=None, limit: int=5, sort: Optional[str]=None) -> List[Dict[str,Any]]:
    params: Dict[str,Any] = {"limit": limit}
    if fields:
        params["fields"] = ",".join(fields)
    if sort:
        params["sort"] = sort
    params.update(_directus_filter_params(filters))
    data = directus_get(f"/items/{collection}", params=params)
    return data.get("data", []) or []

def directus_item_exists_by_filters(collection: str, filters: Dict[str,Any]) -> bool:
    items = directus_find_items(collection, filters, fields=["id"], limit=1)
    return len(items) > 0

def directus_create_lead(lead: Dict[str,Any]) -> Dict[str,Any]:
    return directus_post(f"/items/{config.LEADS_COLLECTION}", lead).get("data")

def directus_update_lead(lead_id: Any, patch: Dict[str,Any]) -> Dict[str,Any]:
    return directus_patch(f"/items/{config.LEADS_COLLECTION}/{lead_id}", patch).get("data")

def directus_create_article(article: Dict[str,Any]) -> Dict[str,Any]:
    return directus_post(f"/items/{config.ARTICLES_COLLECTION}", article).get("data")


# ---------------------------
# Slack helpers
# ---------------------------

_slack_client: Optional[WebClient] = None

def slack_client() -> Optional[WebClient]:
    global _slack_client
    if not config.SLACK_BOT_TOKEN or config.SLACK_BOT_TOKEN.startswith("xoxb-YOUR"):
        return None
    if _slack_client is None:
        _slack_client = WebClient(token=config.SLACK_BOT_TOKEN)
    return _slack_client

def slack_post_candidate(lead_id: Any, title: str, url: str, category_slug: str, score: int, matched_keywords: List[str]) -> Optional[Tuple[str,str]]:
    """
    Posts an approval card to Slack and returns (channel, ts).
    """
    client = slack_client()
    if not client:
        return None

    cat = config.FEATURED_CATEGORIES.get(category_slug, {}).get("name", category_slug)
    kw_preview = ", ".join(matched_keywords[:8]) if matched_keywords else "—"

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "📰 New Tech Story Candidate", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{escape_slack(title)}*\n<{url}|Open source link>"}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"*Category:* {escape_slack(cat)}  |  *Score:* {score}"},
            {"type": "mrkdwn", "text": f"*Keywords:* {escape_slack(kw_preview)}"}
        ]},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✅ Approve", "emoji": True},
             "style": "primary", "action_id": "approve_lead", "value": str(lead_id)},
            {"type": "button", "text": {"type": "plain_text", "text": "🚀 Publish now", "emoji": True},
             "action_id": "publish_now", "value": str(lead_id)},
            {"type": "button", "text": {"type": "plain_text", "text": "❌ Reject", "emoji": True},
             "style": "danger", "action_id": "reject_lead", "value": str(lead_id)}
        ]}
    ]

    try:
        resp = client.chat_postMessage(channel=config.SLACK_CHANNEL, text=title, blocks=blocks)
        return resp["channel"], resp["ts"]
    except SlackApiError as e:
        log.error(f"Slack post failed: {e}")
        return None

def slack_update_message(channel: str, ts: str, text: str):
    client = slack_client()
    if not client:
        return
    try:
        client.chat_update(channel=channel, ts=ts, text=text, blocks=[{"type":"section","text":{"type":"mrkdwn","text":text}}])
    except SlackApiError as e:
        log.error(f"Slack update failed: {e}")

def escape_slack(s: str) -> str:
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


# ---------------------------
# Search helpers
# ---------------------------

# Cache searches to avoid repeated calls (5 minutes)
_search_cache = TTLCache(maxsize=512, ttl=300)

def langsearch_web_search(query: str, *, count: int = None, freshness: str = None, summary: bool = False) -> List[Dict[str,Any]]:
    if not config.LANGSEARCH_API_KEY or config.LANGSEARCH_API_KEY.startswith("YOUR_"):
        raise RuntimeError("LangSearch API key missing in config.py")
    count = count or config.LANGSEARCH_RESULTS
    freshness = freshness or config.LANGSEARCH_FRESHNESS
    cache_key = f"ls:{query}:{count}:{freshness}:{int(summary)}"
    if cache_key in _search_cache:
        return _search_cache[cache_key]

    url = "https://api.langsearch.com/v1/web-search"
    headers = {"Authorization": f"Bearer {config.LANGSEARCH_API_KEY}", "Content-Type":"application/json"}
    body = {"query": query, "count": int(count), "freshness": freshness, "summary": bool(summary)}
    resp = http_request("POST", url, headers=headers, json_body=body)
    if resp.status_code >= 300:
        raise RuntimeError(f"LangSearch web-search failed: {resp.status_code} {resp.text[:500]}")
    data = resp.json()
    results = data.get("data", {}).get("webPages", {}).get("value") or data.get("webPages", {}).get("value") or []
    norm = []
    for r in results:
        norm.append({
            "title": r.get("name") or r.get("title") or "",
            "url": r.get("url") or "",
            "snippet": r.get("snippet") or "",
            "summary": r.get("summary") or ""
        })
    _search_cache[cache_key] = norm
    return norm

def langsearch_rerank(query: str, docs: List[Dict[str,Any]], top_n: int = 6) -> List[Dict[str,Any]]:
    if not config.LANGSEARCH_API_KEY or config.LANGSEARCH_API_KEY.startswith("YOUR_"):
        raise RuntimeError("LangSearch API key missing in config.py")
    if not docs:
        return []
    url = "https://api.langsearch.com/v1/rerank"
    headers = {"Authorization": f"Bearer {config.LANGSEARCH_API_KEY}", "Content-Type":"application/json"}
    documents = []
    for d in docs[:50]:
        # prefer summary if present else snippet
        txt = (d.get("summary") or d.get("snippet") or d.get("title") or "").strip()
        documents.append(txt)
    body = {"model": "langsearch-reranker-v1", "query": query, "documents": documents, "top_n": int(top_n)}
    resp = http_request("POST", url, headers=headers, json_body=body)
    if resp.status_code >= 300:
        raise RuntimeError(f"LangSearch rerank failed: {resp.status_code} {resp.text[:500]}")
    data = resp.json()
    results = data.get("data") or data.get("results") or []
    # Expected: list of {"index": i, "relevance_score": x}
    idxs = [r.get("index") for r in results if isinstance(r, dict) and r.get("index") is not None]
    picked = []
    for i in idxs:
        if 0 <= i < len(docs):
            picked.append(docs[i])
    return picked or docs[:top_n]

def brave_search(query: str, count: int = None) -> List[Dict[str,Any]]:
    if not config.BRAVE_SEARCH_API_KEY:
        raise RuntimeError("Brave Search API key missing in config.py")
    count = count or config.BRAVE_RESULTS
    url = config.BRAVE_SEARCH_ENDPOINT
    headers = {"Accept": "application/json", "X-Subscription-Token": config.BRAVE_SEARCH_API_KEY}
    params = {"q": query, "count": int(count)}
    resp = http_request("GET", url, headers=headers, params=params)
    if resp.status_code >= 300:
        raise RuntimeError(f"Brave search failed: {resp.status_code} {resp.text[:500]}")
    data = resp.json()
    web = (data.get("web") or {}).get("results") or []
    norm = []
    for r in web:
        norm.append({"title": r.get("title",""), "url": r.get("url",""), "snippet": r.get("description","")})
    return norm

def newsdata_discover(query: str, *, language: str = "en", country: str = "") -> List[Dict[str,Any]]:
    if not config.NEWSDATA_API_KEY or config.NEWSDATA_API_KEY.startswith("YOUR_"):
        raise RuntimeError("NewsData API key missing in config.py")
    params = {"apikey": config.NEWSDATA_API_KEY, "q": query, "language": language}
    if country:
        params["country"] = country
    resp = http_request("GET", config.NEWSDATA_ENDPOINT, params=params)
    if resp.status_code >= 300:
        raise RuntimeError(f"NewsData failed: {resp.status_code} {resp.text[:500]}")
    data = resp.json()
    results = data.get("results") or []
    out = []
    for r in results:
        out.append({
            "title": (r.get("title") or "").strip(),
            "url": (r.get("link") or "").strip(),
            "snippet": (r.get("description") or "").strip(),
            "published_raw": (r.get("pubDate") or "").strip(),
            "source": (r.get("source_id") or "").strip()
        })
    return out


# ---------------------------
# Text extraction
# ---------------------------

def fetch_url_text(url: str) -> str:
    url = canonicalize_url(url)
    cached = state.get_cached_text(url)
    if cached:
        return cached
    resp = http_request("GET", url, timeout=config.HTTP_TIMEOUT)
    html = resp.text or ""
    text = extract_main_text(html, base_url=url)
    if not text:
        text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    text = cleanup_text(text)
    state.set_cached_text(url, text)
    return text

def extract_main_text(html: str, base_url: str = "") -> str:
    if not html:
        return ""
    if trafilatura:
        try:
            downloaded = trafilatura.extract(html, url=base_url, include_comments=False, include_tables=False)
            return downloaded or ""
        except Exception:
            pass
    # fallback: best effort with BS4
    soup = BeautifulSoup(html, "html.parser")
    # remove scripts/styles
    for tag in soup(["script","style","noscript","header","footer","nav","aside"]):
        tag.decompose()
    # heuristic: choose the longest <article> or <main>
    candidates = []
    for sel in ["article","main","section"]:
        for node in soup.select(sel):
            txt = node.get_text(" ", strip=True)
            if len(txt) > 400:
                candidates.append((len(txt), txt))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return soup.get_text(" ", strip=True)

def cleanup_text(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"\s+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t


# ---------------------------
# Source selection helpers
# ---------------------------

def pick_unique_domains(urls: List[str], max_items: int) -> List[str]:
    seen = set()
    out = []
    for u in urls:
        d = domain_of(u)
        if not d or d in seen:
            continue
        seen.add(d)
        out.append(u)
        if len(out) >= max_items:
            break
    return out


# ---------------------------
# Image helpers
# ---------------------------

def pick_image_for_query(query: str) -> Optional[Dict[str,Any]]:
    """
    Tries multiple providers and returns:
      {url, provider, credit, alt}
    """
    q = (query or "").strip()
    if not q:
        return None

    # Try Unsplash
    if config.UNSPLASH_ACCESS_KEY:
        img = unsplash_search(q)
        if img:
            return img

    # Try Pexels
    if config.PEXELS_API_KEY:
        img = pexels_search(q)
        if img:
            return img

    # Try Pixabay
    if config.PIXABAY_API_KEY:
        img = pixabay_search(q)
        if img:
            return img

    # Try Openverse (no key)
    img = openverse_search(q)
    if img:
        return img

    # Try Wikimedia (no key)
    img = wikimedia_search(q)
    if img:
        return img

    return None

def unsplash_search(query: str) -> Optional[Dict[str,Any]]:
    url = "https://api.unsplash.com/search/photos"
    params = {"query": query, "per_page": 1, "orientation": "landscape"}
    headers = {"Authorization": f"Client-ID {config.UNSPLASH_ACCESS_KEY}"}
    resp = http_request("GET", url, headers=headers, params=params)
    data = resp.json()
    results = data.get("results") or []
    if not results:
        return None
    r = results[0]
    img_url = (r.get("urls") or {}).get("regular") or (r.get("urls") or {}).get("full")
    user = (r.get("user") or {}).get("name") or "Unsplash contributor"
    link = (r.get("links") or {}).get("html") or "https://unsplash.com"
    return {"url": img_url, "provider": "unsplash", "credit": f"Photo by {user} (Unsplash) {link}", "alt": ""}

def pexels_search(query: str) -> Optional[Dict[str,Any]]:
    url = "https://api.pexels.com/v1/search"
    headers = {"Authorization": config.PEXELS_API_KEY}
    params = {"query": query, "per_page": 1, "orientation": "landscape"}
    resp = http_request("GET", url, headers=headers, params=params)
    data = resp.json()
    photos = data.get("photos") or []
    if not photos:
        return None
    p = photos[0]
    img_url = (p.get("src") or {}).get("large2x") or (p.get("src") or {}).get("large")
    photographer = p.get("photographer") or "Pexels contributor"
    link = p.get("url") or "https://www.pexels.com"
    return {"url": img_url, "provider": "pexels", "credit": f"Photo by {photographer} (Pexels) {link}", "alt": ""}

def pixabay_search(query: str) -> Optional[Dict[str,Any]]:
    url = "https://pixabay.com/api/"
    params = {"key": config.PIXABAY_API_KEY, "q": query, "image_type":"photo", "per_page": 3, "safesearch":"true"}
    resp = http_request("GET", url, params=params)
    data = resp.json()
    hits = data.get("hits") or []
    if not hits:
        return None
    h = hits[0]
    img_url = h.get("largeImageURL") or h.get("webformatURL")
    user = h.get("user") or "Pixabay contributor"
    page = h.get("pageURL") or "https://pixabay.com"
    return {"url": img_url, "provider": "pixabay", "credit": f"Image by {user} (Pixabay) {page}", "alt": ""}

def openverse_search(query: str) -> Optional[Dict[str,Any]]:
    # Public API
    url = "https://api.openverse.engineering/v1/images/"
    params = {"q": query, "page_size": 1}
    resp = http_request("GET", url, params=params)
    data = resp.json()
    results = data.get("results") or []
    if not results:
        return None
    r = results[0]
    img_url = r.get("url") or r.get("thumbnail") or ""
    creator = r.get("creator") or "Openverse"
    license_ = r.get("license") or ""
    source = r.get("foreign_landing_url") or "https://openverse.org"
    credit = f"{creator} ({license_}) {source}".strip()
    return {"url": img_url, "provider": "openverse", "credit": credit, "alt": ""}

def wikimedia_search(query: str) -> Optional[Dict[str,Any]]:
    # MediaWiki API search → imageinfo
    api = "https://commons.wikimedia.org/w/api.php"
    params = {"action":"query","format":"json","list":"search","srsearch": query, "srlimit": 1, "srnamespace": 6}
    resp = http_request("GET", api, params=params)
    data = resp.json()
    search = (data.get("query") or {}).get("search") or []
    if not search:
        return None
    title = search[0].get("title")  # File:Something.jpg
    params2 = {"action":"query","format":"json","titles": title, "prop":"imageinfo", "iiprop":"url|extmetadata", "iiurlwidth": 1400}
    resp2 = http_request("GET", api, params=params2)
    data2 = resp2.json()
    pages = (data2.get("query") or {}).get("pages") or {}
    page = next(iter(pages.values()), {})
    ii = (page.get("imageinfo") or [{}])[0]
    img_url = ii.get("thumburl") or ii.get("url") or ""
    meta = ii.get("extmetadata") or {}
    artist = (meta.get("Artist") or {}).get("value") or "Wikimedia Commons"
    license_short = (meta.get("LicenseShortName") or {}).get("value") or ""
    credit = f"{strip_html(artist)} ({strip_html(license_short)}) https://commons.wikimedia.org/wiki/{title.replace(' ','_')}"
    return {"url": img_url, "provider": "wikimedia", "credit": credit, "alt": ""}

def strip_html(s: str) -> str:
    if not s:
        return ""
    return BeautifulSoup(s, "html.parser").get_text(" ", strip=True)


# ---------------------------
# Locking (simple file lock)
# ---------------------------

def acquire_lock(lock_path: str, stale_seconds: int = 3600) -> bool:
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    # If stale, remove
    if os.path.exists(lock_path):
        try:
            age = time.time() - os.path.getmtime(lock_path)
            if age > stale_seconds:
                os.remove(lock_path)
        except Exception:
            pass
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, utcnow_iso().encode("utf-8"))
        os.close(fd)
        return True
    except FileExistsError:
        return False

def release_lock(lock_path: str):
    try:
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except Exception:
        pass


# ---------------------------
# Helper: Daily publish counters
# ---------------------------

def daily_key(prefix: str = "published_count") -> str:
    d = dt.datetime.now(dt.timezone.utc).date().isoformat()
    return f"{prefix}:{d}"

def get_published_today() -> int:
    v = state.kv_get(daily_key())
    return int(v) if v and v.isdigit() else 0

def increment_published_today():
    k = daily_key()
    n = get_published_today() + 1
    state.kv_set(k, str(n))

def get_last_publish_ts() -> Optional[dt.datetime]:
    v = state.kv_get("last_publish_ts")
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except Exception:
        return None

def set_last_publish_ts(ts: dt.datetime):
    state.kv_set("last_publish_ts", ts.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z"))
