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
model = genai.GenerativeModel('gemini-3-pro-preview', generation_config={"response_mime_type": "application/json"})
slack = WebClient(token=SLACK_BOT_TOKEN)

# --- HELPER: ROBUST CONTENT EXTRACTION ---
def clean_html(html_text):
    """Removes HTML tags to give AI clean text"""
    try:
        if not html_text: return ""
        soup = BeautifulSoup(html_text, "lxml")
        return soup.get_text(separator=" ").strip()
    except:
        return str(html_text)

def get_best_content(entry):
    """
    Robustly extracts text from ANY RSS format.
    """
    content_text = ""

    # 1. Try 'content' (Atom/RSS 2.0 often puts full text here as a list)
    if hasattr(entry, 'content') and isinstance(entry.content, list):
        for item in entry.content:
            if item.get('value'):
                content_text += item.get('value') + " "
    
    # 2. If empty, try 'summary_detail' or 'summary'
    if not content_text.strip():
        if hasattr(entry, 'summary_detail') and hasattr(entry.summary_detail, 'value'):
            content_text = entry.summary_detail.value
        elif hasattr(entry, 'summary'):
            content_text = entry.summary
        elif hasattr(entry, 'description'):
            content_text = entry.description

    # 3. Clean the HTML
    clean_text = clean_html(content_text)

    # 4. FINAL FALLBACK: If text is too short (< 50 chars), USE TITLE + SUMMARY
    # This prevents the "Automated update" error by forcing context.
    if len(clean_text) < 50:
        return f"{entry.title}. {clean_text}"
    
    return clean_text

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
    url = f"{DIRECTUS_URL}/items/news_leads?sort=-date_created&limit=50"
    try:
        r = requests.get(url, headers=headers)
        if r.status_code == 200:
            existing_titles = [item['title'] for item in r.json()['data']]
            for existing in existing_titles:
                if SequenceMatcher(None, title.lower(), existing.lower()).ratio() > 0.65:
                    print(f"🚫 Duplicate Topic: '{title}'", flush=True)
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
    Strict Agent Prompt to ensure unique, non-generic summaries.
    """
    # Combine Title + Context to ensure AI never sees "empty" input
    full_input = f"HEADLINE: {original_title}\nCONTENT: {raw_context[:1500]}"
    
    prompt = f"""
    You are an elite Tech News Editor with 20+ years of experience.
    
    INPUT DATA:
    {full_input}

    YOUR GOAL:
    Turn this into a compelling, human-written news lead.
    
    STRICT RULES:
    1. TITLE: Must be punchy, under 65 chars. NO "Company announces..." boring syntax. Use active verbs.
    2. SUMMARY: 200-230 chars max. Focus on the "So What?". Why does this matter?
    3. TONE: Insider, smart, slightly casual. NOT robotic.
    4. CRITICAL: If the input content is short, INFER the importance based on the headline. NEVER output "Automated news update".

    Output JSON: {{ "title": "...", "summary": "..." }}
    """
    try:
        response = model.generate_content(prompt)
        text = response.text.strip()
        
        # Clean potential markdown wrapping
        if text.startswith("```json"):
            text = text.replace("```json", "").replace("```", "")
        
        return json.loads(text)
    except Exception as e:
        print(f"⚠️ AI Generation Error: {e}", flush=True)
        # Better Fallback: Use the original title instead of generic text
        return {"title": original_title[:65], "summary": f"{original_title} - Read full story for details."}

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
                    time.sleep(2) 
                    
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