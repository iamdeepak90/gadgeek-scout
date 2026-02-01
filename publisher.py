"""
Publisher Service - Queue Manager
Publishes queued articles every 20 minutes with humanized content
"""
import time
from datetime import datetime, timedelta

from config import (
    DIRECTUS_URL, ARTICLE_COLLECTION,
    create_slug, scrape_full_article, directus_request
)
from ai_content import analyze_news_story, write_full_article


# ==================== QUEUE MANAGEMENT ====================

def get_last_published_time():
    """
    Get timestamp of most recently published article
    """
    result = directus_request(
        'GET',
        f'/items/{ARTICLE_COLLECTION}?sort=-date_created&limit=1'
    )
    
    if result and 'data' in result and result['data']:
        try:
            # Parse: 2026-01-31T21:00:00.000Z → datetime
            time_str = result['data'][0]['date_created'].split('.')[0]
            return datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S")
        except Exception as e:
            print(f"⚠️ Date parsing failed: {e}", flush=True)
    
    return None


def get_oldest_queued_lead():
    """
    Get the oldest item in the queue (FIFO)
    """
    result = directus_request(
        'GET',
        '/items/news_leads?filter[status][_eq]=queued&sort=date_created&limit=1'
    )
    
    if result and 'data' in result and result['data']:
        return result['data'][0]
    
    return None


def calculate_minutes_since_last_publish():
    """
    Calculate how long it's been since last publish
    """
    last_pub = get_last_published_time()
    
    if not last_pub:
        return 999  # No previous publish, lane is clear
    
    now = datetime.utcnow()
    time_diff = now - last_pub
    minutes = time_diff.total_seconds() / 60
    
    return minutes


# ==================== PUBLISHING ====================

def publish_article(title, html_content, source_url):
    """
    Publish article to Directus
    """
    payload = {
        "status": "published",
        "title": title,
        "slug": create_slug(title),
        "content": html_content,
        "seo_title": title[:60],
        "source_link": source_url
    }
    
    result = directus_request('POST', f'/items/{ARTICLE_COLLECTION}', payload)
    return result is not None


def mark_lead_processed(lead_id):
    """Update lead status to processed"""
    result = directus_request('PATCH', f'/items/news_leads/{lead_id}', {"status": "processed"})
    return result is not None


# ==================== QUEUE PROCESSOR ====================

def process_queue():
    """
    Main queue processing logic
    Only publishes if 20+ minutes have passed since last publish
    """
    try:
        # Check time since last publish
        minutes_since = calculate_minutes_since_last_publish()
        print(f"🕐 Last publish: {int(minutes_since)} min ago", flush=True)
        
        if minutes_since < 20:
            print(f"⏳ Waiting... (need {20 - int(minutes_since)} more min)", flush=True)
            return
        
        # Lane is clear, check queue
        lead = get_oldest_queued_lead()
        
        if not lead:
            print(f"📭 Queue empty", flush=True)
            return
        
        print(f"\n{'='*60}", flush=True)
        print(f"🚀 Processing: {lead['title']}", flush=True)
        print(f"{'='*60}", flush=True)
        
        # Scrape full content
        print(f"   📥 Scraping source...", flush=True)
        scraped = scrape_full_article(lead['source_url'], max_chars=10000)
        
        if scraped['word_count'] < 100:
            print(f"   ⚠️ Low quality content ({scraped['word_count']} words), skipping", flush=True)
            mark_lead_processed(lead['id'])
            return
        
        # Analyze content
        print(f"   🧠 Analyzing ({scraped['word_count']} words)...", flush=True)
        analysis = analyze_news_story(scraped)
        
        # Write humanized article
        print(f"   ✍️  Writing article...", flush=True)
        article_html = write_full_article(
            title=lead['title'],
            source_text=scraped['text'],
            analysis=analysis
        )
        
        if not article_html or len(article_html) < 200:
            print(f"   ❌ Article generation failed", flush=True)
            mark_lead_processed(lead['id'])
            return
        
        # Publish
        print(f"   📤 Publishing ({len(article_html)} chars)...", flush=True)
        success = publish_article(
            title=lead['title'],
            html_content=article_html,
            source_url=lead['source_url']
        )
        
        if success:
            mark_lead_processed(lead['id'])
            print(f"   ✅ Published successfully!", flush=True)
        else:
            print(f"   ❌ Publishing failed", flush=True)
        
        print(f"{'='*60}\n", flush=True)
        
    except Exception as e:
        print(f"❌ Queue processing error: {e}", flush=True)
        import traceback
        traceback.print_exc()


# ==================== MAIN LOOP ====================

def run_publisher():
    """
    Main service loop
    Checks queue every 5 minutes
    """
    print(f"🟢 Publisher service active", flush=True)
    print(f"📋 Publish interval: 20 minutes", flush=True)
    print(f"🔄 Check interval: 5 minutes\n", flush=True)
    
    while True:
        try:
            print(f"\n⏰ Check: {datetime.now().strftime('%H:%M:%S')}", flush=True)
            process_queue()
            
        except KeyboardInterrupt:
            print("\n👋 Shutting down publisher...")
            break
            
        except Exception as e:
            print(f"❌ Critical error: {e}", flush=True)
            import traceback
            traceback.print_exc()
        
        print(f"💤 Sleep 5 min...\n", flush=True)
        time.sleep(300)  # Check every 5 minutes


# ==================== ENTRY POINT ====================

if __name__ == "__main__":
    print("="*60)
    print("📰 PUBLISHER SERVICE v3.0")
    print("="*60)
    print("✓ Queue-based publishing (FIFO)")
    print("✓ 20-minute spacing between articles")
    print("✓ Humanized AI content generation")
    print("✓ Full article scraping & analysis")
    print("="*60)
    print()
    
    run_publisher()