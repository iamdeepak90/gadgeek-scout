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
DIRECTUS_URL = "[https://cms.gadgeek.in](https://cms.gadgeek.in)"
# PASTE KEYS HERE
DIRECTUS_TOKEN = "Cmq-X3we8iSjBHbxziDrwas55FP3d6gz"
GEMINI_KEY = "AIzaSyARZL9PW073U_T6jxVIPVcFnHhXedZjgO4"
SLACK_BOT_TOKEN = "xoxb-10413021355318-10399647335735-VVr0Giv2PAn0pstMuP5cuDtO"
SLACK_CHANNEL = "C0AC72SJYJW"  

RSS_FEEDS = [
    "[https://feeds.feedburner.com/TechCrunch/](https://feeds.feedburner.com/TechCrunch/)",
    "[https://www.theverge.com/rss/index.xml](https://www.theverge.com/rss/index.xml)",
    "[https://www.gsmarena.com/rss-news-reviews.php3](https://www.gsmarena.com/rss-news-reviews.php3)",
    "[https://www.engadget.com/rss.xml](https://www.engadget.com/rss.xml)",
    "[https://www.wired.com/feed/category/gear/latest/rss](https://www.wired.com/feed/category/gear/latest/rss)",
    "[https://arstechnica.com/feed/](https://arstechnica.com/feed/)",
    "[https://9to5mac.com/feed/](https://9to5mac.com/feed/)",
    "[https://www.androidauthority.com/feed/](https://www.androidauthority.com/feed/)",
    "[https://readwrite.com/feed/](https://readwrite.com/feed/)",
    "[https://venturebeat.com/feed/](https://venturebeat.com/feed/)"
]

# --- SETUP ---
genai.configure(api_key=GEMINI_KEY)
model = genai.GenerativeModel('gemini-1.5-pro', generation_config={"response_mime_type": "application/json"})
slack = WebClient(token=SLACK_BOT_TOKEN)

# --- HELPER: TEXT CLEANING ---
def clean_html(html_text):
    """Removes HTML tags and cleans up whitespace."""
    try:
        if not html_text: return ""
        soup = BeautifulSoup(html_text, "lxml")
        return soup.get_text(separator=" ").strip()
    except:
        return str(html_text)

def extract_json(text):
    """
    Robustly extracts JSON object from AI response, ignoring Markdown or preamble text.
    """
    try:
        # 1. Try direct parsing
        return json.loads(text)
    except:
        # 2. Use Regex to find the first { ... } block
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except:
                pass
    return None

def get_best_content(entry):
    """
    Aggregates all possible text fields to give AI the maximum context.
    """
    text_parts = []
    
    # Title is always useful context
    text_parts.append(f"Title: {entry.title}")

    # Check 'content' (Atom/RSS 2.0)
    if hasattr(entry, 'content') and isinstance(entry.content, list):
        for item in entry.content:
            text_parts.append(item.get('value', ''))
    
    # Check 'summary' or 'description'
    if hasattr(entry, 'summary'):
        text_parts.append(entry.summary)
    if hasattr(entry, 'description'):
        text_parts.append(entry.description)

    # Join and clean
    full_text = " ".join(text_parts)
    clean_text = clean_html(full_text)
    
    # If text is suspiciously short, return strictly the title with a flag
    if len(clean_text) < 50:
        return f"Headline: {entry.title}"
    
    return clean_text[:2000] # Cap at 2000 chars to save tokens

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

def process_with_ai(raw_text):
    """
    High-Level Agent Logic
    """
    prompt = f"""
    You are an expert Senior Tech Editor. 
    
    INPUT CONTENT:
    {raw_text}

    YOUR TASK:
    Analyze the input above. If the input is short, use your internal knowledge about the topic to fill in the context.
    
    OUTPUT REQUIREMENTS (JSON ONLY):
    1. "title": Write a CLICK-WORTHY, engaging headline under 65 characters. Do not use the original title. Make it punchy.
    2. "summary": Write a 2-sentence summary (max 220 chars). Do not say "The article says". Just state the news and why it matters. 
    
    Example Output:
    {{ "title": "iPhone 16 Leaks: No Buttons?", "summary": "Apple's next phone might ditch physical buttons entirely. This shift could redefine smartphone durability standards." }}
    """
    
    try:
        response = model.generate_content(prompt)
        extracted_data = extract_json(response.text)
        
        if extracted_data and 'title' in extracted_data and 'summary' in extracted_data:
            return extracted_data
        else:
            raise ValueError("Invalid JSON found in AI response")

    except Exception as e:
        print(f"⚠️ AI Gen Error: {e}. Raw Text: {raw_text[:50]}...", flush=True)
        # Fallback: simple cleanup
        fallback_title = raw_text.split(':')[1].strip() if "Title:" in raw_text else "New Tech Update"
        return {
            "title": fallback_title[:60], 
            "summary": "Check the source link for the full details on this story."
        }

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
            for entry in d.entries[:2]:
                
                if check_duplicate(entry.link): continue
                if check_semantic_duplicate(entry.title): continue

                print(f"🆕 Found: {entry.title}", flush=True)
                
                # GET FULL CONTENT
                full_context = get_best_content(entry)
                
                # AGENT REWRITE
                ai_data = process_with_ai(full_context)
                
                # SAVE & NOTIFY
                lead_id = create_lead_in_directus(ai_data['title'], entry.link, ai_data['summary'])
                if lead_id:
                    post_to_slack(ai_data, entry.link, lead_id)
                    time.sleep(2)
                    
        except Exception as e:
            print(f"⚠️ Feed Error ({feed}): {e}", flush=True)

if __name__ == "__main__":
    print("🚀 Scout (Agent V2) is starting...", flush=True)
    while True:
        try:
            run_scout()
        except Exception as e:
            print(f"❌ Critical Error: {e}", flush=True)
        
        print("💤 Sleeping for 30 minutes...", flush=True)
        time.sleep(1800)