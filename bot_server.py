"""
Slack Bot Server - Handles button interactions
Urgent posts: Instant publishing
Approve posts: Queue for scheduled publishing
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

def publish_article_to_directus(title, html_content, source_url, status="published"):
    """
    Publish article to Directus CMS
    """
    payload = {
        "status": status,
        "title": title,
        "slug": create_slug(title),
        "content": html_content,
        "seo_title": title[:60],
        "source_link": source_url
    }
    
    result = directus_request('POST', f'/items/{ARTICLE_COLLECTION}', payload)
    return result is not None


def update_lead_status(lead_id, status):
    """Update news lead status in Directus"""
    result = directus_request('PATCH', f'/items/news_leads/{lead_id}', {"status": status})
    return result is not None


# ==================== URGENT PROCESSING ====================

def process_urgent_in_background(lead_id, title, url, channel_id, thread_ts):
    """
    Background worker for urgent posts
    Runs asynchronously so Slack doesn't timeout
    """
    try:
        print(f"\n🔥 URGENT: Processing #{lead_id}", flush=True)
        
        # Step 1: Scrape full content
        print(f"   Scraping...", flush=True)
        scraped = scrape_full_article(url, max_chars=10000)
        
        # Step 2: Analyze for context
        print(f"   Analyzing...", flush=True)
        analysis = analyze_news_story(scraped)
        
        # Step 3: Write humanized article
        print(f"   Writing article...", flush=True)
        article_html = write_full_article(title, scraped['text'], analysis)
        
        if not article_html or len(article_html) < 200:
            raise ValueError("Article generation failed")
        
        # Step 4: Publish immediately
        print(f"   Publishing...", flush=True)
        success = publish_article_to_directus(
            title=title,
            html_content=article_html,
            source_url=url,
            status="published"
        )
        
        if success:
            # Step 5: Update lead status
            update_lead_status(lead_id, "processed")
            
            # Step 6: Notify Slack
            message = f"🚀 *PUBLISHED:* Article is live on the site!\n\n✅ Title: {title}\n📊 Length: {len(article_html)} chars"
            print(f"   ✅ Published successfully", flush=True)
        else:
            message = f"❌ *ERROR:* Failed to publish article.\n\nPlease check Directus logs."
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
    """
    Extract URL and title from Slack message blocks
    Format: *<URL|Title>*
    """
    try:
        text_section = blocks[0]['text']['text']
        
        # Extract URL
        url_match = re.search(r'<([^|>]+)\|', text_section)
        url = url_match.group(1) if url_match else ""
        
        # Extract title
        title_match = re.search(r'\|([^>]+)>', text_section)
        title = title_match.group(1) if title_match else "News Update"
        
        return url, title
        
    except Exception as e:
        print(f"⚠️ Metadata extraction failed: {e}", flush=True)
        return "", "News Update"


@app.route('/slack/interactions', methods=['POST'])
def slack_interactions():
    """
    Handle Slack button clicks
    Routes: Approve → Queue, Urgent → Instant Publish, Reject → Archive
    """
    # Verify Slack signature
    if not verifier.is_valid_request(request.get_data(), request.headers):
        return jsonify({"error": "invalid_request"}), 403
    
    # Parse payload
    data = request.form.to_dict()
    payload = json.loads(data['payload'])
    
    user_name = payload['user']['username']
    action_value = payload['actions'][0]['value']  # "approve_123", "urgent_123", etc.
    action_type, lead_id = action_value.split('_')
    
    # Extract article metadata
    original_blocks = payload['message']['blocks']
    url, title = extract_metadata_from_slack_message(original_blocks)
    
    # Handle different actions
    if action_type == "urgent":
        # Launch background thread for instant publishing
        thread = threading.Thread(
            target=process_urgent_in_background,
            args=(lead_id, title, url, payload['channel']['id'], payload['message']['ts'])
        )
        thread.daemon = True
        thread.start()
        
        feedback = f"🔥 *URGENT* by @{user_name}\n\nArticle is being generated and published now. Check thread for updates."
        
    elif action_type == "approve":
        # Add to queue for scheduled publishing
        update_lead_status(lead_id, "queued")
        feedback = f"✅ *QUEUED* by @{user_name}\n\nArticle will publish in turn (every 20 minutes)."
        
    elif action_type == "reject":
        # Mark as rejected
        update_lead_status(lead_id, "rejected")
        feedback = f"❌ *REJECTED* by @{user_name}"
    
    else:
        feedback = f"⚠️ Unknown action: {action_type}"
    
    # Update Slack message
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
    """Health check endpoint for Coolify"""
    return jsonify({"status": "healthy", "service": "bot_server"}), 200


# ==================== ENTRY POINT ====================

if __name__ == "__main__":
    print("="*60)
    print("🤖 SLACK BOT SERVER v3.0")
    print("="*60)
    print("✓ Handles Slack button interactions")
    print("✓ Urgent: Instant AI article generation")
    print("✓ Approve: Queue for scheduled publishing")
    print("✓ Humanized content (no AI fingerprints)")
    print("="*60)
    print("\n🚀 Server starting on port 3000...\n")
    
    app.run(port=3000, host='0.0.0.0')