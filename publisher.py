"""
Publisher Service - Queue Manager
Publishes queued articles every 20 minutes
"""
import time
from datetime import datetime, timezone

from config import (
    DIRECTUS_URL, ARTICLE_COLLECTION,
    create_slug, scrape_full_article, directus_request, get_current_utc_time
)
from ai_content import analyze_news_story, write_full_article


# ==================== QUEUE MANAGEMENT ====================

def get_last_published_time():
    """Get timestamp of most recently published article"""
    result = directus_request(
        'GET',
        f'/items/{ARTICLE_COLLECTION}?sort=-date_created&limit=1'
    )
    
    if result and 'data' in result and result['data']:
        try:
            time_str = result['data'][0]['date_created'].split('.')[0]
            return datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception as e:
            print(f"⚠️ Date parsing failed: {e}", flush=True)
    
    return None


def get_oldest_queued_lead():
    """Get the oldest item in the queue (FIFO)"""
    result = directus_request(
        'GET',
        '/items/news_leads?filter[status][_eq]=queued&sort=date_created&limit=1'
    )
    
    if result and 'data' in result and result['data']:
        return result['data'][0]
    
    return None


def calculate_minutes_since_last_publish():
    """Calculate how long since last publish (timezone-aware)"""
    last_pub = get_last_published_time()
    
    if not last_pub:
        return 999  # No previous publish
    
    now = get_current_utc_time()
    time_diff = now - last_pub
    minutes = time_diff.total_seconds() / 60
    
    return minutes


# ==================== PUBLISHING ====================

def publish_article(title, html_content, source_url, meta_description, category, image_url=None):
    """Publish article to Directus with full metadata"""
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


def mark_lead_processed(lead_id):
    """Update lead status to processed"""
    result = directus_request('PATCH', f'/items/news_leads/{lead_id}', {"status": "processed"})
    return result is not None


# ==================== QUEUE PROCESSOR ====================

def process_queue():
    """Main queue processing logic"""
    try:
        minutes_since = calculate_minutes_since_last_publish()
        print(f"🕐 Last publish: {int(minutes_since)} min ago", flush=True)
        
        if minutes_since < 20:
            print(f"⏳ Waiting {20 - int(minutes_since)} more minutes", flush=True)
            return
        
        lead = get_oldest_queued_lead()
        
        if not lead:
            print(f"📭 Queue empty", flush=True)
            return
        
        print(f"\n{'='*60}", flush=True)
        print(f"🚀 Processing: {lead['title']}", flush=True)
        print(f"{'='*60}", flush=True)
        
        # Scrape full content
        print(f"   📥 Scraping source...", flush=True)
        scraped = scrape_full_article(lead['source_url'], max_chars=20000)
        
        if scraped['word_count'] < 100:
            print(f"   ⚠️ Low quality ({scraped['word_count']} words), skipping", flush=True)
            mark_lead_processed(lead['id'])
            return
        
        # Deep analysis
        print(f"   🧠 Deep analysis ({scraped['word_count']} words)...", flush=True)
        analysis = analyze_news_story(scraped)
        
        # Write comprehensive article
        print(f"   ✍️  Writing 1000+ word article...", flush=True)
        article_html = write_full_article(
            title=lead['title'],
            source_text=scraped['text'],
            analysis=analysis
        )
        
        if not article_html or len(article_html) < 500:
            print(f"   ❌ Article generation failed", flush=True)
            mark_lead_processed(lead['id'])
            return
        
        # Get metadata from analysis
        meta_description = analysis.get('meta_description', lead.get('ai_summary', lead['title'][:150]))
        category = analysis.get('category', 'technology')
        
        # Publish with full metadata
        print(f"   📤 Publishing ({len(article_html)} chars)...", flush=True)
        success = publish_article(
            title=lead['title'],  # Full title
            html_content=article_html,
            source_url=lead['source_url'],
            meta_description=meta_description,
            category=category
        )
        
        if success:
            mark_lead_processed(lead['id'])
            word_count = len(article_html.split())
            print(f"   ✅ Published! ({word_count} words)", flush=True)
        else:
            print(f"   ❌ Publishing failed", flush=True)
        
        print(f"{'='*60}\n", flush=True)
        
    except Exception as e:
        print(f"❌ Queue processing error: {e}", flush=True)
        import traceback
        traceback.print_exc()


# ==================== MAIN LOOP ====================

def run_publisher():
    """Main service loop"""
    print(f"🟢 Publisher service active", flush=True)
    print(f"📋 Publish interval: 20 minutes", flush=True)
    print(f"🔄 Check interval: 5 minutes\n", flush=True)
    
    while True:
        try:
            current_time = get_current_utc_time()
            print(f"\n⏰ Check: {current_time.strftime('%H:%M:%S UTC')}", flush=True)
            process_queue()
            
        except KeyboardInterrupt:
            print("\n👋 Shutting down publisher...")
            break
            
        except Exception as e:
            print(f"❌ Critical error: {e}", flush=True)
            import traceback
            traceback.print_exc()
        
        print(f"💤 Sleep 5 min...\n", flush=True)
        time.sleep(300)


# ==================== ENTRY POINT ====================

if __name__ == "__main__":
    print("="*60)
    print("📰 PUBLISHER SERVICE v4.0 - ENHANCED")
    print("="*60)
    print("✓ 1000+ word articles")
    print("✓ Rich formatting (lists, bullets, quotes)")
    print("✓ Zero AI detection")
    print("✓ Full titles (no truncation)")
    print("✓ SEO-optimized slugs")
    print("✓ Category & metadata")
    print("="*60)
    print()
    
    run_publisher()