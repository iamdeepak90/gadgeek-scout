import feedparser
import requests
import google.generativeai as genai
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import time
import sys

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
model = genai.GenerativeModel('gemini-1.5-flash')
slack = WebClient(token=SLACK_BOT_TOKEN)

def check_duplicate(link):
    headers = {"Authorization": f"Bearer {DIRECTUS_TOKEN}"}
    r = requests.get(f"{DIRECTUS_URL}/items/news_leads?filter[source_url][_eq]={link}", headers=headers)
    return len(r.json()['data']) > 0

def create_lead_in_directus(title, link, summary):
    headers = {"Authorization": f"Bearer {DIRECTUS_TOKEN}"}
    payload = {
        "title": title,
        "source_url": link,
        "ai_summary": summary,
        "status": "pending"
    }
    r = requests.post(f"{DIRECTUS_URL}/items/news_leads", json=payload, headers=headers)
    return r.json()['data']['id']

def post_to_slack(title, summary, lead_id):
    # This JSON defines the UI with Green/Red buttons
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*🚨 New Lead:* <{title}|{title}>"}
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"_{summary}_"}
        },
        {
            "type": "actions",
            "block_id": f"action_block_{lead_id}",
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
        }
    ]
    
    try:
        slack.chat_postMessage(channel=SLACK_CHANNEL, blocks=blocks, text=f"New Lead: {title}")
    except SlackApiError as e:
        print(f"Error sending to Slack: {e.response['error']}")

def run_scout():
    print("Scanning...")
    for feed in RSS_FEEDS:
        d = feedparser.parse(feed)
        for entry in d.entries[:3]:
            if not check_duplicate(entry.link):
                print(f"New: {entry.title}")
                
                # 1. Summarize
                try:
                    res = model.generate_content(f"Summarize in 15 words: {entry.title}")
                    summary = res.text.strip()
                except:
                    summary = entry.title
                
                # 2. Save DB
                lead_id = create_lead_in_directus(entry.title, entry.link, summary)
                
                # 3. Notify Slack
                post_to_slack(entry.title, summary, lead_id)

if __name__ == "__main__":
    print("🚀 Scout is starting...", flush=True)
    
    while True:
        try:
            run_scout()
        except Exception as e:
            # If it crashes, print error but don't kill the container
            print(f"❌ Error: {e}", flush=True)
        
        # Calculate next run (e.g., run every 30 mins)
        print("💤 Sleeping for 30 minutes...", flush=True)
        time.sleep(1800)