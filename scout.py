"""
Smart Category-Filtered Scout v6.0
Only processes articles matching featured categories
Optimized for Google News
"""
import feedparser
import time
from datetime import datetime
from difflib import SequenceMatcher
from bs4 import BeautifulSoup
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from config import (
    SLACK_BOT_TOKEN, SLACK_CHANNEL, RSS_FEEDS,
    get_domain_name, directus_request
)


slack = WebClient(token=SLACK_BOT_TOKEN)


# ==================== FEATURED CATEGORIES (Google News Optimized) ====================

FEATURED_CATEGORIES = {
    # TIER 1 - PRIMARY CATEGORIES (Highest Priority)
    'smartphones': {
        'name': 'Smartphones',
        'emoji': '📱',
        'priority': 1,
        'keywords': [
            'iphone', 'android', 'samsung', 'pixel', 'oneplus', 'xiaomi',
            'oppo', 'vivo', 'realme', 'nothing phone', 'galaxy', 'smartphone',
            'mobile phone', 'phone launch', 'smartphone launch', '5g phone'
        ]
    },
    
    'phone-reviews': {
        'name': 'Phone Reviews',
        'emoji': '⭐',
        'priority': 1,
        'keywords': [
            'phone review', 'smartphone review', 'iphone review', 'android review',
            'hands-on', 'first impressions', 'unboxing', 'camera test',
            'performance test', 'battery test', 'full review', 'device review'
        ]
    },
    
    'laptops-pcs': {
        'name': 'Laptops & PCs',
        'emoji': '💻',
        'priority': 1,
        'keywords': [
            'macbook', 'laptop', 'notebook', 'gaming laptop', 'ultrabook',
            'chromebook', 'surface', 'thinkpad', 'dell xps', 'pc',
            'desktop', 'laptop review', 'laptop launch'
        ]
    },
    
    'ai-software': {
        'name': 'AI & Software',
        'emoji': '🤖',
        'priority': 1,
        'keywords': [
            'chatgpt', 'gemini', 'artificial intelligence', 'ai', 'machine learning',
            'copilot', 'bard', 'claude', 'openai', 'google ai',
            'software update', 'app update', 'ios update', 'android update',
            'windows update', 'macos'
        ]
    },
    
    'gaming': {
        'name': 'Gaming',
        'emoji': '🎮',
        'priority': 1,
        'keywords': [
            'playstation', 'ps5', 'xbox', 'nintendo', 'switch', 'gaming',
            'game release', 'esports', 'console', 'gaming pc', 'gpu',
            'rtx', 'radeon', 'steam', 'epic games', 'game review'
        ]
    },
    
    # TIER 2 - SECONDARY CATEGORIES (High Value)
    'buying-guides': {
        'name': 'Buying Guides',
        'emoji': '💰',
        'priority': 2,
        'keywords': [
            'best phone', 'best laptop', 'top 10', 'top 5', 'buying guide',
            'best under', 'budget phone', 'budget laptop', 'worth buying',
            'should you buy', 'recommendation', 'best for', 'which to buy',
            'affordable', 'cheap', 'value for money'
        ]
    },
    
    'comparisons': {
        'name': 'Comparisons',
        'emoji': '⚖️',
        'priority': 2,
        'keywords': [
            ' vs ', ' versus ', 'compared', 'comparison', 'which is better',
            ' or ', 'head to head', 'face-off', 'battle', 'showdown',
            'iphone vs', 'android vs', 'mac vs pc'
        ]
    },
    
    'wearables': {
        'name': 'Wearables & Accessories',
        'emoji': '⌚',
        'priority': 2,
        'keywords': [
            'apple watch', 'airpods', 'galaxy watch', 'smartwatch', 'earbuds',
            'wireless earbuds', 'fitness tracker', 'wearable', 'smart ring',
            'buds', 'headphones', 'earphones'
        ]
    },
    
    'tech-industry': {
        'name': 'Tech Industry News',
        'emoji': '🏢',
        'priority': 2,
        'keywords': [
            'acquisition', 'merger', 'layoff', 'ceo', 'earnings', 'revenue',
            'stock price', 'ipo', 'funding', 'investment', 'lawsuit',
            'regulation', 'antitrust', 'partnership', 'deal'
        ]
    },
    
    'privacy-security': {
        'name': 'Privacy & Security',
        'emoji': '🔒',
        'priority': 2,
        'keywords': [
            'privacy', 'security', 'breach', 'data breach', 'hack', 'hacked',
            'vulnerability', 'exploit', 'malware', 'ransomware', 'phishing',
            'data leak', 'stolen data', 'encryption', 'vpn', 'password',
            'two-factor', 'biometric', 'cybersecurity'
        ]
    },
    
    # TIER 3 - SUPPLEMENTARY CATEGORIES (Medium Value)
    'leaks-rumors': {
        'name': 'Leaks & Rumors',
        'emoji': '🔮',
        'priority': 3,
        'keywords': [
            'leak', 'leaked', 'rumor', 'rumored', 'upcoming', 'expected',
            'could launch', 'might release', 'could feature', 'reportedly',
            'sources say', 'insider', 'tipster', 'speculation'
        ]
    },
    
    'tech-events': {
        'name': 'Tech Events',
        'emoji': '🎪',
        'priority': 3,
        'keywords': [
            'ces', 'apple event', 'wwdc', 'google io', 'samsung unpacked',
            'microsoft build', 'conference', 'keynote', 'announcement event',
            'tech summit', 'developer conference'
        ]
    }
}


# ==================== SMART CATEGORY DETECTION ====================

def detect_category(title, description=''):
    """
    Detect category using keyword matching with priority scoring
    Returns (category_slug, category_name, emoji) or None if no match
    """
    text = (title + ' ' + description).lower()
    
    # Score each category
    category_scores = {}
    
    for slug, cat_data in FEATURED_CATEGORIES.items():
        score = 0
        matched_keywords = []
        
        for keyword in cat_data['keywords']:
            if keyword.lower() in text:
                # Multi-word keywords get higher weight
                keyword_weight = len(keyword.split())
                
                # Title matches worth more than description matches
                if keyword.lower() in title.lower():
                    keyword_weight *= 2
                
                # Priority tier multiplier
                priority_multiplier = 1.5 if cat_data['priority'] == 1 else 1.0
                
                score += keyword_weight * priority_multiplier
                matched_keywords.append(keyword)
        
        if score > 0:
            category_scores[slug] = {
                'score': score,
                'name': cat_data['name'],
                'emoji': cat_data['emoji'],
                'priority': cat_data['priority'],
                'matched': matched_keywords
            }
    
    # Return highest scoring category
    if category_scores:
        best_match = max(category_scores.items(), key=lambda x: x[1]['score'])
        slug, data = best_match
        
        # Require minimum score (at least 1 keyword match)
        if data['score'] >= 1:
            print(f"   ✓ {data['emoji']} {data['name']} (score: {data['score']:.1f}, matched: {', '.join(data['matched'][:2])})", flush=True)
            return slug, data['name'], data['emoji']
    
    print(f"   ✗ No category match - SKIPPING", flush=True)
    return None, None, None


# ==================== RSS EXTRACTION ====================

def extract_rss_data(entry):
    """Extract basic data from RSS entry"""
    title = entry.title if hasattr(entry, 'title') else ''
    
    # Get description
    description = ''
    if hasattr(entry, 'summary'):
        description = entry.summary
    elif hasattr(entry, 'description'):
        description = entry.description
    elif hasattr(entry, 'content') and isinstance(entry.content, list):
        description = entry.content[0].get('value', '')
    
    # Clean HTML
    if description:
        description = BeautifulSoup(description, "lxml").get_text(separator=" ").strip()
    
    link = entry.link if hasattr(entry, 'link') else ''
    
    return {
        'title': title,
        'description': description[:500],
        'link': link
    }


# ==================== DUPLICATE DETECTION ====================

def check_exact_duplicate(link):
    """Check if URL already exists"""
    result = directus_request('GET', f'/items/news_leads?filter[source_url][_eq]={link}&limit=1')
    if result and 'data' in result:
        return len(result['data']) > 0
    return False


def check_semantic_duplicate(title):
    """Check for similar titles"""
    result = directus_request('GET', '/items/news_leads?sort=-date_created&limit=100')
    
    if not result or 'data' not in result:
        return False
    
    for item in result['data']:
        existing_title = item.get('title', '')
        similarity = SequenceMatcher(None, title.lower(), existing_title.lower()).ratio()
        
        if similarity > 0.75:
            print(f"   🚫 {similarity:.0%} similar to: '{existing_title[:50]}...'", flush=True)
            return True
    
    return False


# ==================== DIRECTUS OPERATIONS ====================

def create_lead_in_directus(title, link, category):
    """Save lead to Directus (3 fields only: title, source_url, category)"""
    payload = {
        "title": title,
        "source_url": link,
        "category": category
    }
    
    result = directus_request('POST', '/items/news_leads', payload)
    
    if result and 'data' in result:
        lead_id = result['data']['id']
        print(f"   💾 Directus: #{lead_id}", flush=True)
        return lead_id
    
    print(f"   ❌ Directus save failed", flush=True)
    return None


# ==================== SLACK NOTIFICATION ====================

def post_to_slack(title, category_name, category_emoji, link, lead_id):
    """Post to Slack with category info"""
    source_name = get_domain_name(link)
    
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{category_emoji} *<{link}|{title}>*\n\n📁 *Category:* {category_name}\n🔗 *Source:* {source_name}"
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
            text=f"New: {title}",
            unfurl_links=False
        )
        print(f"   ✅ Slack sent", flush=True)
        return True
    except SlackApiError as e:
        print(f"   ❌ Slack error: {e.response.get('error')}", flush=True)
        return False


# ==================== MAIN PROCESSING ====================

def process_feed_entry(entry):
    """Process single RSS entry with category filtering"""
    try:
        data = extract_rss_data(entry)
        
        if not data['title'] or not data['link']:
            return None
        
        print(f"\n🔍 {data['title'][:70]}...", flush=True)
        
        # CATEGORY FILTER - Only proceed if matches featured category
        category_slug, category_name, category_emoji = detect_category(
            data['title'], 
            data['description']
        )
        
        if not category_slug:
            # NO CATEGORY MATCH - SKIP THIS ARTICLE
            return None
        
        # Check duplicates
        if check_exact_duplicate(data['link']):
            print(f"   🚫 Duplicate URL", flush=True)
            return None
        
        if check_semantic_duplicate(data['title']):
            return None
        
        # Save to Directus
        lead_id = create_lead_in_directus(
            title=data['title'],
            link=data['link'],
            category=category_slug
        )
        
        if not lead_id:
            return None
        
        # Notify Slack
        post_to_slack(
            title=data['title'],
            category_name=category_name,
            category_emoji=category_emoji,
            link=data['link'],
            lead_id=lead_id
        )
        
        return lead_id
        
    except Exception as e:
        print(f"   ❌ Error: {e}", flush=True)
        return None


# ==================== MAIN LOOP ====================

def run_scout():
    """Main scanning loop with statistics"""
    print(f"\n{'='*70}", flush=True)
    print(f"🚀 SCAN: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"{'='*70}\n", flush=True)
    
    total_scanned = 0
    total_created = 0
    skipped_no_category = 0
    category_stats = {slug: 0 for slug in FEATURED_CATEGORIES.keys()}
    
    for feed_url in RSS_FEEDS:
        try:
            source_name = get_domain_name(feed_url)
            print(f"\n📰 {source_name}", flush=True)
            print(f"{'─'*70}", flush=True)
            
            feed = feedparser.parse(feed_url)
            
            if not feed.entries:
                print(f"⚠️  No entries", flush=True)
                continue
            
            # Process top 5 entries per feed
            for entry in feed.entries[:5]:
                total_scanned += 1
                result = process_feed_entry(entry)
                
                if result:
                    total_created += 1
                    # Track which category it was
                    data = extract_rss_data(entry)
                    cat_slug, _, _ = detect_category(data['title'], data['description'])
                    if cat_slug:
                        category_stats[cat_slug] += 1
                    time.sleep(2)
                else:
                    # Check if it was skipped due to no category
                    data = extract_rss_data(entry)
                    cat_slug, _, _ = detect_category(data['title'], data['description'])
                    if not cat_slug:
                        skipped_no_category += 1
                    time.sleep(1)
            
            time.sleep(2)
            
        except Exception as e:
            print(f"❌ Feed error: {e}", flush=True)
            continue
    
    # Print statistics
    print(f"\n{'='*70}", flush=True)
    print(f"📊 SCAN STATISTICS", flush=True)
    print(f"{'='*70}", flush=True)
    print(f"Total Scanned:       {total_scanned}", flush=True)
    print(f"✅ Leads Created:    {total_created}", flush=True)
    print(f"🚫 Skipped (no cat): {skipped_no_category}", flush=True)
    print(f"📈 Match Rate:       {(total_created/total_scanned*100) if total_scanned > 0 else 0:.1f}%", flush=True)
    
    if any(count > 0 for count in category_stats.values()):
        print(f"\n📁 BREAKDOWN BY CATEGORY:", flush=True)
        for slug, count in sorted(category_stats.items(), key=lambda x: x[1], reverse=True):
            if count > 0:
                cat_data = FEATURED_CATEGORIES[slug]
                print(f"   {cat_data['emoji']} {cat_data['name']:<25} {count}", flush=True)
    
    print(f"{'='*70}\n", flush=True)


# ==================== ENTRY POINT ====================

if __name__ == "__main__":
    print("="*70)
    print("🤖 SMART SCOUT v6.0 - CATEGORY FILTERED (Google News Optimized)")
    print("="*70)
    print("✓ Only processes featured categories")
    print("✓ Smart keyword-based detection")
    print("✓ Priority scoring system")
    print("✓ Zero AI usage (fast & free)")
    print("="*70)
    print(f"\n📁 FEATURED CATEGORIES ({len(FEATURED_CATEGORIES)}):\n")
    
    for tier in [1, 2, 3]:
        tier_cats = {k: v for k, v in FEATURED_CATEGORIES.items() if v['priority'] == tier}
        if tier_cats:
            tier_name = "PRIMARY" if tier == 1 else "SECONDARY" if tier == 2 else "SUPPLEMENTARY"
            print(f"   TIER {tier} - {tier_name}:")
            for slug, data in tier_cats.items():
                print(f"      {data['emoji']} {data['name']}")
            print()
    
    print("="*70)
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
        
        print("💤 Sleeping 30 minutes...\n", flush=True)
        time.sleep(1800)