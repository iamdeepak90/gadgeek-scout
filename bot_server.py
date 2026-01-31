import os
import json
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# --- CONFIG ---
DIRECTUS_URL = "https://admin.gadgeek.in"
DIRECTUS_TOKEN = "Cmq-X3we8iSjBHbxziDrwas55FP3d6gz"
SLACK_SIGNING_SECRET = "4f28dc0a3781d55f764267910c7bcc77"

@app.route("/slack/interactions", methods=["POST"])
def slack_interactions():
    # Parse the Slack payload
    payload = json.loads(request.form["payload"])
    action = payload["actions"][0]
    value_parts = action["value"].split("_")
    
    decision = value_parts[0]  # approve, urgent, or reject
    lead_id = value_parts[1]
    
    # 1. Update Directus
    status_map = {
        "approve": "approved",
        "urgent": "approved_high",
        "reject": "rejected"
    }
    new_status = status_map.get(decision, "pending")
    
    headers = {"Authorization": f"Bearer {DIRECTUS_TOKEN}"}
    requests.patch(
        f"{DIRECTUS_URL}/items/News_Leads/{lead_id}", 
        json={"status": new_status}, 
        headers=headers
    )

    # 2. Update Slack Message (Remove buttons, show result)
    original_text = payload["message"]["blocks"][0]["text"]["text"]
    
    response_text = ""
    if decision == "approve": response_text = "✅ *Approved*"
    elif decision == "urgent": response_text = "🔥 *Marked Urgent*"
    elif decision == "reject": response_text = "❌ *Rejected*"

    return jsonify({
        "replace_original": "true",
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": original_text}
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": response_text}]
            }
        ]
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)