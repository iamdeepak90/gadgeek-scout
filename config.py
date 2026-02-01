"""
Multi-AI Configuration - Using Free Tier APIs
Combines multiple AI models for maximum humanization
"""
import os
import re
import requests
from urllib.parse import urlparse
from bs4 import BeautifulSoup

# ==================== DIRECTUS & SLACK ====================
DIRECTUS_URL = "https://admin.gadgeek.in"
DIRECTUS_TOKEN = "Cmq-X3we8iSjBHbxziDrwas55FP3d6gz"
SLACK_BOT_TOKEN = "xoxb-10413021355318-10399647335735-VVr0Giv2PAn0pstMuP5cuDtO"
SLACK_CHANNEL = "C0AC72SJYJW"
SLACK_SIGNING_SECRET = "4f28dc0a3781d55f764267910c7bcc77"
ARTICLE_COLLECTION = "Articles"

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

# ==================== AI API KEYS ====================

# Option 1: Groq (RECOMMENDED - Very generous free tier)
# Sign up: https://console.groq.com
# Free tier: 30 requests/minute, 14,400/day
GROQ_API_KEY = "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"  # ← Add your Groq key

# Option 2: OpenRouter (Multiple free models available)
# Sign up: https://openrouter.ai
# Free models: Google Gemini Flash, Meta Llama, Mistral, etc.
OPENROUTER_API_KEY = "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"  # ← Add your OpenRouter key

# Option 3: Gemini (Keep as backup, but use Flash-8B for higher limits)
# Free tier: 15 RPM, 1500 RPD
GEMINI_API_KEY = "AIzaSyARZL9PW073U_T6jxVIPVcFnHhXedZjgO4"

# Option 4: Hugging Face (Free inference API)
# Sign up: https://huggingface.co/settings/tokens
HUGGINGFACE_API_KEY = "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"  # ← Optional

# ==================== AI ROUTING STRATEGY ====================

# Primary: Groq (Fast analysis & drafting)
PRIMARY_AI = "groq"  # Options: "groq", "openrouter", "gemini"

# Secondary: OpenRouter (Humanization & refinement)
SECONDARY_AI = "openrouter"  # Options: "openrouter", "groq", "gemini"

# Fallback: Gemini Flash-8B (Final validation)
FALLBACK_AI = "gemini"

# ==================== HELPER FUNCTIONS ====================

def extract_json(text):
    """Extract JSON from AI response"""
    import json
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


def get_domain_name(url):
    """Extract domain name from URL"""
    try:
        domain = urlparse(url).netloc
        return domain.replace('www.', '').split('.')[0].capitalize()
    except:
        return "Tech Source"


def create_slug(title):
    """Create URL slug from title"""
    slug = title.lower().strip()
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'[\s-]+', '-', slug)
    return slug[:100]


def scrape_full_article(url, max_chars=15000):
    """Scrape full article content"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
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
        
        # Extract content
        content_text = ''
        
        # Try article tag
        article = soup.find('article')
        if article:
            for tag in article.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside', 'iframe', 'form']):
                tag.decompose()
            content_text = article.get_text(separator=' ', strip=True)
        
        # Try common classes
        if not content_text or len(content_text) < 200:
            for class_name in ['content', 'article-body', 'post-content', 'entry-content', 
                              'article-content', 'story-body', 'article__body', 'post__content']:
                content_div = soup.find('div', class_=class_name)
                if content_div:
                    for tag in content_div.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside', 'iframe', 'form']):
                        tag.decompose()
                    content_text = content_div.get_text(separator=' ', strip=True)
                    if len(content_text) > 200:
                        break
        
        # Try main tag
        if not content_text or len(content_text) < 200:
            main = soup.find('main')
            if main:
                for tag in main.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside', 'iframe', 'form']):
                    tag.decompose()
                content_text = main.get_text(separator=' ', strip=True)
        
        # Fallback to paragraphs
        if not content_text or len(content_text) < 200:
            paragraphs = soup.find_all('p')
            content_text = ' '.join([p.get_text().strip() for p in paragraphs if len(p.get_text().strip()) > 30])
        
        content_text = ' '.join(content_text.split())
        
        return {
            'title': title,
            'text': content_text[:max_chars],
            'word_count': len(content_text.split())
        }
        
    except Exception as e:
        print(f"⚠️ Scraping failed: {e}", flush=True)
        return {
            'title': '',
            'text': 'Content unavailable.',
            'word_count': 0
        }


def directus_request(method, endpoint, data=None, timeout=15):
    """Centralized Directus API handler"""
    headers = {"Authorization": f"Bearer {DIRECTUS_TOKEN}"}
    url = f"{DIRECTUS_URL}{endpoint}"
    
    try:
        if method.upper() == 'GET':
            r = requests.get(url, headers=headers, timeout=timeout)
        elif method.upper() == 'POST':
            r = requests.post(url, json=data, headers=headers, timeout=timeout)
        elif method.upper() == 'PATCH':
            r = requests.patch(url, json=data, headers=headers, timeout=timeout)
        else:
            return None
        
        if r.status_code in [200, 201]:
            return r.json()
        else:
            print(f"⚠️ Directus {method} {endpoint}: {r.status_code}", flush=True)
            return None
            
    except Exception as e:
        print(f"⚠️ Directus error: {str(e)[:100]}", flush=True)
        return None