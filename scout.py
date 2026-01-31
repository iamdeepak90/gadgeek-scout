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
import hashlib
from datetime import datetime

# --- SILENCE WARNINGS ---
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

# --- CONFIGURATION ---
DIRECTUS_URL = "https://admin.gadgeek.in"  # FIXED: Correct URL
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

# --- AI SETUP ---
genai.configure(api_key=GEMINI_KEY)

# Two models: analysis (factual) and generation (creative)
analysis_model = genai.GenerativeModel(
    'gemini-1.5-flash',
    generation_config={
        "response_mime_type": "application/json",
        "temperature": 0.3
    }
)

creative_model = genai.GenerativeModel(
    'gemini-1.5-flash',
    generation_config={
        "response_mime_type": "application/json",
        "temperature": 0.7
    }
)

slack = WebClient(token=SLACK_BOT_TOKEN)

# --- ADVANCED CONTENT EXTRACTION (BEAUTIFULSOUP-BASED) ---
def scrape_full_article(url):
    """
    Uses requests + BeautifulSoup to extract article content.
    No dependency on newspaper3k.
    """
    try:
        print(f"🌐 Fetching full article from: {url[:50]}...", flush=True)
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'lxml')
        
        # Extract title
        title = ''
        if soup.find('h1'):
            title = soup.find('h1').get_text().strip()
        elif soup.find('title'):
            title = soup.find('title').get_text().strip()
        
        # Extract main content - try multiple strategies
        content_text = ''
        
        # Strategy 1: Look for article tags
        article = soup.find('article')
        if article:
            for tag in article.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside', 'iframe']):
                tag.decompose()
            content_text = article.get_text(separator=' ', strip=True)
        
        # Strategy 2: Look for common content divs
        if not content_text or len(content_text) < 200:
            for class_name in ['content', 'article-body', 'post-content', 'entry-content', 
                              'article-content', 'story-body', 'article__body']:
                content_div = soup.find('div', class_=class_name)
                if content_div:
                    for tag in content_div.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside', 'iframe']):
                        tag.decompose()
                    content_text = content_div.get_text(separator=' ', strip=True)
                    if len(content_text) > 200:
                        break
        
        # Strategy 3: Look for main tag
        if not content_text or len(content_text) < 200:
            main = soup.find('main')
            if main:
                for tag in main.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside', 'iframe']):
                    tag.decompose()
                content_text = main.get_text(separator=' ', strip=True)
        
        # Strategy 4: Find all paragraphs (fallback)
        if not content_text or len(content_text) < 200:
            paragraphs = soup.find_all('p')
            content_text = ' '.join([p.get_text().strip() for p in paragraphs if len(p.get_text().strip()) > 30])
        
        # Extract metadata
        meta_desc = ''
        meta_tag = soup.find('meta', attrs={'name': 'description'}) or soup.find('meta', attrs={'property': 'og:description'})
        if meta_tag:
            meta_desc = meta_tag.get('content', '')
        
        # Extract author
        authors = []
        author_meta = soup.find('meta', attrs={'name': 'author'})
        if author_meta:
            authors.append(author_meta.get('content', ''))
        
        content_data = {
            'title': title,
            'text': content_text,
            'authors': authors,
            'publish_date': '',
            'meta_description': meta_desc,
            'meta_keywords': []
        }
        
        # Calculate content quality score
        word_count = len(content_data['text'].split())
        quality_score = min(100, (word_count / 10))
        
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
        'meta_description': '',
        'meta_keywords': []
    }


def get_comprehensive_content(entry):
    """
    AGENTIC DECISION: Try full scraping first, fall back to RSS if needed.
    """
    scraped_data, quality_score = scrape_full_article(entry.link)
    
    if not scraped_data or quality_score < 20:
        print(f"📰 Using RSS content as fallback", flush=True)
        scraped_data = extract_rss_content(entry)
        quality_score = 30
    
    scraped_data['source_url'] = entry.link
    return scraped_data, quality_score


# --- MULTI-STAGE AI PROCESSING ---
def analyze_content_deeply(content_data):
    """
    STAGE 1: Deep content analysis
    """
    analysis_prompt = f"""
You are an expert tech journalist analyzing a news story.

ARTICLE DATA:
Title: {content_data['title']}
Full Text: {content_data['text'][:4000]}
Meta Description: {content_data['meta_description']}

YOUR TASK - ANALYZE THIS STORY:
1. Identify the main newsworthy element (what actually happened)
2. Determine the significance (why does this matter)
3. Identify key players (companies, products, people)
4. Assess story type (product_launch, industry_news, controversy, breakthrough, rumor, review)
5. Rate urgency (1-10, where 10 is breaking news)

OUTPUT AS JSON:
{{
  "main_event": "What happened in one sentence",
  "significance": "Why this matters in one sentence",
  "key_players": ["Company/Person 1", "Company/Person 2"],
  "story_type": "product_launch",
  "urgency_score": 5,
  "target_audience": "tech_enthusiasts",
  "unique_angle": "What makes this story interesting"
}}
"""
    
    try:
        response = analysis_model.generate_content(analysis_prompt)
        analysis = extract_json(response.text)
        
        if analysis and all(k in analysis for k in ['main_event', 'significance']):
            print(f"🧠 AI Analysis: {analysis.get('story_type', 'unknown')} story, urgency {analysis.get('urgency_score', 0)}/10", flush=True)
            return analysis
        else:
            raise ValueError("Incomplete analysis")
            
    except Exception as e:
        print(f"⚠️ Analysis failed: {e}", flush=True)
        return {
            "main_event": content_data['title'],
            "significance": "Relevant tech news update",
            "key_players": [],
            "story_type": "industry_news",
            "urgency_score": 5,
            "target_audience": "tech_enthusiasts",
            "unique_angle": "Latest development"
        }


def generate_humanized_content(content_data, analysis):
    """
    STAGE 2: Generate engaging title and summary
    """
    generation_prompt = f"""
You are a viral tech content creator.

STORY ANALYSIS:
Main Event: {analysis['main_event']}
Significance: {analysis['significance']}
Key Players: {', '.join(analysis['key_players']) if analysis['key_players'] else 'Various'}
Story Type: {analysis['story_type']}
Unique Angle: {analysis['unique_angle']}

ORIGINAL CONTENT:
{content_data['text'][:3000]}

CREATE ENGAGING CONTENT:

1. HEADLINE (CRITICAL):
   - MUST be under 65 characters (including spaces)
   - Use power words: "Leaked", "Finally", "Shocking", "Revolutionary"
   - Include key names (Apple, Google, Tesla, etc.)
   - Create curiosity without clickbait
   - Conversational tone
   - NO generic phrases like "New Update" or "Latest News"
   
2. SUMMARY (220 chars max):
   - Sentence 1: What happened (the news)
   - Sentence 2: Why it matters (the impact)
   - Direct and confident
   - NO "The article discusses" or "According to reports"
   - Include specific details/numbers

EXAMPLES:
- "Apple Kills the iPhone SE—Here's Why" (42 chars)
- "Tesla's $25K Car: Everything We Know" (38 chars)
- "Google AI Now Reads 1 Million Tokens" (38 chars)

OUTPUT JSON:
{{
  "title": "Your headline (under 65 chars)",
  "summary": "First sentence. Second sentence.",
  "char_count_title": 42,
  "char_count_summary": 85
}}

VALIDATE: Count characters before output. Title > 65? REWRITE SHORTER.
"""
    
    try:
        response = creative_model.generate_content(generation_prompt)
        generated = extract_json(response.text)
        
        if not generated or 'title' not in generated or 'summary' not in generated:
            raise ValueError("Invalid output")
        
        # Validate and truncate
        if len(generated['title']) > 65:
            print(f"⚠️ Title too long ({len(generated['title'])} chars), truncating", flush=True)
            generated['title'] = generated['title'][:62] + "..."
        
        if len(generated['summary']) > 220:
            generated['summary'] = generated['summary'][:217] + "..."
        
        print(f"✍️ Generated: '{generated['title']}' ({len(generated['title'])} chars)", flush=True)
        
        return generated
        
    except Exception as e:
        print(f"⚠️ Generation failed: {e}", flush=True)
        fallback_title = analysis['main_event'][:62] + "..." if len(analysis['main_event']) > 65 else analysis['main_event']
        fallback_summary = f"{analysis['main_event'][:100]}. {analysis['significance'][:100]}."
        
        return {
            "title": fallback_title,
            "summary": fallback_summary[:220]
        }


def extract_json(text):
    """Extract JSON from AI response"""
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
    """Check if URL exists in database"""
    headers = {"Authorization": f"Bearer {DIRECTUS_TOKEN}"}
    url = f"{DIRECTUS_URL}/items/news_leads?filter[source_url][_eq]={link}"
    
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            return len(r.json()['data']) > 0
        elif r.status_code == 401:
            print(f"⚠️ Auth failed. Check DIRECTUS_TOKEN", flush=True)
    except Exception as e:
        print(f"⚠️ Duplicate check failed: {str(e)[:100]}", flush=True)
    
    return False


def check_semantic_duplicate(title, content_text):
    """Advanced duplicate detection using similarity"""
    headers = {"Authorization": f"Bearer {DIRECTUS_TOKEN}"}
    url = f"{DIRECTUS_URL}/items/news_leads?sort=-date_created&limit=100"
    
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return False
        
        existing_items = r.json()['data']
        
        for item in existing_items:
            existing_title = item.get('title', '')
            title_similarity = SequenceMatcher(None, title.lower(), existing_title.lower()).ratio()
            
            if title_similarity > 0.70:
                print(f"🚫 Duplicate: '{title}' ≈ '{existing_title}' ({title_similarity:.0%})", flush=True)
                return True
        
        return False
        
    except Exception as e:
        print(f"⚠️ Duplicate check failed: {str(e)[:100]}", flush=True)
        return False


# --- DATABASE OPERATIONS ---
def create_lead_in_directus(title, link, summary, metadata=None):
    """Save lead to Directus"""
    headers = {"Authorization": f"Bearer {DIRECTUS_TOKEN}"}
    
    payload = {
        "title": title,
        "source_url": link,
        "ai_summary": summary,
        "status": "pending"
    }
    
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
            print(f"💾 Saved to DB: #{lead_id}", flush=True)
            return lead_id
        else:
            print(f"❌ DB returned status {r.status_code}: {r.text[:200]}", flush=True)
    except Exception as e:
        print(f"❌ DB Save Error: {str(e)[:150]}", flush=True)
    
    return None


# --- SLACK NOTIFICATION ---
def post_to_slack(title, summary, link, lead_id, metadata=None):
    """Post to Slack"""
    source_name = get_domain_name(link)
    
    urgency = metadata.get('urgency_score', 5) if metadata else 5
    urgency_emoji = "🔥" if urgency >= 8 else "⚡" if urgency >= 6 else "📢"
    
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
        print(f"✅ Slack sent", flush=True)
    except SlackApiError as e:
        print(f"❌ Slack Error: {e.response['error']}", flush=True)


def get_domain_name(url):
    """Extract domain name"""
    try:
        domain = urlparse(url).netloc
        return domain.replace('www.', '').split('.')[0].capitalize()
    except:
        return "Tech Source"


# --- MAIN WORKFLOW ---
def process_article_with_ai_agent(entry):
    """
    Core AI agent workflow
    """
    try:
        print(f"\n{'='*60}", flush=True)
        print(f"🔍 Processing: {entry.title[:60]}...", flush=True)
        print(f"{'='*60}", flush=True)
        
        # Get content
        content_data, quality_score = get_comprehensive_content(entry)
        
        if quality_score < 10:
            print(f"⚠️ Quality too low ({quality_score}%), skipping", flush=True)
            return None
        
        # Check duplicates
        if check_exact_duplicate(entry.link):
            print(f"🚫 Exact duplicate, skipping", flush=True)
            return None
        
        if check_semantic_duplicate(content_data['title'], content_data['text']):
            return None
        
        # AI analysis
        analysis = analyze_content_deeply(content_data)
        
        # AI generation
        generated = generate_humanized_content(content_data, analysis)
        
        # Validation
        if not generated['title'] or len(generated['title']) < 10:
            print(f"⚠️ Title too short, skipping", flush=True)
            return None
        
        # Save
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
            print(f"❌ Failed to save", flush=True)
            return None
        
        # Notify
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
        print(f"❌ Processing failed: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return None


# --- MAIN LOOP ---
def run_scout():
    """Main scanning loop"""
    print(f"\n🚀 Scan cycle: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"📡 Monitoring {len(RSS_FEEDS)} feeds\n", flush=True)
    
    total_processed = 0
    total_created = 0
    
    for feed_url in RSS_FEEDS:
        try:
            print(f"📰 Fetching: {get_domain_name(feed_url)}", flush=True)
            
            feed = feedparser.parse(feed_url)
            
            if not feed.entries:
                print(f"⚠️ No entries", flush=True)
                continue
            
            for entry in feed.entries[:3]:
                total_processed += 1
                result = process_article_with_ai_agent(entry)
                
                if result:
                    total_created += 1
                    time.sleep(3)
                else:
                    time.sleep(1)
            
            time.sleep(2)
            
        except Exception as e:
            print(f"❌ Feed error: {e}", flush=True)
            continue
    
    print(f"\n{'='*60}", flush=True)
    print(f"📊 Complete: {total_created}/{total_processed} leads created", flush=True)
    print(f"{'='*60}\n", flush=True)


# --- START ---
if __name__ == "__main__":
    print("="*60)
    print("🤖 AI NEWS SCOUT v2.2 - PRODUCTION READY")
    print("="*60)
    print("✓ BeautifulSoup scraping (no newspaper3k)")
    print("✓ Multi-stage AI (Gemini 1.5 Flash)")
    print("✓ Humanized content generation")
    print("✓ Character limit enforcement")
    print("✓ Advanced duplicate detection")
    print("✓ Slack notifications")
    print("="*60)
    print()
    
    while True:
        try:
            run_scout()
        except KeyboardInterrupt:
            print("\n👋 Shutting down...")
            break
        except Exception as e:
            print(f"❌ Critical: {e}", flush=True)
            import traceback
            traceback.print_exc()
        
        print("💤 Sleep 30 min\n", flush=True)
        time.sleep(1800)