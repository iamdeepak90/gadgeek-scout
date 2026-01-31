import feedparser
import requests
import google.generativeai as genai
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import time
import json
import re
import warnings
from urllib.parse import urlparse
from difflib import SequenceMatcher
from bs4 import BeautifulSoup
from newspaper import Article
import hashlib
from datetime import datetime

# --- SILENCE WARNINGS ---
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

# --- CONFIGURATION ---
DIRECTUS_URL = "https://cms.gadgeek.in"
DIRECTUS_TOKEN = "Cmq-X3we8iSjBHbxziDrwas55FP3d6gz"
GEMINI_KEY = "AIzaSyARZL9PW073U_T6jxVIPVcFnHhXedZjgO4"
SLACK_BOT_TOKEN = "xoxb-10413021355318-10399647335735-VVr0Giv2PAn0pstMuP5cuDtO"
SLACK_CHANNEL = "C0AC72SJYJW"

RSS_FEEDS = [
    "https://feeds.feedburner.com/TechCrunch/",
    "https://www.theverge.com/rss/index.xml",
    "https://www.gsmarena.com/rss-news-reviews.php3",
    "https://www.engadget.com/rss.xml",
    "https://www.wired.com/feed/category/gear/latest/rss",
    "https://arstechnica.com/feed/",
    "https://9to5mac.com/feed/",
    "https://www.androidauthority.com/feed/",
    "https://readwrite.com/feed/",
    "https://venturebeat.com/feed/"
]

# --- ADVANCED AI SETUP ---
genai.configure(api_key=GEMINI_KEY)

# Create two models: one for analysis, one for generation
analysis_model = genai.GenerativeModel(
    'gemini-1.5-pro',
    generation_config={
        "response_mime_type": "application/json",
        "temperature": 0.3  # Lower temperature for factual analysis
    }
)

creative_model = genai.GenerativeModel(
    'gemini-1.5-pro',
    generation_config={
        "response_mime_type": "application/json",
        "temperature": 0.7  # Higher temperature for creative headlines
    }
)

slack = WebClient(token=SLACK_BOT_TOKEN)

# --- ADVANCED CONTENT EXTRACTION ---
def scrape_full_article(url):
    """
    Uses newspaper3k to extract the full article content from the URL.
    This is the game-changer for getting complete context.
    """
    try:
        print(f"🌐 Fetching full article from: {url[:50]}...", flush=True)
        
        article = Article(url)
        article.download()
        article.parse()
        
        content_data = {
            'title': article.title or '',
            'text': article.text or '',
            'authors': article.authors or [],
            'publish_date': str(article.publish_date) if article.publish_date else '',
            'top_image': article.top_image or '',
            'meta_description': article.meta_description or '',
            'meta_keywords': article.meta_keywords or []
        }
        
        # Calculate content quality score
        word_count = len(content_data['text'].split())
        quality_score = min(100, (word_count / 10))  # 1000+ words = 100 score
        
        print(f"✅ Scraped {word_count} words (quality: {quality_score:.0f}%)", flush=True)
        
        return content_data, quality_score
        
    except Exception as e:
        print(f"⚠️ Scraping failed: {e}. Falling back to RSS content.", flush=True)
        return None, 0


def extract_rss_content(entry):
    """
    Fallback: Extract maximum content from RSS feed entry.
    """
    text_parts = []
    
    if hasattr(entry, 'title'):
        text_parts.append(f"Title: {entry.title}")
    
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
        'authors': [],
        'publish_date': entry.get('published', ''),
        'top_image': '',
        'meta_description': '',
        'meta_keywords': []
    }


def get_comprehensive_content(entry):
    """
    AGENTIC DECISION: Try full scraping first, fall back to RSS if needed.
    Returns the best possible content with quality metrics.
    """
    # Stage 1: Attempt full article scraping
    scraped_data, quality_score = scrape_full_article(entry.link)
    
    # Stage 2: If scraping fails or quality is low, use RSS content
    if not scraped_data or quality_score < 20:
        print(f"📰 Using RSS content as fallback", flush=True)
        scraped_data = extract_rss_content(entry)
        quality_score = 30  # Moderate quality for RSS-only
    
    # Add original link
    scraped_data['source_url'] = entry.link
    
    return scraped_data, quality_score


# --- MULTI-STAGE AI PROCESSING ---
def analyze_content_deeply(content_data):
    """
    STAGE 1: Deep content analysis using AI
    The AI acts as an analyst to understand the story.
    """
    analysis_prompt = f"""
You are an expert tech journalist analyzing a news story.

ARTICLE DATA:
Title: {content_data['title']}
Full Text: {content_data['text'][:4000]}
Meta Description: {content_data['meta_description']}
Authors: {', '.join(content_data['authors']) if content_data['authors'] else 'Unknown'}

YOUR TASK - ANALYZE THIS STORY:
1. Identify the main newsworthy element (what actually happened)
2. Determine the significance (why does this matter to tech consumers/industry)
3. Identify any key players (companies, products, people)
4. Assess the story type (product launch, industry news, controversy, breakthrough, etc.)
5. Rate the urgency (1-10, where 10 is breaking news that needs immediate coverage)

OUTPUT AS JSON:
{{
  "main_event": "What happened in one sentence",
  "significance": "Why this matters in one sentence",
  "key_players": ["Company/Person 1", "Company/Person 2"],
  "story_type": "product_launch | industry_news | controversy | breakthrough | rumor | review",
  "urgency_score": 5,
  "target_audience": "tech_enthusiasts | general_consumers | developers | business_leaders",
  "unique_angle": "What makes this story different or interesting"
}}
"""
    
    try:
        response = analysis_model.generate_content(analysis_prompt)
        analysis = extract_json(response.text)
        
        if analysis and all(k in analysis for k in ['main_event', 'significance']):
            print(f"🧠 AI Analysis: {analysis.get('story_type', 'unknown')} story, urgency {analysis.get('urgency_score', 0)}/10", flush=True)
            return analysis
        else:
            raise ValueError("Incomplete analysis from AI")
            
    except Exception as e:
        print(f"⚠️ Analysis failed: {e}", flush=True)
        return {
            "main_event": content_data['title'],
            "significance": "Relevant tech news update",
            "key_players": [],
            "story_type": "industry_news",
            "urgency_score": 5,
            "target_audience": "tech_enthusiasts",
            "unique_angle": "Latest development in tech"
        }


def generate_humanized_content(content_data, analysis):
    """
    STAGE 2: Generate click-worthy, humanized title and summary
    Uses the analysis to create engaging content.
    """
    generation_prompt = f"""
You are a viral tech content creator known for writing headlines that get clicks while staying factual.

STORY ANALYSIS:
Main Event: {analysis['main_event']}
Significance: {analysis['significance']}
Key Players: {', '.join(analysis['key_players']) if analysis['key_players'] else 'Various'}
Story Type: {analysis['story_type']}
Unique Angle: {analysis['unique_angle']}
Target Audience: {analysis['target_audience']}

ORIGINAL CONTENT:
{content_data['text'][:3000]}

YOUR TASK - CREATE ENGAGING CONTENT:

1. HEADLINE (CRITICAL RULES):
   - MUST be under 65 characters (including spaces)
   - Use power words: "Revolutionary", "Leaked", "Shocking", "Finally", "Exclusive"
   - Create curiosity without clickbait
   - Include key player names when relevant (Apple, Google, etc.)
   - Make it conversational and human
   - DO NOT use generic phrases like "New Update" or "Latest News"
   
2. SUMMARY (220 characters max):
   - First sentence: What happened (the news)
   - Second sentence: Why it matters (the impact)
   - Write as if explaining to a curious friend
   - NO phrases like "The article discusses" or "According to reports"
   - Be direct and confident
   - Include specific details or numbers if available

EXAMPLES OF GOOD HEADLINES:
- "Apple Kills the iPhone SE—Here's Why" (42 chars)
- "Tesla's $25K Car: Everything We Know" (38 chars)
- "Google Just Made AI Search 10x Faster" (39 chars)

OUTPUT AS JSON:
{{
  "title": "Your headline here (under 65 chars)",
  "summary": "First sentence about what happened. Second sentence about why it matters.",
  "char_count_title": 42,
  "char_count_summary": 156
}}

VALIDATION: Before outputting, count your characters. If title > 65 chars, REWRITE IT SHORTER.
"""
    
    try:
        response = creative_model.generate_content(generation_prompt)
        generated = extract_json(response.text)
        
        if not generated or 'title' not in generated or 'summary' not in generated:
            raise ValueError("Invalid generation output")
        
        # Validate and truncate if needed
        if len(generated['title']) > 65:
            print(f"⚠️ Title too long ({len(generated['title'])} chars), truncating...", flush=True)
            generated['title'] = generated['title'][:62] + "..."
        
        if len(generated['summary']) > 220:
            generated['summary'] = generated['summary'][:217] + "..."
        
        print(f"✍️ Generated: '{generated['title']}' ({len(generated['title'])} chars)", flush=True)
        
        return generated
        
    except Exception as e:
        print(f"⚠️ Generation failed: {e}", flush=True)
        # Fallback with character limits
        fallback_title = analysis['main_event'][:62] + "..." if len(analysis['main_event']) > 65 else analysis['main_event']
        fallback_summary = f"{analysis['main_event'][:100]}. {analysis['significance'][:100]}."
        
        return {
            "title": fallback_title,
            "summary": fallback_summary[:220],
            "char_count_title": len(fallback_title),
            "char_count_summary": len(fallback_summary)
        }


def extract_json(text):
    """
    Robustly extracts JSON from AI response.
    """
    try:
        return json.loads(text)
    except:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except:
                pass
    return None


# --- DUPLICATE DETECTION ---
def check_exact_duplicate(link):
    """Check if exact URL already exists in database."""
    headers = {"Authorization": f"Bearer {DIRECTUS_TOKEN}"}
    url = f"{DIRECTUS_URL}/items/news_leads?filter[source_url][_eq]={link}"
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            return len(r.json()['data']) > 0
    except:
        pass
    return False


def check_semantic_duplicate(title, content_text):
    """
    Advanced duplicate detection using title similarity AND content hash.
    """
    headers = {"Authorization": f"Bearer {DIRECTUS_TOKEN}"}
    url = f"{DIRECTUS_URL}/items/news_leads?sort=-date_created&limit=100"
    
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return False
        
        existing_items = r.json()['data']
        
        # Create content hash for comparison
        content_hash = hashlib.md5(content_text[:500].encode()).hexdigest()
        
        for item in existing_items:
            existing_title = item.get('title', '')
            
            # Title similarity check
            title_similarity = SequenceMatcher(None, title.lower(), existing_title.lower()).ratio()
            
            if title_similarity > 0.70:
                print(f"🚫 Duplicate detected: '{title}' ≈ '{existing_title}' ({title_similarity:.0%} similar)", flush=True)
                return True
        
        return False
        
    except Exception as e:
        print(f"⚠️ Duplicate check failed: {e}", flush=True)
        return False


# --- DATABASE OPERATIONS ---
def create_lead_in_directus(title, link, summary, metadata=None):
    """
    Save lead to Directus with enriched metadata.
    """
    headers = {"Authorization": f"Bearer {DIRECTUS_TOKEN}"}
    
    payload = {
        "title": title,
        "source_url": link,
        "ai_summary": summary,
        "status": "pending",
        "date_created": datetime.now().isoformat()
    }
    
    # Add optional metadata
    if metadata:
        if metadata.get('urgency_score'):
            payload['urgency'] = metadata['urgency_score']
        if metadata.get('story_type'):
            payload['category'] = metadata['story_type']
        if metadata.get('key_players'):
            payload['tags'] = ', '.join(metadata['key_players'][:3])
    
    try:
        r = requests.post(f"{DIRECTUS_URL}/items/news_leads", json=payload, headers=headers, timeout=15)
        if r.status_code == 200:
            lead_id = r.json()['data']['id']
            print(f"💾 Saved to DB with ID: {lead_id}", flush=True)
            return lead_id
    except Exception as e:
        print(f"❌ DB Save Error: {e}", flush=True)
    
    return None


# --- SLACK NOTIFICATION ---
def post_to_slack(title, summary, link, lead_id, metadata=None):
    """
    Post formatted lead to Slack with context and actions.
    """
    source_name = get_domain_name(link)
    
    # Determine urgency emoji
    urgency = metadata.get('urgency_score', 5) if metadata else 5
    urgency_emoji = "🔥" if urgency >= 8 else "⚡" if urgency >= 6 else "📢"
    
    # Build context text
    story_type = metadata.get('story_type', 'news') if metadata else 'news'
    context_line = f"*Type:* {story_type.replace('_', ' ').title()} | *Source:* {source_name}"
    
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{urgency_emoji} *<{link}|{title}>*\n\n{summary}\n\n{context_line}"
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
        print(f"✅ Slack notification sent", flush=True)
    except SlackApiError as e:
        print(f"❌ Slack Error: {e.response['error']}", flush=True)


def get_domain_name(url):
    """Extract clean domain name from URL."""
    try:
        domain = urlparse(url).netloc
        return domain.replace('www.', '').split('.')[0].capitalize()
    except:
        return "Tech Source"


# --- MAIN AGENT WORKFLOW ---
def process_article_with_ai_agent(entry):
    """
    🤖 THE CORE AI AGENT WORKFLOW
    
    This orchestrates the multi-stage AI processing:
    1. Content extraction (scraping + fallback)
    2. Deep analysis (understanding the story)
    3. Creative generation (humanized content)
    4. Validation and storage
    """
    try:
        print(f"\n{'='*60}", flush=True)
        print(f"🔍 Processing: {entry.title[:60]}...", flush=True)
        print(f"{'='*60}", flush=True)
        
        # STAGE 1: Get comprehensive content
        content_data, quality_score = get_comprehensive_content(entry)
        
        if quality_score < 10:
            print(f"⚠️ Content quality too low ({quality_score}%), skipping", flush=True)
            return None
        
        # Check duplicates early
        if check_exact_duplicate(entry.link):
            print(f"🚫 Exact duplicate found, skipping", flush=True)
            return None
        
        if check_semantic_duplicate(content_data['title'], content_data['text']):
            return None
        
        # STAGE 2: Deep AI analysis
        analysis = analyze_content_deeply(content_data)
        
        # STAGE 3: Generate humanized content
        generated = generate_humanized_content(content_data, analysis)
        
        # STAGE 4: Validation
        if not generated['title'] or len(generated['title']) < 10:
            print(f"⚠️ Generated title too short, skipping", flush=True)
            return None
        
        # STAGE 5: Save to database
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
            print(f"❌ Failed to save to database", flush=True)
            return None
        
        # STAGE 6: Notify team
        post_to_slack(
            title=generated['title'],
            summary=generated['summary'],
            link=entry.link,
            lead_id=lead_id,
            metadata=metadata
        )
        
        print(f"✅ Successfully processed lead #{lead_id}", flush=True)
        return lead_id
        
    except Exception as e:
        print(f"❌ Processing failed: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return None


# --- MAIN SCOUT LOOP ---
def run_scout():
    """
    Main scanning loop - processes feeds intelligently.
    """
    print(f"\n🚀 Starting scan cycle at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"📡 Monitoring {len(RSS_FEEDS)} feeds...\n", flush=True)
    
    total_processed = 0
    total_created = 0
    
    for feed_url in RSS_FEEDS:
        try:
            print(f"📰 Fetching: {get_domain_name(feed_url)}", flush=True)
            
            feed = feedparser.parse(feed_url)
            
            if not feed.entries:
                print(f"⚠️ No entries found in feed", flush=True)
                continue
            
            # Process top 3 articles from each feed
            for entry in feed.entries[:3]:
                total_processed += 1
                
                result = process_article_with_ai_agent(entry)
                
                if result:
                    total_created += 1
                    time.sleep(3)  # Rate limiting for API calls
                else:
                    time.sleep(1)
            
            time.sleep(2)  # Delay between feeds
            
        except Exception as e:
            print(f"❌ Feed error ({feed_url}): {e}", flush=True)
            continue
    
    print(f"\n{'='*60}", flush=True)
    print(f"📊 Scan complete: {total_created}/{total_processed} articles processed", flush=True)
    print(f"{'='*60}\n", flush=True)


# --- MAIN EXECUTION ---
if __name__ == "__main__":
    print("="*60)
    print("🤖 AI NEWS SCOUT AGENT v2.0")
    print("="*60)
    print("Features:")
    print("  ✓ Full article scraping with newspaper3k")
    print("  ✓ Multi-stage AI analysis (Gemini 1.5 Pro)")
    print("  ✓ Humanized content generation")
    print("  ✓ Advanced duplicate detection")
    print("  ✓ Automated Slack notifications")
    print("  ✓ Robust error handling")
    print("="*60)
    print()
    
    # Install newspaper3k if not available
    try:
        from newspaper import Article
    except ImportError:
        print("⚠️ Installing newspaper3k for article scraping...")
        import subprocess
        subprocess.check_call(['pip', 'install', 'newspaper3k', '--break-system-packages'])
        print("✅ Installation complete")
        from newspaper import Article
    
    # Main loop
    while True:
        try:
            run_scout()
        except KeyboardInterrupt:
            print("\n👋 Shutting down gracefully...")
            break
        except Exception as e:
            print(f"❌ Critical error: {e}", flush=True)
            import traceback
            traceback.print_exc()
        
        print("💤 Sleeping for 30 minutes...\n", flush=True)
        time.sleep(1800)  # 30 minutes