"""
Publisher Service - Uses Deep Research + OpenAI
"""
import time
from datetime import datetime, timezone

from config import get_current_utc_time, directus_request, publish_article_to_directus, update_lead_status
from ai_content import create_complete_article


# ==================== QUEUE MANAGEMENT ====================

def get_last_published_time():
    """Get timestamp of last published article"""
    from config import ARTICLE_COLLECTION
    
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
    """Get oldest queued lead (FIFO)"""
    result = directus_request(
        'GET',
        '/items/news_leads?filter[status][_eq]=queued&sort=date_created&limit=1'
    )
    
    if result and 'data' in result and result['data']:
        return result['data'][0]
    
    return None


def calculate_minutes_since_last_publish():
    """Calculate minutes since last publish"""
    last_pub = get_last_published_time()
    
    if not last_pub:
        return 999
    
    now = get_current_utc_time()
    minutes = (now - last_pub).total_seconds() / 60
    
    return minutes


# ==================== QUEUE PROCESSOR ====================

def process_queue():
    """Process queued leads with deep research"""
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
        
        # Create article using deep research + OpenAI
        article_data = create_complete_article(
            title=lead['title'],
            category=lead.get('category', 'technology')
        )
        
        if not article_data or not article_data.get('content'):
            print(f"   ❌ Article generation failed", flush=True)
            update_lead_status(lead['id'], "processed")
            return
        
        # Publish using modular function
        success = publish_article_to_directus(
            title=article_data['title'],
            content=article_data['content'],
            short_description=article_data['short_description'],
            category=lead.get('category', 'technology'),
            source_url=lead['source_url'],
            featured_image=article_data.get('featured_image'),
            slug=article_data.get('slug')
        )
        
        if success:
            update_lead_status(lead['id'], "processed")
            word_count = len(article_data['content'].split())
            print(f"   ✅ Published ({word_count} words)!", flush=True)
        else:
            print(f"   ❌ Publishing failed", flush=True)
        
        print(f"{'='*60}\n", flush=True)
        
    except Exception as e:
        print(f"❌ Queue error: {e}", flush=True)
        import traceback
        traceback.print_exc()


# ==================== MAIN LOOP ====================

def run_publisher():
    """Main service loop"""
    print(f"🟢 Publisher active", flush=True)
    print(f"📋 Publish interval: 20 minutes", flush=True)
    print(f"🔄 Check interval: 5 minutes\n", flush=True)
    
    while True:
        try:
            current_time = get_current_utc_time()
            print(f"\n⏰ Check: {current_time.strftime('%H:%M:%S UTC')}", flush=True)
            process_queue()
            
        except KeyboardInterrupt:
            print("\n👋 Shutting down...")
            break
            
        except Exception as e:
            print(f"❌ Critical: {e}", flush=True)
            import traceback
            traceback.print_exc()
        
        print(f"💤 Sleep 5 min\n", flush=True)
        time.sleep(300)


# ==================== ENTRY POINT ====================

if __name__ == "__main__":
    print("="*60)
    print("📰 PUBLISHER v5.0 - DEEP RESEARCH")
    print("="*60)
    print("✓ Gemini Deep Research")
    print("✓ OpenAI GPT-4 generation")
    print("✓ 1500-2000 word articles")
    print("✓ Rich formatting & tables")
    print("✓ Zero AI detection")
    print("="*60)
    print()
    
    run_publisher()