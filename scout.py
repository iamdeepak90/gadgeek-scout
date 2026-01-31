import feedparser
import requests
import google.generativeai as genai
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import time
import json
import warnings
from urllib.parse import urlparse
from difflib import SequenceMatcher

# --- SILENCE WARNINGS ---
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

# --- CONFIGURATION ---
DIRECTUS_URL = "https://admin.gadgeek.in"
DIRECTUS_TOKEN = "Cmq-X3we8iSjBHbxziDrwas55FP3d6gz"
GEMINI_KEY = "AIzaSyARZL9PW073U_T6jxVIPVcFnHhXedZjgO4"
SLACK_BOT_TOKEN = "xoxb-10413021355318-10399647335735-VVr0Giv2PAn0pstMuP5cuDtO"
SLACK_CHANNEL = "C0AC72SJYJW" 

# --- EXPANDED FEED LIST ---
RSS_FEEDS = [
    "https://feeds.feedburner.com/TechCrunch/",
    "https://www.theverge.com/rss/index.xml",
    "https://www.gsmarena.com/rss-news-reviews.php3",
    "https://www.engadget.com/rss.xml",
    "https://www.wired.com/feed/category/gear/latest/rss",
    "https://arstechnica.com/feed/",
    "https://9to5mac.com/feed/",
    "https://www.androidauthority.com/feed/",
    "https://mashable.com/feeds/rss/tech",
    "https://gizmodo.com/rss",
    "https://readwrite.com/feed/",
    "https://venturebeat.com/feed/"
]

# --- SETUP ---
genai.configure(api_key=GEMINI_KEY)

# UPGRADE: Using 'gemini-1.5-pro' for maximum humanization quality
model = genai.GenerativeModel('gemini-1.5-pro', generation_config={"response_mime_type": "application/json"})
slack = WebClient(token=SLACK_BOT_TOKEN)

def get_domain_name(url):
    try:
        domain = urlparse(url).netloc
        return domain.replace('www.', '').split('.')[0].capitalize()
    except:
        return "News Source"

def check_duplicate(link):
    """Checks if specific URL exists"""
    headers = {"Authorization": f"Bearer {DIRECTUS_TOKEN}"}
    url = f"{DIRECTUS_URL}/items/news_leads?filter[source_url][_eq]={link}"
    try:
        r = requests.get(url, headers=headers)
        if r.status_code == 200:
            return len(r.json()['data']) > 0
    except:
        return False
    return False

def check_semantic_duplicate(title):
    """
    Advanced: Checks if we have a similar title already (e.g. 'iPhone 17 Leaks' vs 'Apple iPhone 17 Rumors')
    This prevents spamming the same story from different sources.
    """
    headers = {"Authorization": f"Bearer {DIRECTUS_TOKEN}"}
    # Search for leads created in last 24 hours (simplified logic: just check last 50 items)
    url = f"{DIRECTUS_URL}/items/news_leads?sort=-date_created&limit=50"
    try:
        r = requests.get(url, headers=headers)
        if r.status_code == 200:
            existing_titles = [item['title'] for item in r.json()['data']]
            for existing in existing_titles:
                # Similarity ratio > 0.6 means it's likely the same story
                if SequenceMatcher(None, title.lower(), existing.lower()).ratio() > 0.6:
                    print(f"🚫 Skipping Semantic Duplicate: '{title}' is too similar to '{existing}'", flush=True)
                    return True
    except:
        pass
    return False

def create_lead_in_directus(title, link, summary):
    headers = {"Authorization": f"Bearer {DIRECTUS_TOKEN}"}
    payload = {
        "title": title,
        "source_url": link,
        "ai_summary": summary,
        "status": "pending"
    }
    try:
        r = requests.post(f"{DIRECTUS_URL}/items/news_leads", json=payload, headers=headers)
        if r.status_code == 200:
            return r.json()['data']['id']
    except Exception as e:
        print(f"❌ DB Save Error: {e}", flush=True)
    return None

def process_with_ai(original_title, original_summary_from_rss):
    """
    Uses Gemini 1.5 Pro to completely rewrite the angle.
    """
    prompt = f"""
    You are a senior tech editor known for witty, insider takes.
    
    Original Headline: "{original_title}"
    Context/Snippet: "{original_summary_from_rss}"

    Your Task:
    1. WRITE A NEW TITLE: Create a new, click-worthy title (under 65 chars). Do NOT use the exact words from the original. Make it sound like a unique scoop.
    2. WRITE A SUMMARY: Write a 2-sentence summary (200 chars max) that explains WHY this matters. Use an active, human voice. Avoid "The article discusses..." or "This news is about...". Just tell the story.

    Output JSON: {{ "title": "...", "summary": "..." }}
    """
    try:
        response = model.generate_content(prompt)
        return json.loads(response.text)
    except Exception as e:
        print(f"⚠️ AI Error: {e}", flush=True)
        return {"title": original_title[:65], "summary": "News update."}

def post_to_slack(ai_data, original_link, lead_id):
    source_name = get_domain_name(original_link)
    
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Title:* <{original_link}|{ai_data['title']}>\n*Summary:* {ai_data['summary']}\n*Source:* {source_name}"
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
        slack.chat_postMessage(channel=SLACK_CHANNEL, blocks=blocks, text=f"New Lead: {ai_data['title']}")
        print(f"✅ Sent to Slack: {ai_data['title']}", flush=True)
    except SlackApiError as e:
        print(f"❌ Slack Error: {e.response['error']}", flush=True)

def run_scout():
    print("📡 Scanning extended feed list...", flush=True)
    for feed in RSS_FEEDS:
        try:
            d = feedparser.parse(feed)
            # Check top 2 items from each feed to avoid flood
            for entry in d.entries[:2]:
                
                # Check 1: Exact URL Duplicate
                if check_duplicate(entry.link):
                    continue

                # Check 2: Semantic Title Duplicate (Avoids "iPhone 17" from 5 different sites)
                if check_semantic_duplicate(entry.title):
                    continue

                print(f"🆕 Processing: {entry.title}", flush=True)
                
                # Get RSS Summary if available, else empty
                rss_summary = getattr(entry, 'summary', '')
                
                # AI Rewrite
                ai_data = process_with_ai(entry.title, rss_summary)
                
                # Save & Notify
                lead_id = create_lead_in_directus(ai_data['title'], entry.link, ai_data['summary'])
                if lead_id:
                    post_to_slack(ai_data, entry.link, lead_id)
                    # Small sleep to be nice to Gemini API
                    time.sleep(2)
        except Exception as e:
            print(f"⚠️ Feed Error ({feed}): {e}", flush=True)

if __name__ == "__main__":
    print("🚀 Scout Pro is starting...", flush=True)
    while True:
        try:
            run_scout()
        except Exception as e:
            print(f"❌ Critical Error in Main Loop: {e}", flush=True)
        
        print("💤 Sleeping for 30 minutes...", flush=True)
        time.sleep(1800)