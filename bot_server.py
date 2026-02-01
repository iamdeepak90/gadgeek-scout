"""
Slack Bot Server v7.0 - Enhanced with better error handling
Uses Deep Research + Humanized Content Generation
"""
import json
import threading
import re
import traceback
from flask import Flask, request, jsonify
from slack_sdk.signature import SignatureVerifier
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from config import SLACK_SIGNING_SECRET, SLACK_BOT_TOKEN, publish_article_to_directus, update_lead_status
from ai_content import create_complete_article

app = Flask(__name__)
verifier = SignatureVerifier(SLACK_SIGNING_SECRET)
slack_client = WebClient(token=SLACK_BOT_TOKEN)


# ==================== URGENT PROCESSING ====================

def process_urgent_in_background(lead_id, title, category, url, channel_id, thread_ts):
    """
    Process urgent posts with deep research and humanized content
    Runs in background thread to avoid blocking Slack response
    
    Args:
        lead_id: Database lead ID
        title: Article title
        category: Article category
        url: Source URL
        channel_id: Slack channel ID
        thread_ts: Slack thread timestamp
    """
    try:
        print(f"\n{'='*60}", flush=True)
        print(f"🔥 URGENT PROCESSING: Lead #{lead_id}", flush=True)
        print(f"📰 {title[:80]}", flush=True)
        print(f"{'='*60}", flush=True)
        
        # Send initial status update
        try:
            slack_client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f"⚡ Starting urgent processing...\n🔬 Research phase beginning"
            )
        except SlackApiError as e:
            print(f"   ⚠️ Slack update failed: {e.response.get('error')}", flush=True)
        
        # Create article using deep research + OpenAI + humanization
        article_data = create_complete_article(
            title=title,
            category=category
        )
        
        if not article_data or not article_data.get('content'):
            error_msg = "Article generation failed - missing content"
            print(f"   ❌ {error_msg}", flush=True)
            
            slack_client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f"❌ *ERROR:* {error_msg}"
            )
            update_lead_status(lead_id, "processed")
            return
        
        # Validate article quality
        word_count = len(article_data['content'].split())
        
        if word_count < 500:
            print(f"   ⚠️ Article too short ({word_count} words)", flush=True)
            slack_client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f"⚠️ *WARNING:* Article only {word_count} words (expected 1200+)\nPublishing anyway..."
            )
        
        # Send generation complete update
        try:
            slack_client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f"✅ Article generated ({word_count} words)\n📤 Publishing to Directus..."
            )
        except SlackApiError:
            pass
        
        # Publish to Directus
        success = publish_article_to_directus(
            title=article_data['title'],
            content=article_data['content'],
            short_description=article_data['short_description'],
            category=category,
            source_url=url,
            featured_image=article_data.get('featured_image'),
            slug=article_data.get('slug')
        )
        
        # Update lead status
        update_lead_status(lead_id, "processed")
        
        # Send final status
        if success:
            message = f"""🚀 *PUBLISHED LIVE!*

✅ {article_data['title']}
📊 {word_count} words
📁 {category}
🔗 Source: {url[:100]}

*Status:* Successfully published to Directus"""
            
            print(f"   ✅ URGENT article published ({word_count} words)", flush=True)
        else:
            message = f"""❌ *PUBLISHING ERROR*

Article was generated successfully ({word_count} words) but failed to publish to Directus.

Please check Directus connection and try again."""
            
            print(f"   ❌ Publishing to Directus failed", flush=True)
        
        slack_client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=message
        )
        
        print(f"{'='*60}\n", flush=True)
        
    except Exception as e:
        error_msg = f"Critical error: {str(e)[:200]}"
        print(f"   ❌ {error_msg}", flush=True)
        print(f"   Traceback:\n{traceback.format_exc()}", flush=True)
        
        try:
            slack_client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f"❌ *CRITICAL ERROR:*\n```{error_msg}```"
            )
        except:
            print(f"   ❌ Failed to send error message to Slack", flush=True)


# ==================== SLACK INTERACTIONS ====================

def extract_metadata_from_slack(blocks):
    """
    Extract URL, title, and category from Slack message blocks
    
    Args:
        blocks: Slack message blocks
    
    Returns:
        tuple: (url, title, category)
    """
    try:
        text_section = blocks[0]['text']['text']
        
        # Extract URL from markdown link format: <URL|Title>
        url_match = re.search(r'<([^|>]+)\|', text_section)
        url = url_match.group(1) if url_match else ""
        
        # Extract title from markdown link
        title_match = re.search(r'\|([^>]+)>', text_section)
        title = title_match.group(1) if title_match else "Tech News Update"
        
        # Extract category from bold "Category:" line
        category_match = re.search(r'\*Category:\*\s+(\w+)', text_section)
        category = category_match.group(1) if category_match else "technology"
        
        print(f"   📋 Extracted metadata:", flush=True)
        print(f"      URL: {url[:80]}", flush=True)
        print(f"      Title: {title[:80]}", flush=True)
        print(f"      Category: {category}", flush=True)
        
        return url, title, category
        
    except Exception as e:
        print(f"   ⚠️ Metadata extraction failed: {e}", flush=True)
        print(f"      Using fallback values", flush=True)
        return "", "Tech News Update", "technology"


@app.route('/slack/interactions', methods=['POST'])
def slack_interactions():
    """
    Handle Slack button clicks (Approve, Urgent, Reject)
    
    Returns:
        JSON response for Slack
    """
    # Verify request signature
    if not verifier.is_valid_request(request.get_data(), request.headers):
        print(f"⚠️ Invalid Slack request signature", flush=True)
        return jsonify({"error": "invalid_request"}), 403
    
    try:
        # Parse payload
        data = request.form.to_dict()
        payload = json.loads(data['payload'])
        
        user_name = payload['user']['username']
        action_value = payload['actions'][0]['value']
        action_type, lead_id = action_value.split('_')
        
        original_blocks = payload['message']['blocks']
        url, title, category = extract_metadata_from_slack(original_blocks)
        
        print(f"\n{'='*60}", flush=True)
        print(f"🎯 Slack Action: {action_type.upper()}", flush=True)
        print(f"👤 User: @{user_name}", flush=True)
        print(f"🔖 Lead ID: {lead_id}", flush=True)
        print(f"{'='*60}", flush=True)
        
        # Handle different action types
        if action_type == "urgent":
            # Launch background thread for urgent processing
            thread = threading.Thread(
                target=process_urgent_in_background,
                args=(lead_id, title, category, url, payload['channel']['id'], payload['message']['ts']),
                daemon=True
            )
            thread.start()
            
            feedback = f"🔥 *URGENT* by @{user_name}\n\n⚡ Deep research + AI generation starting...\n📊 Check thread for real-time updates"
            
            print(f"   ✅ Urgent thread launched", flush=True)
            
        elif action_type == "approve":
            # Queue for normal publishing
            update_lead_status(lead_id, "queued")
            feedback = f"✅ *QUEUED* by @{user_name}\n\n📅 Will publish in turn (every 20 min)\n⏰ Processed via publisher service"
            
            print(f"   ✅ Queued for publishing", flush=True)
            
        elif action_type == "reject":
            # Reject the lead
            update_lead_status(lead_id, "rejected")
            feedback = f"❌ *REJECTED* by @{user_name}\n\n🚫 Will not be published"
            
            print(f"   ✅ Rejected", flush=True)
        else:
            feedback = f"⚠️ Unknown action: {action_type}"
            print(f"   ⚠️ Unknown action type", flush=True)
        
        print(f"{'='*60}\n", flush=True)
        
        # Update Slack message with action feedback
        new_blocks = [
            original_blocks[0],  # Keep original message
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": feedback}]
            },
            {"type": "divider"}
        ]
        
        return jsonify({"replace_original": "true", "blocks": new_blocks})
        
    except Exception as e:
        error_msg = f"Error processing Slack interaction: {str(e)}"
        print(f"❌ {error_msg}", flush=True)
        print(f"   Traceback:\n{traceback.format_exc()}", flush=True)
        
        return jsonify({"error": "internal_error", "message": error_msg}), 500


@app.route('/health', methods=['GET'])
def health_check():
    """
    Health check endpoint
    
    Returns:
        JSON with service status
    """
    from config import check_api_health
    
    health_status = check_api_health()
    
    return jsonify({
        "status": "healthy",
        "service": "bot_server_v7",
        "apis": health_status
    }), 200


@app.route('/', methods=['GET'])
def index():
    """
    Root endpoint with service info
    """
    return jsonify({
        "service": "Slack Bot Server v7.0",
        "status": "running",
        "features": [
            "Deep research with Gemini 2.0",
            "Humanized content with OpenAI GPT-4",
            "Urgent processing in background threads",
            "Quality validation",
            "Real-time Slack updates"
        ],
        "endpoints": {
            "/slack/interactions": "POST - Handle Slack button clicks",
            "/health": "GET - Health check",
            "/": "GET - Service info"
        }
    }), 200


# ==================== ERROR HANDLERS ====================

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "endpoint_not_found"}), 404


@app.errorhandler(500)
def internal_error(error):
    print(f"❌ Internal server error: {error}", flush=True)
    return jsonify({"error": "internal_server_error"}), 500


# ==================== ENTRY POINT ====================

if __name__ == "__main__":
    print("="*70)
    print("🤖 BOT SERVER v7.0 - ENHANCED")
    print("="*70)
    print("✓ Gemini 2.0 Deep Research")
    print("✓ OpenAI GPT-4 Humanized Generation")
    print("✓ Advanced error handling")
    print("✓ Real-time Slack updates")
    print("✓ Quality validation")
    print("✓ Retry logic with exponential backoff")
    print("="*70)
    
    # Check API health on startup
    from config import check_api_health
    health = check_api_health()
    
    print(f"\n📊 API Health Check:")
    for service, status in health.items():
        icon = "✅" if status in ['healthy', 'configured'] else "❌"
        print(f"   {icon} {service.capitalize()}: {status}")
    print()
    
    print("="*70)
    print("\n🚀 Server starting on port 3000...\n")
    
    app.run(port=3000, host='0.0.0.0', debug=False)