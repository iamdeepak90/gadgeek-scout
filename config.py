"""
Core Configuration - Centralized settings with enhanced reliability
v7.0 - Added retry logic, caching, and humanization support
"""
import re
import requests
import json
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from datetime import datetime, timezone
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from cachetools import TTLCache
import time

# ==================== API CREDENTIALS ====================
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
# Primary: Gemini 2.0 for Deep Research (Free tier: 15 RPM, 1M TPM, 1500 RPD)
GEMINI_API_KEY = "AIzaSyARZL9PW073U_T6jxVIPVcFnHhXedZjgO4"

# Secondary: OpenAI for Content Generation
OPENAI_API_KEY = "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"

# Pexels for Images (Free: 200 requests/hour)
PEXELS_API_KEY = "ks1X6yC5ydEypXxnu3qydCtK8Aso9KmIJcAB2R9WG292CZSx2ZZtJtVT"

# ==================== CACHING ====================
# Cache for API responses (10-minute TTL)
api_cache = TTLCache(maxsize=100, ttl=600)


# ==================== RETRY DECORATORS ====================

def retry_on_api_error(func):
    """Decorator for API calls with exponential backoff"""
    return retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((requests.exceptions.RequestException, requests.exceptions.Timeout)),
        before_sleep=lambda retry_state: print(f"   🔄 Retry {retry_state.attempt_number}/3...", flush=True)
    )(func)


# ==================== UTILITY FUNCTIONS ====================

def get_current_utc_time():
    """Get current UTC time (timezone-aware)"""
    return datetime.now(timezone.utc)


def extract_json(text):
    """
    Extract JSON from AI response with better error handling
    Handles markdown code blocks and malformed JSON
    """
    if not text:
        return None
    
    # Try direct JSON parse first
    try:
        return json.loads(text)
    except:
        pass
    
    # Try to extract from markdown code blocks
    patterns = [
        r'```json\s*\n(.*?)\n```',  # ```json ... ```
        r'```\s*\n(.*?)\n```',      # ``` ... ```
        r'\{.*\}',                   # Any JSON object
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                json_str = match.group(1) if '```' in pattern else match.group(0)
                return json.loads(json_str)
            except:
                continue
    
    return None


def get_domain_name(url):
    """Extract clean domain name from URL"""
    try:
        domain = urlparse(url).netloc
        # Remove www. and get main part
        domain = domain.replace('www.', '')
        # Get first part before TLD
        parts = domain.split('.')
        return parts[0].capitalize() if parts else "Tech Source"
    except:
        return "Tech Source"


def create_slug_from_text(text):
    """
    Create SEO slug from text with better keyword extraction
    Returns: lowercase-hyphenated-slug (max 60 chars, 5-6 keywords)
    """
    # Common filler words to remove
    filler_words = {
        'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 
        'of', 'with', 'by', 'from', 'as', 'is', 'was', 'are', 'be', 'has', 
        'have', 'this', 'that', 'will', 'can', 'been', 'into', 'its', 'than',
        'now', 'new', 'just', 'how', 'what', 'when', 'where', 'why', 'who'
    }
    
    # Clean and normalize
    slug = text.lower().strip()
    slug = re.sub(r'[^\w\s-]', '', slug)  # Remove special chars
    slug = re.sub(r'\s+', ' ', slug)       # Normalize spaces
    
    # Split into words
    words = slug.split()
    
    # Extract meaningful keywords
    keywords = []
    for word in words:
        if word not in filler_words and len(word) > 2:
            keywords.append(word)
            if len(keywords) >= 6:
                break
    
    # Fallback: if too few keywords, take first 6 words
    if len(keywords) < 3:
        keywords = [w for w in words if len(w) > 2][:6]
    
    # Join and clean
    slug = '-'.join(keywords)
    slug = re.sub(r'-+', '-', slug)  # Remove multiple hyphens
    slug = slug.strip('-')            # Remove leading/trailing hyphens
    
    return slug[:60]  # Limit to 60 characters


# ==================== API REQUEST HANDLERS ====================

@retry_on_api_error
def directus_request(method, endpoint, data=None, timeout=30):
    """
    Centralized Directus API handler with retry logic
    
    Args:
        method: HTTP method (GET, POST, PATCH)
        endpoint: API endpoint (e.g., '/items/Articles')
        data: Request payload (optional)
        timeout: Request timeout in seconds
    
    Returns:
        dict: Response JSON or None on failure
    """
    headers = {"Authorization": f"Bearer {DIRECTUS_TOKEN}"}
    url = f"{DIRECTUS_URL}{endpoint}"
    
    # Cache key for GET requests
    cache_key = f"{method}:{endpoint}" if method.upper() == 'GET' else None
    
    # Check cache for GET requests
    if cache_key and cache_key in api_cache:
        print(f"   📦 Using cached response", flush=True)
        return api_cache[cache_key]
    
    try:
        if method.upper() == 'GET':
            r = requests.get(url, headers=headers, timeout=timeout)
        elif method.upper() == 'POST':
            r = requests.post(url, json=data, headers=headers, timeout=timeout)
        elif method.upper() == 'PATCH':
            r = requests.patch(url, json=data, headers=headers, timeout=timeout)
        else:
            print(f"   ⚠️ Unsupported HTTP method: {method}", flush=True)
            return None
        
        # Success codes
        if r.status_code in [200, 201]:
            result = r.json()
            # Cache GET responses
            if cache_key:
                api_cache[cache_key] = result
            return result
        
        # Error handling
        print(f"   ⚠️ Directus {method} {endpoint}: HTTP {r.status_code}", flush=True)
        
        if r.status_code == 500:
            # Log the full error for debugging
            try:
                error_detail = r.json()
                print(f"      Server Error Details:", flush=True)
                print(f"      {json.dumps(error_detail, indent=2)[:500]}", flush=True)
            except:
                print(f"      Server Error: {r.text[:500]}", flush=True)
        elif r.status_code == 401:
            print(f"      Authentication failed - check token", flush=True)
        elif r.status_code == 404:
            print(f"      Endpoint not found", flush=True)
        elif r.status_code == 429:
            print(f"      Rate limit exceeded - backing off", flush=True)
            time.sleep(5)
        elif r.status_code == 400:
            # Bad request - show validation errors
            try:
                error_detail = r.json()
                print(f"      Validation Error:", flush=True)
                print(f"      {json.dumps(error_detail, indent=2)[:500]}", flush=True)
            except:
                print(f"      Bad Request: {r.text[:500]}", flush=True)
        
        return None
            
    except requests.exceptions.Timeout:
        print(f"   ⏱️ Directus request timeout ({timeout}s)", flush=True)
        raise
    except requests.exceptions.ConnectionError:
        print(f"   🔌 Connection error to Directus", flush=True)
        raise
    except Exception as e:
        print(f"   ❌ Directus error: {str(e)[:150]}", flush=True)
        raise


@retry_on_api_error
def fetch_image_from_pexels(keywords):
    """
    Fetch relevant image from Pexels with retry logic
    
    Args:
        keywords: Search keywords for image
    
    Returns:
        str: Image URL or None
    """
    if not PEXELS_API_KEY or PEXELS_API_KEY == "YOUR_PEXELS_KEY_HERE":
        return None
    
    try:
        # Clean keywords for URL
        keywords_clean = keywords.replace(' ', '+')[:100]
        
        url = f"https://api.pexels.com/v1/search?query={keywords_clean}&per_page=1&orientation=landscape"
        headers = {"Authorization": PEXELS_API_KEY}
        
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get('photos') and len(data['photos']) > 0:
                image_url = data['photos'][0]['src']['large']
                print(f"   🖼️ Image found: Pexels", flush=True)
                return image_url
        
        print(f"   ⚠️ No images found for: {keywords[:50]}", flush=True)
        return None
        
    except requests.exceptions.Timeout:
        print(f"   ⏱️ Pexels request timeout", flush=True)
        return None
    except Exception as e:
        print(f"   ⚠️ Image fetch failed: {str(e)[:100]}", flush=True)
        return None


# ==================== PUBLISHING FUNCTIONS ====================

def publish_article_to_directus(title, content, short_description, category, 
                                  source_url, featured_image=None, slug=None):
    """
    Single function to publish articles to Directus
    Used by both bot_server and publisher
    
    Args:
        title: Article title (60-70 chars)
        content: Full HTML content
        short_description: 200 char description
        category: Article category slug
        source_url: Original source URL
        featured_image: Image URL (optional)
        slug: Custom slug or auto-generated
    
    Returns:
        bool: True if published successfully
    """
    
    # Auto-generate slug if not provided
    if not slug:
        slug = create_slug_from_text(short_description if short_description else title)
    
    # Validate required fields
    if not title or not content or not short_description:
        print(f"   ❌ Missing required fields for publishing", flush=True)
        return False
    
    # Validate minimum content length
    word_count = len(content.split())
    if word_count < 500:
        print(f"   ❌ Article too short ({word_count} words, minimum 500)", flush=True)
        print(f"   ℹ️ This article will not be published to maintain quality standards", flush=True)
        return False
    
    payload = {
        "status": "published",
        "title": title[:200],
        "slug": slug[:100],
        "content": content,
        "short_description": short_description[:300],
    }
    
    # Only add featured_image if it exists
    if featured_image:
        payload["featured_image"] = featured_image
    
    print(f"   📤 Publishing to Directus...", flush=True)
    print(f"      Title: {title[:60]}...", flush=True)
    print(f"      Slug: {slug}", flush=True)
    print(f"      Category: {category}", flush=True)
    print(f"      Content: {len(content)} chars, {len(content.split())} words", flush=True)
    
    result = directus_request('POST', '/items/{ARTICLE_COLLECTION}', payload)
    
    if result and 'data' in result:
        article_id = result['data'].get('id', 'unknown')
        print(f"   ✅ Published successfully! ID: {article_id}", flush=True)
        return True
    else:
        print(f"   ❌ Publishing failed", flush=True)
        return False


def update_lead_status(lead_id, status):
    """
    Single function to update lead status
    
    Args:
        lead_id: Lead ID
        status: New status (pending, queued, processed, rejected)
    
    Returns:
        bool: True if updated successfully
    """
    valid_statuses = ['pending', 'queued', 'processed', 'rejected']
    
    if status not in valid_statuses:
        print(f"   ⚠️ Invalid status: {status}", flush=True)
        return False
    
    result = directus_request('PATCH', f'/items/news_leads/{lead_id}', {"status": status}, timeout=10)
    
    if result:
        print(f"   ✅ Lead #{lead_id} → {status}", flush=True)
        return True
    else:
        print(f"   ❌ Failed to update lead #{lead_id}", flush=True)
        return False


# ==================== HEALTH CHECK ====================

def check_api_health():
    """
    Check health of all API connections
    Returns dict with status of each service
    """
    health_status = {}
    
    # Check Directus
    try:
        result = directus_request('GET', '/server/health', timeout=5)
        health_status['directus'] = 'healthy' if result else 'error'
    except:
        health_status['directus'] = 'error'
    
    # Check Gemini API
    health_status['gemini'] = 'configured' if GEMINI_API_KEY and GEMINI_API_KEY != "YOUR_GEMINI_KEY" else 'missing'
    
    # Check OpenAI API
    health_status['openai'] = 'configured' if OPENAI_API_KEY and OPENAI_API_KEY != "YOUR_OPENAI_KEY" else 'missing'
    
    # Check Pexels API
    health_status['pexels'] = 'configured' if PEXELS_API_KEY and PEXELS_API_KEY != "YOUR_PEXELS_KEY" else 'missing'
    
    return health_status