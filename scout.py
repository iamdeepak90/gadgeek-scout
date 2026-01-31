import feedparser
import requests
import google.generativeai as genai
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import time
import json
import warnings
from urllib.parse import urlparse
import os

# --- SILENCE WARNINGS ---
# Suppresses the "google.genai" deprecation warning to keep logs clean
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
    "https://www.gsmarena.com/rss-news-reviews.php3"
]

# --- SETUP ---
genai.configure(api_key=GEMINI_KEY)
# Using 'gemini-1.5-flash' for speed and cost efficiency
model = genai.GenerativeModel('gemini-1.5-flash', generation_config={"response_mime_type": "application/json"})
slack = WebClient(token=SLACK_BOT_TOKEN)

def get_domain_name(url):
    try:
        domain = urlparse(url).netloc
        return domain.replace('www.', '').split('.')[0].capitalize()
    except:
        return "News Source"

def check_duplicate(link):
    headers = {"Authorization": f"Bearer {DIRECTUS_TOKEN}"}
    # Using lowercase 'news_leads' as verified
    url = f"{DIRECTUS_URL}/items/news_leads?filter[source_url][_eq]={link}"
    try:
        r = requests.get(url, headers=headers)
        if r.status_code == 200:
            return len(r.json()['data']) > 0
    except Exception as e:
        print(f"⚠️ Network check failed: {e}", flush=True)
        return False
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
        else:
            print(f"❌ DB Save Error: {r.text}", flush=True)
            return None
    except Exception as e:
        print(f"❌ DB Connection Error: {e}", flush=True)
        return None

def process_with_ai(original_title):
    """
    Asks Gemini to rewrite the title and write a summary in one go.
    """
    prompt = f"""
    Analyze this news headline: "{original_title}"

    Task:
    1. Write a catchy 'title' under 65 characters.
    2. Write a 'summary' between 200-220 characters.

    Output strictly valid JSON: {{ "title": "...", "summary": "..." }}
    """
    try:
        response = model.generate_content(prompt)
        return json.loads(response.text)
    except Exception as e:
        print(f"⚠️ AI Error: {e}", flush=True)
        # Fallback if AI fails
        return {"title": original_title[:65], "summary": original_title}

def post_to_slack(ai_data, original_link, lead_id):
    source_name = get_domain_name(original_link)
    
    # Slack Block Kit Layout for better formatting
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
    print("📡 Scanning feeds...", flush=True)
    for feed in RSS_FEEDS:
        d = feedparser.parse(feed)
        # Check top 3 items
        for entry in d.entries[:3]:
            if not check_duplicate(entry.link):
                print(f"🆕 Found: {entry.title}", flush=True)
                
                # 1. AI Processing (Title & Summary)
                ai_data = process_with_ai(entry.title)
                
                # 2. Save to Directus
                lead_id = create_lead_in_directus(ai_data['title'], entry.link, ai_data['summary'])
                
                # 3. Notify Slack (Only if DB save succeeded)
                if lead_id:
                    post_to_slack(ai_data, entry.link, lead_id)

if __name__ == "__main__":
    print("🚀 Scout is starting...", flush=True)
    while True:
        try:
            run_scout()
        except Exception as e:
            print(f"❌ Critical Error in Main Loop: {e}", flush=True)
        
        print("💤 Sleeping for 30 minutes...", flush=True)
        time.sleep(1800)