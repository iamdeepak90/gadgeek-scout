import json, threading
from flask import Flask, request, jsonify, send_from_directory
from common import *
import publisher as pub_mod

app = Flask(__name__, static_folder="static", template_folder="templates")
setup_logging(); init_db()

@app.route("/settings")
@require_basic_auth
def settings_page(): return send_from_directory("templates", "settings.html")

@app.get("/api/state")
@require_basic_auth
def api_state():
    s, r, f = list_settings(), get_model_routes(), list_feeds()
    health = {k: bool(s.get(k) and s.get(k).strip()) for k in ["directus_url", "slack_bot_token", "tavily_api_key", "together_api_key", "openrouter_api_key"]}
    return jsonify({"settings": s, "routes": r, "feeds_count": len(f), "health": health})

@app.get("/api/settings")
@require_basic_auth
def api_get_settings():
    s, secrets = list_settings(), {"directus_token", "slack_bot_token", "slack_signing_secret", "tavily_api_key", "together_api_key", "openrouter_api_key"}
    out = {k: ("" if k in secrets and v else v) for k, v in s.items()}
    out["_secrets_present"] = {k: bool(s.get(k)) for k in secrets}
    return jsonify(out)

@app.post("/api/settings")
@require_basic_auth
def api_set_settings():
    d, secrets = request.get_json() or {}, {"directus_token", "slack_bot_token", "slack_signing_secret", "tavily_api_key", "together_api_key", "openrouter_api_key"}
    for k, v in d.items():
        if k in secrets and not str(v).strip(): continue
        set_setting(k, str(v) if v is not None else "")
    return jsonify({"ok": True})

@app.get("/api/feeds")
@require_basic_auth
def api_list_feeds(): return jsonify(list_feeds())

@app.post("/api/feeds")
@require_basic_auth
def api_upsert_feed():
    d = request.get_json() or {}
    return jsonify({"ok": True, "id": upsert_feed(d)}) if d.get("url") else (jsonify({"error": "url required"}), 400)

@app.delete("/api/feeds/<int:fid>")
@require_basic_auth
def api_delete_feed(fid): delete_feed(fid); return jsonify({"ok": True})

@app.get("/api/categories")
@require_basic_auth
def api_categories(): return jsonify({"ok": True, "categories": get_categories()})

URGENT_QUEUE, _W_STARTED, _W_LOCK = "queue:urgent_publish", False, threading.Lock()

def _urgent_worker():
    r = get_redis_client()
    while True:
        p = r.blpop(URGENT_QUEUE, timeout=2)
        if not p: continue
        d = json.loads(p[1])
        lid, s_ctx, r_url = d["lead_id"], d.get("slack_ctx"), d.get("response_url")
        res = pub_mod.publish_lead_by_id(lid, slack_ctx=s_ctx)
        if r_url: slack_ephemeral(r_url, "✅ Published" if res.get("ok") else f"❌ Error: {res.get('error')}")

@app.post("/slack/interactions")
def slack_interactions():
    raw = request.get_data()
    if not verify_slack_signature(dict(request.headers), raw): return "unauthorized", 401
    p = json.loads(request.form.get("payload", "{}")) if request.form.get("payload") else request.get_json() or {}
    if p.get("type") == "block_actions":
        a = p["actions"][0]; lid, r_url = a["value"], p.get("response_url")
        if a["action_id"] == "approve": update_lead_status(lid, "approved"); slack_ephemeral(r_url, "✅ Approved")
        elif a["action_id"] == "reject": update_lead_status(lid, "rejected"); slack_ephemeral(r_url, "❌ Rejected")
        elif a["action_id"] == "urgent":
            update_lead_status(lid, "approved_high")
            get_redis_client().rpush(URGENT_QUEUE, json.dumps({"lead_id": lid, "slack_ctx": {"channel": p["channel"]["id"], "ts": p["message"]["ts"]}, "response_url": r_url}))
            with _W_LOCK:
                global _W_STARTED
                if not _W_STARTED: threading.Thread(target=_urgent_worker, daemon=True).start(); _W_STARTED = True
            slack_ephemeral(r_url, "🚀 Urgent queued")
    return jsonify({"ok": True})

if __name__ == "__main__": app.run(host="0.0.0.0", port=8000)