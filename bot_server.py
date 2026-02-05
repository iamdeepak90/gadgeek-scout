import json
import threading
from typing import Any, Dict

from flask import Flask, request, jsonify, send_from_directory

from common import (
    LOG,
    setup_logging,
    init_db,
    require_basic_auth,
    list_settings,
    set_setting,
    list_feeds,
    upsert_feed,
    delete_feed,
    get_model_routes,
    set_model_route,
    verify_slack_signature,
    update_lead_status,
    slack_ephemeral,
)
import publisher as publisher_mod
from common import get_categories, parse_feed, extract_entry_fields

app = Flask(__name__, static_folder="static", template_folder="templates")

setup_logging()
init_db()


# -------------------------
# Settings UI (Basic Auth)
# -------------------------

@app.route("/settings")
@require_basic_auth
def settings_page():
    return send_from_directory("templates", "settings.html")

@app.route("/static/<path:path>")
@require_basic_auth
def static_files(path):
    return send_from_directory("static", path)

@app.get("/api/state")
@require_basic_auth
def api_state():
    s = list_settings()
    routes = get_model_routes()
    feeds = list_feeds()
    
    # Health check helper
    def is_configured(val): 
        return bool(val and str(val).strip())
    
    health = {
        "directus": is_configured(s.get("directus_url")) and is_configured(s.get("directus_token")),
        "slack": is_configured(s.get("slack_bot_token")) and is_configured(s.get("slack_channel_id")),
        "tavily": is_configured(s.get("tavily_api_key")),
        "together": is_configured(s.get("together_api_key")),
        "openrouter": is_configured(s.get("openrouter_api_key")),
    }
    
    return jsonify({
        "settings": s, 
        "routes": routes, 
        "feeds_count": len(feeds), 
        "health": health
    })

@app.get("/api/settings")
@require_basic_auth
def api_get_settings():
    s = list_settings()
    
    # Secret keys to mask
    secret_keys = {
        "directus_token", "slack_bot_token", "slack_signing_secret",
        "tavily_api_key", "together_api_key", "openrouter_api_key"
    }
    
    # Mask secrets in response
    out = dict(s)
    for key in secret_keys:
        if key in out and out[key]:
            out[key] = ""
    
    # Add presence indicators for secrets
    out["_secrets_present"] = {key: bool(s.get(key) and s.get(key).strip()) for key in secret_keys}
    
    return jsonify(out)

@app.post("/api/settings")
@require_basic_auth
def api_set_settings():
    data = request.get_json(force=True, silent=False) or {}
    
    # Secret keys should not be overwritten with empty strings
    secret_keys = {
        "directus_token", "slack_bot_token", "slack_signing_secret",
        "tavily_api_key", "together_api_key", "openrouter_api_key"
    }
    
    for key, value in data.items():
        if not isinstance(key, str):
            continue
        
        # Skip empty secrets to prevent accidental deletion
        if key in secret_keys and not str(value).strip():
            continue
        
        set_setting(key, str(value) if value is not None else "")
    
    return jsonify({"ok": True})

@app.get("/api/feeds")
@require_basic_auth
def api_list_feeds():
    return jsonify(list_feeds())

@app.post("/api/feeds")
@require_basic_auth
def api_upsert_feed():
    feed = request.get_json(force=True, silent=False) or {}
    if not feed.get("url"):
        return jsonify({"ok": False, "error": "url required"}), 400
    fid = upsert_feed(feed)
    return jsonify({"ok": True, "id": fid})

@app.delete("/api/feeds/<int:feed_id>")
@require_basic_auth
def api_delete_feed(feed_id: int):
    delete_feed(feed_id)
    return jsonify({"ok": True})

@app.post("/api/feeds/test")
@require_basic_auth
def api_test_feed():
    payload = request.get_json(force=True, silent=False) or {}
    url = payload.get("url")
    if not url:
        return jsonify({"ok": False, "error": "url required"}), 400
    selectors = {
        "title_key": payload.get("title_key"),
        "description_key": payload.get("description_key"),
        "content_key": payload.get("content_key"),
        "category_key": payload.get("category_key"),
    }
    try:
        parsed = parse_feed(url)
        samples = []
        for ent in (parsed.entries or [])[:3]:
            f = extract_entry_fields(ent, selectors)
            samples.append({"title": f.get("title"), "link": f.get("link"), "description": (f.get("description") or "")[:180], "category": f.get("category")})
        return jsonify({"ok": True, "samples": samples, "feed_title": getattr(parsed.feed, "title", "") or parsed.feed.get("title", "")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/api/models")
@require_basic_auth
def api_get_models():
    return jsonify(get_model_routes())

@app.post("/api/models")
@require_basic_auth
def api_set_models():
    data = request.get_json(force=True, silent=False) or {}
    
    valid_stages = {"generation", "humanize", "seo", "image"}
    
    for stage, cfg in data.items():
        if stage not in valid_stages:
            continue
        
        provider = cfg.get("provider", "").strip()
        model = cfg.get("model", "").strip()
        
        if not provider or not model:
            continue
        
        # Helper to safely convert to int/float
        def safe_float(val):
            try:
                return float(val) if val not in (None, "") else None
            except (ValueError, TypeError):
                return None
        
        def safe_int(val):
            try:
                return int(val) if val not in (None, "") else None
            except (ValueError, TypeError):
                return None
        
        set_model_route(
            stage, 
            provider, 
            model,
            temperature=safe_float(cfg.get("temperature")),
            max_tokens=safe_int(cfg.get("max_tokens")),
            width=safe_int(cfg.get("width")),
            height=safe_int(cfg.get("height"))
        )
    
    return jsonify({"ok": True})

@app.get("/api/categories")
@require_basic_auth
def api_categories():
    try:
        cats = get_categories()
        return jsonify({"ok": True, "categories": cats})
    except Exception as e:
        LOG.exception("Categories API error")
        return jsonify({"ok": False, "error": str(e), "categories": []}), 500

# -------------------------
# Slack interactions
# -------------------------

def _parse_slack_payload(req) -> Dict[str, Any]:
    ctype = req.headers.get("Content-Type", "")
    if "application/json" in ctype:
        return req.get_json(force=True, silent=True) or {}
    # Slack interactive payload is form-encoded: payload=<json>
    if "application/x-www-form-urlencoded" in ctype or "multipart/form-data" in ctype:
        payload = req.form.get("payload") or ""
        return json.loads(payload) if payload else {}
    return {}

def _async_publish(lead_id: int, slack_ctx: Dict[str, str], response_url: str = ""):
    res = publisher_mod.publish_lead_by_id(lead_id, slack_ctx=slack_ctx)
    if not res.get("ok") and response_url:
        slack_ephemeral(response_url, f"❌ Publish failed, reverted to approved.\nError: {res.get('error')}")

@app.post("/slack/interactions")
def slack_interactions():
    raw = request.get_data() or b""
    if not verify_slack_signature(dict(request.headers), raw):
        return "invalid signature", 401

    payload = _parse_slack_payload(request)
    if not payload:
        return jsonify({"ok": True})

    # Block actions
    if payload.get("type") == "block_actions":
        actions = payload.get("actions") or []
        if not actions:
            return jsonify({"ok": True})
        action = actions[0]
        action_id = action.get("action_id")
        lead_id = int(action.get("value") or 0)
        response_url = payload.get("response_url") or ""

        channel_id = (payload.get("channel") or {}).get("id") or ""
        message_ts = (payload.get("message") or {}).get("ts") or ""
        title = ((payload.get("message") or {}).get("text") or "").strip()

        if lead_id <= 0:
            return jsonify({"ok": True})

        try:
            if action_id == "approve":
                update_lead_status(lead_id, "approved")
                # optional ephemeral ack
                if response_url:
                    slack_ephemeral(response_url, "✅ Approved.")
                return jsonify({"ok": True})

            if action_id == "reject":
                update_lead_status(lead_id, "rejected")
                if response_url:
                    slack_ephemeral(response_url, "❌ Rejected.")
                return jsonify({"ok": True})

            if action_id == "urgent":
                # Mark approved then publish immediately
                update_lead_status(lead_id, "approved")
                slack_ctx = {"channel": channel_id, "ts": message_ts, "title": title}
                t = threading.Thread(target=_async_publish, args=(lead_id, slack_ctx, response_url), daemon=True)
                t.start()
                # no "publishing..." message; only update after published in publisher.py
                return jsonify({"ok": True})

        except Exception as e:
            LOG.exception("Slack action handling error: %s", e)
            if response_url:
                slack_ephemeral(response_url, f"❌ Error: {e}")
            return jsonify({"ok": True})

    return jsonify({"ok": True})

# Backward compatible alias
@app.post("/slack/actions")
def slack_actions_alias():
    return slack_interactions()

if __name__ == "__main__":
    setup_logging()
    init_db()
    app.run(host="0.0.0.0", port=8000)