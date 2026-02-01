"""
Enhanced Multi-AI Configuration
Uses multiple free APIs for maximum humanization
"""
import re
import requests
import json
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from datetime import datetime, timezone

# ==================== CONFIGURATION ====================
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

# ==================== FREE AI API KEYS ====================
# Groq - BEST for analysis & initial drafts (14,400/day)
GROQ_API_KEY = "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"

# OpenRouter - Multiple free models
OPENROUTER_API_KEY = "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"

# Together AI - Alternative free API (1M tokens/day free tier)
TOGETHER_API_KEY = "YOUR_TOGETHER_API_KEY_HERE"

# Hugging Face - Fallback
HUGGINGFACE_API_KEY = "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"

# Gemini - Last resort fallback
GEMINI_API_KEY = "AIzaSyARZL9PW073U_T6jxVIPVcFnHhXedZjgO4"

# Unsplash for images (Free tier: 50 requests/hour)
UNSPLASH_ACCESS_KEY = "Zf7mQjN04Ec-6LWBdngVEL6sKLnnXbqZTsMov5_F7CI"

# Pexels for images (Free, unlimited)
PEXELS_API_KEY = "ks1X6yC5ydEypXxnu3qydCtK8Aso9KmIJcAB2R9WG292CZSx2ZZtJtVT"

# ==================== HELPER FUNCTIONS ====================

def get_current_utc_time():
    """Get current time in UTC (timezone-aware)"""
    return datetime.now(timezone.utc)


def extract_json(text):
    """Extract JSON from AI response"""
    try:
        return json.loads(text)
    except:
        # Try to find JSON in markdown
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
    """Create SEO-friendly slug from title"""
    # Extract main keywords (remove filler words)
    filler_words = ['the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 
                    'of', 'with', 'by', 'from', 'as', 'is', 'was', 'are', 'be', 'has', 'have']
    
    slug = title.lower().strip()
    
    # Remove special characters
    slug = re.sub(r'[^\w\s-]', '', slug)
    
    # Split into words
    words = slug.split()
    
    # Remove filler words and keep important ones
    keywords = [w for w in words if w not in filler_words]
    
    # Take first 5-6 keywords
    keywords = keywords[:6]
    
    # Join with hyphens
    slug = '-'.join(keywords)
    
    # Clean up multiple hyphens
    slug = re.sub(r'-+', '-', slug)
    
    return slug[:60]  # Limit length


def scrape_full_article(url, max_chars=20000):
    """Enhanced article scraping with more content"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'lxml')
        
        title = ''
        if soup.find('h1'):
            title = soup.find('h1').get_text().strip()
        elif soup.find('title'):
            title = soup.find('title').get_text().strip()
        
        content_text = ''
        
        article = soup.find('article')
        if article:
            for tag in article.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside', 'iframe', 'form']):
                tag.decompose()
            content_text = article.get_text(separator=' ', strip=True)
        
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
        
        if not content_text or len(content_text) < 200:
            main = soup.find('main')
            if main:
                for tag in main.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside', 'iframe', 'form']):
                    tag.decompose()
                content_text = main.get_text(separator=' ', strip=True)
        
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


def fetch_relevant_image(keywords, preferred_service='pexels'):
    """
    Fetch relevant image from free stock photo APIs
    Returns image URL or None
    """
    try:
        if preferred_service == 'pexels' and PEXELS_API_KEY and PEXELS_API_KEY != "YOUR_PEXELS_KEY_HERE":
            # Pexels API (Free, unlimited)
            url = f"https://api.pexels.com/v1/search?query={keywords}&per_page=1&orientation=landscape"
            headers = {"Authorization": PEXELS_API_KEY}
            
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data.get('photos'):
                    return data['photos'][0]['src']['large']
        
        elif preferred_service == 'unsplash' and UNSPLASH_ACCESS_KEY and UNSPLASH_ACCESS_KEY != "YOUR_UNSPLASH_KEY_HERE":
            # Unsplash API (50 req/hour free)
            url = f"https://api.unsplash.com/search/photos?query={keywords}&per_page=1&orientation=landscape"
            headers = {"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}
            
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data.get('results'):
                    return data['results'][0]['urls']['regular']
    
    except Exception as e:
        print(f"⚠️ Image fetch failed: {e}", flush=True)
    
    return None


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