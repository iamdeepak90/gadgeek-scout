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
from bs4 import BeautifulSoup # Required for cleaning RSS HTML

# --- SILENCE WARNINGS ---
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

# --- CONFIGURATION ---
DIRECTUS_URL = "https://admin.gadgeek.in"
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

# --- SETUP ---
genai.configure(api_key=GEMINI_KEY)

# Using 'gemini-1.5-pro' for maximum reasoning capability
model = genai.GenerativeModel('gemini-3-pro-preview', generation_config={"response_mime_type": "application/json"})
slack = WebClient(token=SLACK_BOT_TOKEN)

# --- HELPER: ROBUST CONTENT EXTRACTION ---
def clean_html(html_text):
    """Removes <div> <p> and other HTML noise from RSS feeds"""
    try:
        if not html_text: return ""
        soup = BeautifulSoup(html_text, "lxml")
        return soup.get_text(separator=" ").strip()
    except:
        return html_text

def get_best_content(entry):
    """
    Hunts for the best description text across all possible RSS fields.
    """
    # 1. Try 'content' (often contains full article)
    if hasattr(entry, 'content'):
        return clean_html(entry.content[0].value)
    
    # 2. Try 'summary_detail' or 'summary'
    if hasattr(entry, 'summary_detail'):
        return clean_html(entry.summary_detail.value)
    if hasattr(entry, 'summary'):
        return clean_html(entry.summary)
        
    # 3. Try 'description'
    if hasattr(entry, 'description'):
        return clean_html(entry.description)
        
    # 4. Fallback: If absolutely nothing, return Title so AI has *something* to work with
    return entry.title

def get_domain_name(url):
    try:
        domain = urlparse(url).netloc
        return domain.replace('www.', '').split('.')[0].capitalize()
    except:
        return "News Source"

def check_duplicate(link):
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
    headers = {"Authorization": f"Bearer {DIRECTUS_TOKEN}"}
    # Check last 50 items to see if we already covered this topic
    url = f"{DIRECTUS_URL}/items/news_leads?sort=-date_created&limit=50"
    try:
        r = requests.get(url, headers=headers)
        if r.status_code == 200:
            existing_titles = [item['title'] for item in r.json()['data']]
            for existing in existing_titles:
                # If titles match > 65%, assume it's the same news
                if SequenceMatcher(None, title.lower(), existing.lower()).ratio() > 0.65:
                    print(f"🚫 Duplicate Topic: '{title}' matches '{existing}'", flush=True)
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

def process_with_ai(original_title, raw_context):
    """
    The 'Agent' Prompt.
    """
    # Truncate context to 1000 chars to save tokens/speed, usually the lead is enough
    short_context = raw_context[:1000]
    
    prompt = f"""
    You are an elite Tech News Editor with 20+ years of experience in this field.
    
    INPUT DATA:
    Headline: "{original_title}"
    Snippet: "{short_context}"

    YOUR GOAL:
    Turn this into a compelling, human-written news lead.
    
    STRICT RULES:
    1. TITLE: Must be punchy, under 65 chars. NO "Company announces..." boring syntax. Use active verbs.
    2. SUMMARY: 200-230 chars max. Focus on the "So What?". Why does this matter?
    3. TONE: Insider, smart, slightly casual. NOT robotic.
    4. SAFETY: If the input is just a generic update or an ad, make the title descriptive based on the Headline.

    Output JSON: {{ "title": "...", "summary": "..." }}
    """
    try:
        response = model.generate_content(prompt)
        return json.loads(response.text)
    except Exception as e:
        print(f"⚠️ AI Generation Error: {e}", flush=True)
        return {"title": original_title[:65], "summary": "Automated news update found."}

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
        print(f"✅ Slack Sent: {ai_data['title']}", flush=True)
    except SlackApiError as e:
        print(f"❌ Slack Error: {e.response['error']}", flush=True)

def run_scout():
    print("📡 Scanning feeds...", flush=True)
    for feed in RSS_FEEDS:
        try:
            d = feedparser.parse(feed)
            for entry in d.entries[:2]: # Top 2 per feed
                
                # Check Duplicates
                if check_duplicate(entry.link): continue
                if check_semantic_duplicate(entry.title): continue

                print(f"🆕 Found: {entry.title}", flush=True)
                
                # EXTRACT ROBUST CONTEXT
                context = get_best_content(entry)
                
                # AI PROCESSING
                ai_data = process_with_ai(entry.title, context)
                
                # SAVE & NOTIFY
                lead_id = create_lead_in_directus(ai_data['title'], entry.link, ai_data['summary'])
                if lead_id:
                    post_to_slack(ai_data, entry.link, lead_id)
                    time.sleep(2) # Rate limit politeness
                    
        except Exception as e:
            print(f"⚠️ Feed Error ({feed}): {e}", flush=True)

if __name__ == "__main__":
    print("🚀 Scout (Agent) is starting...", flush=True)
    while True:
        try:
            run_scout()
        except Exception as e:
            print(f"❌ Critical Error: {e}", flush=True)
        
        print("💤 Sleeping for 30 minutes...", flush=True)
        time.sleep(1800)