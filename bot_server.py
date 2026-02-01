"""
Slack Bot Server - Uses Deep Research for Urgent Posts
"""
import json
import threading
import re
from flask import Flask, request, jsonify
from slack_sdk.signature import SignatureVerifier
from slack_sdk import WebClient

from config import SLACK_SIGNING_SECRET, SLACK_BOT_TOKEN, publish_article_to_directus, update_lead_status
from ai_content import create_complete_article

app = Flask(__name__)
verifier = SignatureVerifier(SLACK_SIGNING_SECRET)
slack_client = WebClient(token=SLACK_BOT_TOKEN)


# ==================== URGENT PROCESSING ====================

def process_urgent_in_background(lead_id, title, category, url, channel_id, thread_ts):
    """Process urgent posts with deep research"""
    try:
        print(f"\n🔥 URGENT: #{lead_id}", flush=True)
        
        # Create article using deep research + OpenAI
        article_data = create_complete_article(
            title=title,
            category=category
        )
        
        if not article_data or not article_data.get('content'):
            raise ValueError("Article generation failed")
        
        # Publish using modular function
        success = publish_article_to_directus(
            title=article_data['title'],
            content=article_data['content'],
            short_description=article_data['short_description'],
            category=category,
            source_url=url,
            featured_image=article_data.get('featured_image'),
            slug=article_data.get('slug')
        )
        
        if success:
            update_lead_status(lead_id, "processed")
            word_count = len(article_data['content'].split())
            message = f"🚀 *PUBLISHED LIVE!*\n\n✅ {article_data['title']}\n📊 {word_count} words\n📁 {category}"
            print(f"   ✅ Published ({word_count} words)", flush=True)
        else:
            message = f"❌ *ERROR:* Publishing failed"
            print(f"   ❌ Publishing failed", flush=True)
        
        slack_client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=message
        )
        
    except Exception as e:
        error_msg = f"❌ *ERROR:* {str(e)[:200]}"
        print(f"   ❌ Error: {e}", flush=True)
        slack_client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=error_msg
        )


# ==================== SLACK INTERACTIONS ====================

def extract_metadata_from_slack(blocks):
    """Extract URL and title from Slack message"""
    try:
        text_section = blocks[0]['text']['text']
        
        # Extract URL
        url_match = re.search(r'<([^|>]+)\|', text_section)
        url = url_match.group(1) if url_match else ""
        
        # Extract title
        title_match = re.search(r'\|([^>]+)>', text_section)
        title = title_match.group(1) if title_match else "News Update"
        
        # Extract category (look for "Category:" line)
        category_match = re.search(r'\*Category:\*\s+(\w+)', text_section)
        category = category_match.group(1) if category_match else "technology"
        
        return url, title, category
        
    except Exception as e:
        print(f"⚠️ Metadata extraction failed: {e}", flush=True)
        return "", "News Update", "technology"


@app.route('/slack/interactions', methods=['POST'])
def slack_interactions():
    """Handle Slack button clicks"""
    if not verifier.is_valid_request(request.get_data(), request.headers):
        return jsonify({"error": "invalid_request"}), 403
    
    data = request.form.to_dict()
    payload = json.loads(data['payload'])
    
    user_name = payload['user']['username']
    action_value = payload['actions'][0]['value']
    action_type, lead_id = action_value.split('_')
    
    original_blocks = payload['message']['blocks']
    url, title, category = extract_metadata_from_slack(original_blocks)
    
    if action_type == "urgent":
        # Launch background thread
        thread = threading.Thread(
            target=process_urgent_in_background,
            args=(lead_id, title, category, url, payload['channel']['id'], payload['message']['ts'])
        )
        thread.daemon = True
        thread.start()
        
        feedback = f"🔥 *URGENT* by @{user_name}\n\n⚡ Deep research + AI generation starting...\nCheck thread for updates."
        
    elif action_type == "approve":
        update_lead_status(lead_id, "queued")
        feedback = f"✅ *QUEUED* by @{user_name}\n\nWill publish in turn (every 20 min)."
        
    elif action_type == "reject":
        update_lead_status(lead_id, "rejected")
        feedback = f"❌ *REJECTED* by @{user_name}"
    
    else:
        feedback = f"⚠️ Unknown action"
    
    new_blocks = [
        original_blocks[0],
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": feedback}]
        },
        {"type": "divider"}
    ]
    
    return jsonify({"replace_original": "true", "blocks": new_blocks})


@app.route('/health', methods=['GET'])
def health_check():
    """Health check"""
    return jsonify({"status": "healthy", "service": "bot_server_v5"}), 200


# ==================== ENTRY POINT ====================

if __name__ == "__main__":
    print("="*60)
    print("🤖 BOT SERVER v5.0 - DEEP RESEARCH")
    print("="*60)
    print("✓ Gemini Deep Research")
    print("✓ OpenAI GPT-4 generation")
    print("✓ Modular publishing")
    print("="*60)
    print("\n🚀 Server starting on port 3000...\n")
    
    app.run(port=3000, host='0.0.0.0')