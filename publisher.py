"""
Publisher Service v7.0 - Enhanced with better reliability
Automated queue processing with deep research + humanized content
"""
import time
import traceback
from datetime import datetime, timezone

from config import (
    get_current_utc_time, directus_request, 
    publish_article_to_directus, update_lead_status,
    check_api_health, ARTICLE_COLLECTION
)
from ai_content import create_complete_article


# ==================== QUEUE MANAGEMENT ====================

def get_last_published_time():
    """
    Get timestamp of the most recently published article
    
    Returns:
        datetime: UTC timestamp or None if no articles
    """
    try:
        result = directus_request(
            'GET',
            f'/items/{ARTICLE_COLLECTION}?sort=-date_created&limit=1',
            timeout=10
        )
        
        if result and 'data' in result and len(result['data']) > 0:
            try:
                time_str = result['data'][0]['date_created']
                
                # Handle both formats: with and without microseconds
                if '.' in time_str:
                    time_str = time_str.split('.')[0]
                
                # Remove timezone suffix if present
                if 'Z' in time_str:
                    time_str = time_str.replace('Z', '')
                elif '+' in time_str:
                    time_str = time_str.split('+')[0]
                
                dt = datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                
                print(f"   📅 Last published: {dt.strftime('%Y-%m-%d %H:%M:%S UTC')}", flush=True)
                return dt
                
            except Exception as e:
                print(f"   ⚠️ Date parsing error: {e}", flush=True)
                return None
        
        print(f"   ℹ️ No previous articles found", flush=True)
        return None
        
    except Exception as e:
        print(f"   ⚠️ Failed to get last publish time: {e}", flush=True)
        return None


def get_oldest_queued_lead():
    """
    Get oldest queued lead (FIFO: First In, First Out)
    
    Returns:
        dict: Lead data or None if queue is empty
    """
    try:
        result = directus_request(
            'GET',
            '/items/news_leads?filter[status][_eq]=queued&sort=date_created&limit=1',
            timeout=10
        )
        
        if result and 'data' in result and len(result['data']) > 0:
            lead = result['data'][0]
            print(f"   📋 Found queued lead: #{lead['id']}", flush=True)
            print(f"      Title: {lead.get('title', 'N/A')[:80]}", flush=True)
            return lead
        
        return None
        
    except Exception as e:
        print(f"   ⚠️ Failed to get queued lead: {e}", flush=True)
        return None


def get_queue_stats():
    """
    Get statistics about the current queue
    
    Returns:
        dict: Queue statistics
    """
    try:
        result = directus_request(
            'GET',
            '/items/news_leads?aggregate[count]=id&groupBy[]=status',
            timeout=10
        )
        
        if result and 'data' in result:
            stats = {}
            for item in result['data']:
                status = item.get('status', 'unknown')
                count = item.get('count', {}).get('id', 0)
                stats[status] = count
            return stats
        
        return {}
        
    except Exception as e:
        print(f"   ⚠️ Failed to get queue stats: {e}", flush=True)
        return {}


def calculate_minutes_since_last_publish():
    """
    Calculate minutes elapsed since last published article
    
    Returns:
        float: Minutes elapsed (999 if no previous articles)
    """
    last_pub = get_last_published_time()
    
    if not last_pub:
        return 999  # Large number to trigger immediate publish
    
    now = get_current_utc_time()
    elapsed = (now - last_pub).total_seconds() / 60
    
    return elapsed


# ==================== QUEUE PROCESSOR ====================

def process_queue():
    """
    Main queue processing logic
    Publishes one article if timing is right and queue has items
    """
    try:
        print(f"\n{'='*70}", flush=True)
        print(f"🔄 QUEUE CHECK: {get_current_utc_time().strftime('%Y-%m-%d %H:%M:%S UTC')}", flush=True)
        print(f"{'='*70}", flush=True)
        
        # Check queue stats
        stats = get_queue_stats()
        if stats:
            print(f"\n📊 Queue Statistics:", flush=True)
            for status, count in stats.items():
                icon = "📌" if status == "queued" else "✅" if status == "processed" else "❌" if status == "rejected" else "⏳"
                print(f"   {icon} {status.capitalize()}: {count}", flush=True)
        
        # Check timing
        print(f"\n⏰ Timing Check:", flush=True)
        minutes_since = calculate_minutes_since_last_publish()
        print(f"   Last publish: {int(minutes_since)} minutes ago", flush=True)
        
        if minutes_since < 20:
            wait_time = 20 - int(minutes_since)
            print(f"   ⏳ Too soon - waiting {wait_time} more minutes", flush=True)
            print(f"{'='*70}\n", flush=True)
            return
        
        print(f"   ✅ Time OK - ready to publish", flush=True)
        
        # Get lead from queue
        print(f"\n📋 Queue Check:", flush=True)
        lead = get_oldest_queued_lead()
        
        if not lead:
            print(f"   📭 Queue empty - nothing to process", flush=True)
            print(f"{'='*70}\n", flush=True)
            return
        
        # Process the lead
        print(f"\n{'='*70}", flush=True)
        print(f"🚀 PROCESSING LEAD #{lead['id']}", flush=True)
        print(f"📰 {lead.get('title', 'Untitled')[:80]}", flush=True)
        print(f"📁 Category: {lead.get('category', 'unknown')}", flush=True)
        print(f"{'='*70}", flush=True)
        
        # Create article using AI
        print(f"\n📝 Article Generation:", flush=True)
        article_data = create_complete_article(
            title=lead.get('title', 'Tech News Update'),
            category=lead.get('category', 'technology')
        )
        
        if not article_data or not article_data.get('content'):
            print(f"\n❌ Article generation failed - no content produced", flush=True)
            print(f"   Marking lead as processed anyway", flush=True)
            update_lead_status(lead['id'], "processed")
            print(f"{'='*70}\n", flush=True)
            return
        
        # Validate article quality
        word_count = len(article_data['content'].split())
        print(f"\n✅ Article Generated:", flush=True)
        print(f"   Words: {word_count}", flush=True)
        print(f"   Title: {article_data.get('title', 'N/A')[:80]}", flush=True)
        print(f"   Description: {article_data.get('short_description', 'N/A')[:100]}", flush=True)
        
        if word_count < 500:
            print(f"   ⚠️ Warning: Article is short ({word_count} words)", flush=True)
        
        # Publish to Directus
        print(f"\n📤 Publishing:", flush=True)
        success = publish_article_to_directus(
            title=article_data['title'],
            content=article_data['content'],
            short_description=article_data['short_description'],
            category=lead.get('category', 'technology'),
            source_url=lead.get('source_url', ''),
            featured_image=article_data.get('featured_image'),
            slug=article_data.get('slug')
        )
        
        # Update lead status
        update_lead_status(lead['id'], "processed")
        
        # Final status
        if success:
            print(f"\n✅ SUCCESS!", flush=True)
            print(f"   Published: {article_data['title'][:60]}...", flush=True)
            print(f"   Words: {word_count}", flush=True)
            print(f"   Lead #{lead['id']} → processed", flush=True)
        else:
            print(f"\n❌ FAILED!", flush=True)
            print(f"   Article generated but Directus publish failed", flush=True)
            print(f"   Lead #{lead['id']} → processed (to avoid retry)", flush=True)
        
        print(f"{'='*70}\n", flush=True)
        
    except KeyboardInterrupt:
        raise  # Pass through to main loop
    except Exception as e:
        print(f"\n❌ QUEUE PROCESSING ERROR!", flush=True)
        print(f"   {str(e)[:200]}", flush=True)
        print(f"\n   Full traceback:", flush=True)
        print(traceback.format_exc(), flush=True)
        print(f"{'='*70}\n", flush=True)


# ==================== MAIN LOOP ====================

def run_publisher():
    """
    Main service loop
    Checks queue every 5 minutes and publishes if conditions are met
    """
    print(f"\n{'='*70}", flush=True)
    print(f"⏰ SERVICE STARTED", flush=True)
    print(f"{'='*70}", flush=True)
    
    # API health check on startup
    print(f"\n📊 API Health Check:", flush=True)
    health = check_api_health()
    for service, status in health.items():
        icon = "✅" if status in ['healthy', 'configured'] else "❌"
        print(f"   {icon} {service.capitalize()}: {status}")
    print()
    
    print(f"ℹ️ Configuration:", flush=True)
    print(f"   📅 Publish interval: 20 minutes", flush=True)
    print(f"   🔄 Check interval: 5 minutes", flush=True)
    print(f"   🎯 Queue processing: FIFO (oldest first)", flush=True)
    print(f"{'='*70}\n", flush=True)
    
    consecutive_errors = 0
    max_consecutive_errors = 5
    
    while True:
        try:
            process_queue()
            consecutive_errors = 0  # Reset on success
            
        except KeyboardInterrupt:
            print(f"\n{'='*70}", flush=True)
            print("👋 SHUTDOWN REQUESTED")
            print(f"{'='*70}", flush=True)
            print("Stopping publisher service...")
            break
            
        except Exception as e:
            consecutive_errors += 1
            print(f"\n{'❌'*35}", flush=True)
            print(f"CRITICAL ERROR #{consecutive_errors}/{max_consecutive_errors}", flush=True)
            print(f"{'❌'*35}", flush=True)
            print(f"{str(e)[:300]}", flush=True)
            print(f"\nFull traceback:", flush=True)
            print(traceback.format_exc(), flush=True)
            print(f"{'❌'*35}\n", flush=True)
            
            if consecutive_errors >= max_consecutive_errors:
                print(f"\n{'🚨'*35}", flush=True)
                print(f"TOO MANY CONSECUTIVE ERRORS - STOPPING SERVICE")
                print(f"{'🚨'*35}\n", flush=True)
                break
        
        # Sleep between checks
        sleep_seconds = 300  # 5 minutes
        print(f"💤 Sleeping {sleep_seconds//60} minutes until next check...\n", flush=True)
        time.sleep(sleep_seconds)


# ==================== ENTRY POINT ====================

if __name__ == "__main__":
    print("="*70)
    print("📰 PUBLISHER SERVICE v7.0 - ENHANCED")
    print("="*70)
    print("✓ Automated queue processing")
    print("✓ Gemini 2.0 deep research")
    print("✓ OpenAI GPT-4 humanized generation")
    print("✓ Quality validation")
    print("✓ Robust error handling")
    print("✓ FIFO queue management")
    print("✓ 20-minute publish interval")
    print("="*70)
    print()
    
    try:
        run_publisher()
    except Exception as e:
        print(f"\n{'🚨'*35}", flush=True)
        print("FATAL ERROR - SERVICE CRASHED")
        print(f"{'🚨'*35}", flush=True)
        print(f"{str(e)}", flush=True)
        print(f"\nFull traceback:", flush=True)
        print(traceback.format_exc(), flush=True)
        print(f"{'🚨'*35}\n", flush=True)
        exit(1)