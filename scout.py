"""
Enhanced News Scout with Deep AI Integration
Monitors RSS feeds and generates humanized lead summaries
"""
import feedparser
import time
import warnings
from datetime import datetime
from difflib import SequenceMatcher
from bs4 import BeautifulSoup
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from config import (
    DIRECTUS_URL, SLACK_BOT_TOKEN, SLACK_CHANNEL, RSS_FEEDS,
    get_domain_name, directus_request, scrape_full_article
)
from ai_content import analyze_news_story, generate_humanized_headline_and_summary

warnings.filterwarnings("ignore")

slack = WebClient(token=SLACK_BOT_TOKEN)

# ==================== CONTENT EXTRACTION ====================

def extract_rss_content(entry):
    """Fallback: Extract content from RSS feed entry"""
    text_parts = []
    
    if hasattr(entry, 'title'):
        text_parts.append(entry.title)
    
    if hasattr(entry, 'content') and isinstance(entry.content, list):
        for item in entry.content:
            text_parts.append(item.get('value', ''))
    
    if hasattr(entry, 'summary'):
        text_parts.append(entry.summary)
    
    if hasattr(entry, 'description'):
        text_parts.append(entry.description)
    
    full_text = " ".join(text_parts)
    clean_text = BeautifulSoup(full_text, "lxml").get_text(separator=" ").strip()
    
    return {
        'title': entry.title if hasattr(entry, 'title') else 'Unknown',
        'text': clean_text,
        'word_count': len(clean_text.split())
    }


def get_comprehensive_content(entry):
    """
    Get best available content - scrape full article or use RSS
    """
    # Try full scraping first
    scraped = scrape_full_article(entry.link, max_chars=6000)
    
    # If scraping succeeds and has good content, use it
    if scraped['word_count'] > 100:
        quality_score = min(100, scraped['word_count'] / 10)
        print(f"✅ Scraped: {scraped['word_count']} words ({quality_score:.0f}% quality)", flush=True)
        return scraped, quality_score
    
    # Fallback to RSS content
    print(f"📰 Using RSS content", flush=True)
    rss_content = extract_rss_content(entry)
    quality_score = min(40, rss_content['word_count'] / 10)
    return rss_content, quality_score


# ==================== DUPLICATE DETECTION ====================

def check_exact_duplicate(link):
    """Check if URL already exists"""
    result = directus_request('GET', f'/items/news_leads?filter[source_url][_eq]={link}')
    if result and 'data' in result:
        return len(result['data']) > 0
    return False


def check_semantic_duplicate(title, content_text):
    """Check for similar stories by title similarity"""
    result = directus_request('GET', '/items/news_leads?sort=-date_created&limit=100')
    
    if not result or 'data' not in result:
        return False
    
    for item in result['data']:
        existing_title = item.get('title', '')
        similarity = SequenceMatcher(None, title.lower(), existing_title.lower()).ratio()
        
        if similarity > 0.70:
            print(f"🚫 Similar to: '{existing_title}' ({similarity:.0%})", flush=True)
            return True
    
    return False


# ==================== DATABASE OPERATIONS ====================

def create_lead_in_directus(title, link, summary, metadata=None):
    """Save news lead to Directus"""
    payload = {
        "title": title,
        "source_url": link,
        "ai_summary": summary,
        "status": "pending"
    }
    
    # Add metadata if available
    if metadata:
        if metadata.get('urgency_score'):
            payload['urgency'] = metadata['urgency_score']
        if metadata.get('story_type'):
            payload['category'] = metadata['story_type']
        if metadata.get('key_players'):
            payload['tags'] = ', '.join(metadata['key_players'][:3])
    
    result = directus_request('POST', '/items/news_leads', payload)
    
    if result and 'data' in result:
        lead_id = result['data']['id']
        print(f"💾 Saved: #{lead_id}", flush=True)
        return lead_id
    
    return None


# ==================== SLACK NOTIFICATION ====================

def post_to_slack(title, summary, link, lead_id, metadata=None):
    """Post formatted notification to Slack"""
    source_name = get_domain_name(link)
    
    # Determine urgency
    urgency = metadata.get('urgency_score', 5) if metadata else 5
    if urgency >= 8:
        emoji = "🔥"
    elif urgency >= 6:
        emoji = "⚡"
    else:
        emoji = "📢"
    
    # Format story type
    story_type = metadata.get('story_type', 'news') if metadata else 'news'
    formatted_type = story_type.replace('_', ' ').title()
    
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{emoji} *<{link}|{title}>*\n\n{summary}\n\n*Type:* {formatted_type} | *Source:* {source_name}"
            }
        },
        {
            "type": "actions",
            "block_id": f"action_{lead_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve"},
                    "style": "primary",
                    "value": f"approve_{lead_id}",
                    "action_id": "approve_click"
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔥 Urgent"},
                    "style": "danger",
                    "value": f"urgent_{lead_id}",
                    "action_id": "urgent_click"
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Reject"},
                    "value": f"reject_{lead_id}",
                    "action_id": "reject_click"
                }
            ]
        },
        {"type": "divider"}
    ]
    
    try:
        slack.chat_postMessage(
            channel=SLACK_CHANNEL,
            blocks=blocks,
            text=f"New Lead: {title}",
            unfurl_links=False
        )
        print(f"✅ Slack sent", flush=True)
    except SlackApiError as e:
        print(f"❌ Slack error: {e.response.get('error', 'unknown')}", flush=True)


# ==================== MAIN PROCESSING ====================

def process_article(entry):
    """
    Core workflow: Extract → Analyze → Generate → Save → Notify
    """
    try:
        print(f"\n{'='*60}", flush=True)
        print(f"🔍 {entry.title[:60]}...", flush=True)
        print(f"{'='*60}", flush=True)
        
        # STEP 1: Get content
        content_data, quality_score = get_comprehensive_content(entry)
        
        if quality_score < 10:
            print(f"⚠️ Quality too low ({quality_score}%), skipping", flush=True)
            return None
        
        # STEP 2: Check duplicates
        if check_exact_duplicate(entry.link):
            print(f"🚫 Exact duplicate", flush=True)
            return None
        
        if check_semantic_duplicate(content_data['title'], content_data['text']):
            return None
        
        # STEP 3: Deep AI analysis
        print(f"🧠 Analyzing...", flush=True)
        analysis = analyze_news_story(content_data)
        print(f"   Type: {analysis.get('story_type', 'unknown')}, Urgency: {analysis.get('urgency_score', 0)}/10", flush=True)
        
        # STEP 4: Generate humanized content
        print(f"✍️  Generating...", flush=True)
        generated = generate_humanized_headline_and_summary(content_data, analysis)
        print(f"   Title: '{generated['title']}' ({len(generated['title'])} chars)", flush=True)
        
        # STEP 5: Validate
        if not generated['title'] or len(generated['title']) < 10:
            print(f"⚠️ Invalid title, skipping", flush=True)
            return None
        
        # STEP 6: Save to database
        metadata = {
            'urgency_score': analysis.get('urgency_score', 5),
            'story_type': analysis.get('story_type', 'news'),
            'key_players': analysis.get('key_players', [])
        }
        
        lead_id = create_lead_in_directus(
            title=generated['title'],
            link=entry.link,
            summary=generated['summary'],
            metadata=metadata
        )
        
        if not lead_id:
            print(f"❌ Save failed", flush=True)
            return None
        
        # STEP 7: Notify Slack
        post_to_slack(
            title=generated['title'],
            summary=generated['summary'],
            link=entry.link,
            lead_id=lead_id,
            metadata=metadata
        )
        
        print(f"✅ Success: #{lead_id}", flush=True)
        return lead_id
        
    except Exception as e:
        print(f"❌ Error: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return None


# ==================== MAIN LOOP ====================

def run_scout():
    """Main scanning loop"""
    print(f"\n🚀 Scan: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"📡 Feeds: {len(RSS_FEEDS)}\n", flush=True)
    
    total_processed = 0
    total_created = 0
    
    for feed_url in RSS_FEEDS:
        try:
            source_name = get_domain_name(feed_url)
            print(f"\n📰 {source_name}", flush=True)
            
            feed = feedparser.parse(feed_url)
            
            if not feed.entries:
                print(f"⚠️ No entries", flush=True)
                continue
            
            # Process top 3 articles per feed
            for entry in feed.entries[:3]:
                total_processed += 1
                result = process_article(entry)
                
                if result:
                    total_created += 1
                    time.sleep(4)  # Rate limit for AI calls
                else:
                    time.sleep(1)
            
            time.sleep(2)  # Delay between feeds
            
        except Exception as e:
            print(f"❌ Feed error: {e}", flush=True)
            continue
    
    print(f"\n{'='*60}", flush=True)
    print(f"📊 Results: {total_created}/{total_processed} leads created", flush=True)
    print(f"{'='*60}\n", flush=True)


# ==================== ENTRY POINT ====================

if __name__ == "__main__":
    print("="*60)
    print("🤖 AI NEWS SCOUT v3.0 - HUMANIZED")
    print("="*60)
    print("✓ Deep AI analysis")
    print("✓ Humanized content (no AI fingerprints)")
    print("✓ Multi-strategy content extraction")
    print("✓ Advanced duplicate detection")
    print("✓ Automatic Slack notifications")
    print("="*60)
    print()
    
    while True:
        try:
            run_scout()
        except KeyboardInterrupt:
            print("\n👋 Shutting down...")
            break
        except Exception as e:
            print(f"❌ Critical error: {e}", flush=True)
            import traceback
            traceback.print_exc()
        
        print("💤 Sleep 30 min\n", flush=True)
        time.sleep(1800)