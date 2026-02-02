"""
bot_server.py — Slack interactive approval server.

Endpoints:
- POST /slack/actions  (Slack interactive components)
- GET  /health

This server updates Directus lead statuses based on Slack button clicks:
- approve_lead → queued
- reject_lead → rejected
- publish_now → queued + triggers immediate publish in background

Run:
  gunicorn -w 2 -b 0.0.0.0:8000 bot_server:app
"""

from __future__ import annotations

import json
import threading
import traceback
from flask import Flask, request, jsonify
from slack_sdk.signature import SignatureVerifier

import config
from common import log, directus_update_lead, slack_update_message
import publisher  # imports functions only; safe

app = Flask(__name__)
verifier = SignatureVerifier(config.SLACK_SIGNING_SECRET)


def _ok():
    return jsonify({"ok": True})


@app.get("/health")
def health():
    return _ok()


@app.post("/slack/interactions")
def slack_actions():
    # Verify Slack signature
    if config.SLACK_SIGNING_SECRET and not config.SLACK_SIGNING_SECRET.startswith("YOUR_"):
        if not verifier.is_valid_request(request.get_data(), request.headers):
            return jsonify({"error": "invalid signature"}), 403

    payload = request.form.get("payload")
    if not payload:
        return jsonify({"error": "missing payload"}), 400

    try:
        data = json.loads(payload)
        actions = data.get("actions") or []
        if not actions:
            return _ok()

        action = actions[0]
        action_id = action.get("action_id")
        lead_id = action.get("value")

        channel = (data.get("channel") or {}).get("id") or ""
        message = data.get("message") or {}
        ts = message.get("ts") or ""

        user = (data.get("user") or {}).get("username") or (data.get("user") or {}).get("name") or "unknown"

        if action_id == "approve_lead":
            directus_update_lead(lead_id, {
                config.LEAD_F_STATUS: config.LEAD_STATUS_QUEUED,
                config.LEAD_F_APPROVED_AT: None,
                config.LEAD_F_APPROVED_BY: user
            })
            slack_update_message(channel, ts, f"✅ Approved by *{user}* — queued for publishing.")
            return _ok()

        if action_id == "reject_lead":
            directus_update_lead(lead_id, {
                config.LEAD_F_STATUS: config.LEAD_STATUS_REJECTED,
                config.LEAD_F_APPROVED_BY: user
            })
            slack_update_message(channel, ts, f"❌ Rejected by *{user}*.")
            return _ok()

        if action_id == "publish_now":
            # Queue + trigger
            directus_update_lead(lead_id, {
                config.LEAD_F_STATUS: config.LEAD_STATUS_QUEUED,
                config.LEAD_F_APPROVED_BY: user,
                config.LEAD_F_PRIORITY: 0
            })
            slack_update_message(channel, ts, f"🚀 Publish requested by *{user}* — processing now.")
            th = threading.Thread(target=_publish_thread, args=(lead_id, channel, ts), daemon=True)
            th.start()
            return _ok()

        return _ok()

    except Exception as e:
        log.error(f"Slack action error: {e}\n{traceback.format_exc()}")
        return jsonify({"error": "server error"}), 500


def _publish_thread(lead_id: str, channel: str, ts: str):
    try:
        res = publisher.process_lead_id(lead_id)
        if res and res.get("article_id"):
            slack_update_message(channel, ts, f"✅ Published successfully — Article ID: `{res['article_id']}`")
        else:
            slack_update_message(channel, ts, "⚠️ Processing finished, but no article_id returned. Check logs.")
    except Exception as e:
        log.error(f"Publish thread failed: {e}\n{traceback.format_exc()}")
        slack_update_message(channel, ts, f"❌ Publish failed: `{e}`")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
