"""
Slack Bot Server - Handles button interactions
Enhanced with rich article generation
"""
import json
import threading
import re
from flask import Flask, request, jsonify
from slack_sdk.signature import SignatureVerifier
from slack_sdk import WebClient

from config import (
    DIRECTUS_URL, SLACK_SIGNING_SECRET, SLACK_BOT_TOKEN, ARTICLE_COLLECTION,
    create_slug, scrape_full_article, directus_request
)
from ai_content import analyze_news_story, write_full_article

app = Flask(__name__)
verifier = SignatureVerifier(SLACK_SIGNING_SECRET)
slack_client = WebClient(token=SLACK_BOT_TOKEN)

# ==================== ARTICLE PUBLISHING ====================

def publish_article_to_directus(title, html_content, source_url, meta_description, category, image_url=None, status="published"):
    """Publish article with full metadata"""
    payload = {
        "status": "published",
        "title": title,
        "slug": create_slug(title),
        "content": html_content,
        "short_description": meta_description,
        "category": category,
        "featured_image": image_url if image_url else None
    }
    
    result = directus_request('POST', f'/items/{ARTICLE_COLLECTION}', payload)
    return result is not None


def update_lead_status(lead_id, status):
    """Update news lead status"""
    result = directus_request('PATCH', f'/items/news_leads/{lead_id}', {"status": status})
    return result is not None


# ==================== URGENT PROCESSING ====================

def process_urgent_in_background(lead_id, title, url, channel_id, thread_ts):
    """Background worker for urgent posts"""
    try:
        print(f"\n🔥 URGENT: Processing #{lead_id}", flush=True)
        
        print(f"   📥 Scraping...", flush=True)
        scraped = scrape_full_article(url, max_chars=20000)
        
        print(f"   🧠 Deep analysis ({scraped['word_count']} words)...", flush=True)
        analysis = analyze_news_story(scraped)
        
        print(f"   ✍️  Writing 1000+ word article...", flush=True)
        article_html = write_full_article(title, scraped['text'], analysis)
        
        if not article_html or len(article_html) < 500:
            raise ValueError("Article generation failed")
        
        meta_description = analysis.get('meta_description', title[:150])
        category = analysis.get('category', 'technology')
        
        print(f"   📤 Publishing...", flush=True)
        success = publish_article_to_directus(
            title=title,
            html_content=article_html,
            source_url=url,
            meta_description=meta_description,
            category=category,
            status="published"
        )
        
        if success:
            update_lead_status(lead_id, "processed")
            word_count = len(article_html.split())
            message = f"🚀 *PUBLISHED LIVE!*\n\n✅ Title: {title}\n📊 Length: {word_count} words\n📁 Category: {category}"
            print(f"   ✅ Published ({word_count} words)", flush=True)
        else:
            message = f"❌ *ERROR:* Failed to publish.\n\nCheck Directus logs for details."
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

def extract_metadata_from_slack_message(blocks):
    """Extract URL and title from Slack message"""
    try:
        text_section = blocks[0]['text']['text']
        url_match = re.search(r'<([^|>]+)\|', text_section)
        url = url_match.group(1) if url_match else ""
        title_match = re.search(r'\|([^>]+)>', text_section)
        title = title_match.group(1) if title_match else "News Update"
        return url, title
    except Exception as e:
        print(f"⚠️ Metadata extraction failed: {e}", flush=True)
        return "", "News Update"


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
    url, title = extract_metadata_from_slack_message(original_blocks)
    
    if action_type == "urgent":
        thread = threading.Thread(
            target=process_urgent_in_background,
            args=(lead_id, title, url, payload['channel']['id'], payload['message']['ts'])
        )
        thread.daemon = True
        thread.start()
        
        feedback = f"🔥 *URGENT* by @{user_name}\n\n⚡ Generating 1000+ word article with rich formatting...\nCheck thread for updates."
        
    elif action_type == "approve":
        update_lead_status(lead_id, "queued")
        feedback = f"✅ *QUEUED* by @{user_name}\n\nArticle will publish in turn (every 20 min)."
        
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
    
    return jsonify({
        "replace_original": "true",
        "blocks": new_blocks
    })


@app.route('/health', methods=['GET'])
def health_check():
    """Health check for Coolify"""
    return jsonify({"status": "healthy", "service": "bot_server"}), 200


# ==================== ENTRY POINT ====================

if __name__ == "__main__":
    print("="*60)
    print("🤖 SLACK BOT SERVER v4.0 - ENHANCED")
    print("="*60)
    print("✓ 1000+ word articles")
    print("✓ Rich formatting & zero AI detection")
    print("✓ Full titles & SEO slugs")
    print("✓ Category & metadata support")
    print("="*60)
    print("\n🚀 Server starting on port 3000...\n")
    
    app.run(port=3000, host='0.0.0.0')