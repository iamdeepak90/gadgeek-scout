"""
config.py — Single source of truth for ALL credentials + runtime settings.

IMPORTANT
- Do NOT commit real secrets to Git.
- This project intentionally does NOT use .env files (per your request).
- Put all credentials below.

Time zone: Asia/Kolkata (UTC+05:30)
"""

from __future__ import annotations

# =========================
# Directus (CMS)
# =========================
DIRECTUS_URL = "https://admin.gadgeek.in"
DIRECTUS_TOKEN = "Cmq-X3we8iSjBHbxziDrwas55FP3d6gz"

# Collections (change if your Directus collection names differ)
LEADS_COLLECTION = "news_leads"
ARTICLES_COLLECTION = "Articles"  # keep existing if you already use "Articles"
CATEGORIES_COLLECTION = "Categories"  # optional (recommended)

# Field mapping (change if your Directus fields differ)
# Leads
LEAD_F_TITLE = "title"
LEAD_F_SOURCE_URL = "source_url"
LEAD_F_CATEGORY = "category"          # string slug; if you use M2O to categories, set to relation field name
LEAD_F_STATUS = "status"
LEAD_F_DISCOVERED_AT = "discovered_at"
LEAD_F_FINGERPRINT = "fingerprint"
LEAD_F_MATCHED_KEYWORDS = "matched_keywords"
LEAD_F_SOURCE_DOMAIN = "source_domain"
LEAD_F_PUBLISHED_AT = "source_published_at"
LEAD_F_SLACK_TS = "slack_ts"
LEAD_F_SLACK_CHANNEL = "slack_channel"
LEAD_F_APPROVED_AT = "approved_at"
LEAD_F_APPROVED_BY = "approved_by"
LEAD_F_LAST_ERROR = "last_error"
LEAD_F_PRIORITY = "priority"              # integer (optional)

# Articles
ART_F_TITLE = "title"
ART_F_SLUG = "slug"
ART_F_STATUS = "status"                   # "draft" / "published"
ART_F_CATEGORY = "category"          # string slug; if you use M2O to categories, set to relation field name
ART_F_SOURCE_URL = "source_url"
ART_F_FINGERPRINT = "fingerprint"
ART_F_SHORT_DESCRIPTION = "short_description"
ART_F_CONTENT = "content"                 # long text (HTML)
ART_F_SOURCES = "sources"                 # JSON array of URLs/domains
ART_F_FEATURED_IMAGE_URL = "featured_image"
ART_F_FEATURED_IMAGE_ALT = "featured_image_alt"
ART_F_FEATURED_IMAGE_CREDIT = "featured_image_credit"
ART_F_FOCUS_KEYWORD = "focus_keyword"
ART_F_TAGS = "tags"                       # JSON array
ART_F_META_TITLE = "meta_title"
ART_F_META_DESCRIPTION = "meta_description"
ART_F_WORD_COUNT = "word_count"
ART_F_PUBLISHED_AT = "published_at"

# =========================
# Slack (Approval workflow)
# =========================
SLACK_BOT_TOKEN = "xoxb-10413021355318-10399647335735-VVr0Giv2PAn0pstMuP5cuDtO"
SLACK_SIGNING_SECRET = "4f28dc0a3781d55f764267910c7bcc77"
SLACK_CHANNEL = "C0AC72SJYJW"

# If Slack is not configured, scout will auto-queue leads (no manual approval).
REQUIRE_SLACK_APPROVAL = True

# =========================
# Discovery sources
# =========================
ENABLE_RSS_DISCOVERY = True
ENABLE_NEWSDATA_DISCOVERY = False

# Scout behavior (how many leads and Slack messages per run)
# NOTE: scheduling is typically handled by cron/systemd. If you run scout.py in loop mode,
# it will sleep this many seconds between runs.
SCOUT_INTERVAL_SECONDS = 30 * 60

# Hard cap Slack messages per run (prevents 150+ messages)
SCOUT_MAX_SLACK_PER_RUN = 10

# Quotas to avoid spam from a single category or single source domain
SCOUT_MAX_SLACK_PER_CATEGORY_PER_RUN = 2
SCOUT_MAX_SLACK_PER_DOMAIN_PER_RUN = 2

# How many new leads to create per run (can be > Slack cap; backlog will be posted in later runs)
SCOUT_MAX_NEW_LEADS_PER_RUN = 80

# Minimum "final_score" to create a lead (final_score = classifier score + priority boost)
SCOUT_MIN_SCORE_TO_CREATE = 0

# NewsData.io — optional supplement for discovery (title/URL only)
NEWSDATA_API_KEY = "YOUR_NEWSDATA_KEY"
NEWSDATA_ENDPOINT = "https://newsdata.io/api/1/news"

# RSS feeds:
# You can add MANY feeds; the classifier will map items to your category slugs.
# Tip: Keep a broad list. The category filter will enforce your selected categories.
RSS_FEEDS = [
    # Broad tech
    "https://www.theverge.com/rss/index.xml",
    "https://techcrunch.com/feed/",
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://www.wired.com/feed/rss",
    "https://www.androidpolice.com/feed/",
    "https://www.xda-developers.com/feed/",
    "https://www.tomshardware.com/feeds/all",
    "https://krebsonsecurity.com/feed/",
    "https://www.bleepingcomputer.com/feed/",
    "https://news.ycombinator.com/rss",
    # Vendor / platform blogs
    "https://developer.nvidia.com/blog/feed",
    "https://aws.amazon.com/blogs/aws/feed/",
    "https://blog.google/rss/",
    "https://devblogs.microsoft.com/feed/",
    "https://engineering.fb.com/feed/",
]

# =========================
# Search / Research APIs
# =========================
# LangSearch (primary for deep research)
LANGSEARCH_API_KEY = "sk-f1a7162c071e485fad0a38a1cb068b5f"
LANGSEARCH_ENABLE_RERANK = True
LANGSEARCH_FRESHNESS = "oneWeek"     # oneDay / oneWeek / oneMonth / oneYear / noLimit
LANGSEARCH_RESULTS = 10              # max 10 per API call (per docs)
LANGSEARCH_TOP_SOURCES = 6           # how many sources to keep after rerank + domain dedupe

# Brave Search (fallback if LangSearch fails / quota)
BRAVE_SEARCH_API_KEY = ""            # optional
BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
BRAVE_RESULTS = 10

# =========================
# LLMs (Writing)
# =========================
# Primary: Gemini (Google AI Studio / Gemini API)
GEMINI_API_KEY = "AIzaSyARZL9PW073U_T6jxVIPVcFnHhXedZjgO4"
GEMINI_MODEL_FACTPACK = "gemini-2.5-flash-lite"
GEMINI_MODEL_WRITER = "gemini-2.5-flash"
GEMINI_TEMPERATURE_FACTPACK = 0.2
GEMINI_TEMPERATURE_WRITER = 0.7
GEMINI_TEMPERATURE_POLISH = 0.6

# Optional fallback: OpenAI (only used if Gemini fails AND key provided)
OPENAI_API_KEY = "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"
OPENAI_MODEL_WRITER = "gpt-5-mini"

# =========================
# Images (free sources)
# =========================
# Provide keys if you have them; otherwise Wikimedia/Openverse will still work.
UNSPLASH_ACCESS_KEY = "Zf7mQjN04Ec-6LWBdngVEL6sKLnnXbqZTsMov5_F7CI"
PEXELS_API_KEY = "ks1X6yC5ydEypXxnu3qydCtK8Aso9KmIJcAB2R9WG292CZSx2ZZtJtVT"
PIXABAY_API_KEY = "13824721-59e9edde0ff0f966d84216449"

# =========================
# Category configuration
# =========================
# IMPORTANT:
# - selected categories define what your site publishes.
# - tier-3 categories are recommended as optional (tech-events ok; leaks-rumors often excluded from Google News sitemap).
INCLUDE_TIER3 = False

# If you want ONLY specific categories, list them here. Empty means auto-select:
# - all priority 1 & 2 categories (+ the two Option-B adds)
SELECTED_CATEGORY_SLUGS = []  # e.g. ["smartphones","ai-software","privacy-security"]

FEATURED_CATEGORIES = {
    # TIER 1
    "smartphones": {
        "name": "Smartphones",
        "emoji": "📱",
        "priority": 1,
        "keywords": [
            "smartphone","mobile phone","phone launch","smartphone launch","5g phone",
            "foldable","flip phone","clamshell","flagship","mid-range",
            "android","ios","one ui","oxygenos","hyperos","miui","coloros","funtouch os",
            "iphone","samsung","pixel","oneplus","xiaomi","oppo","vivo","realme","nothing phone",
            "galaxy","motorola","moto","sony xperia","asus","rog phone","huawei","honor","iqoo",
            "infinix","tecno","lava",
            "snapdragon","dimensity","exynos","tensor","bionic","a-series chip"
        ]
    },
    "phone-reviews": {
        "name": "Phone Reviews",
        "emoji": "⭐",
        "priority": 1,
        "keywords": [
            "phone review","smartphone review","iphone review","android review",
            "hands-on","first impressions","unboxing",
            "camera test","camera samples","performance test","benchmark",
            "battery test","battery life","display test","speaker test",
            "full review","long-term review","device review","verdict","rating","review roundup"
        ]
    },
    "laptops-pcs": {
        "name": "Laptops & PCs",
        "emoji": "💻",
        "priority": 1,
        "keywords": [
            "laptop","notebook","ultrabook","gaming laptop","chromebook","chromebook plus",
            "pc","desktop","workstation","mini pc",
            "macbook","surface","thinkpad","dell xps",
            "2-in-1","convertible","detachable","tablet","ipad",
            "windows","macos","linux","copilot+ pc","npu","arm laptop","snapdragon x",
            "laptop review","laptop launch","pc build","prebuilt"
        ]
    },
    "ai-software": {
        "name": "AI & Software",
        "emoji": "🤖",
        "priority": 1,
        "keywords": [
            "ai","artificial intelligence","machine learning","llm","foundation model",
            "open-source model","model release","fine-tuning","inference","agents",
            "context window","token",
            "chatgpt","openai","gemini","deepmind","anthropic","claude","copilot","github copilot",
            "mistral","llama",
            "software update","app update","ios update","android update",
            "windows update","macos update","chrome update","edge update","firefox update",
            "security patch","patch tuesday","stable channel","beta channel",
            "api update","sdk","release notes","changelog"
        ]
    },
    "gaming": {
        "name": "Gaming",
        "emoji": "🎮",
        "priority": 1,
        "keywords": [
            "gaming","playstation","ps5","xbox","nintendo","switch",
            "console","handheld","steam deck","rog ally","legion go",
            "game release","game review","esports","steam","epic games",
            "game pass","ps plus",
            "gaming pc","gpu","rtx","radeon",
            "driver update","dlss","fsr","frame generation","ray tracing"
        ]
    },
    # NEW Tier 1
    "chips-silicon": {
        "name": "Chips & Silicon",
        "emoji": "🧩",
        "priority": 1,
        "keywords": [
            "intel","amd","nvidia","qualcomm","mediatek","apple silicon","arm","risc-v",
            "ryzen","threadripper","epyc","core ultra","xeon",
            "geforce","rtx","radeon","arc",
            "snapdragon","dimensity","exynos","tensor",
            "npu","gpu","cpu","apu","soc","chipset",
            "accelerator","cuda","rocm","benchmark","performance","efficiency",
            "tsmc","samsung foundry","intel foundry","fab","node","nm","chip shortage"
        ]
    },

    # TIER 2
    "buying-guides": {
        "name": "Buying Guides",
        "emoji": "💰",
        "priority": 2,
        "keywords": [
            "buying guide","best phone","best laptop","best earbuds","best smartwatch",
            "top 10","top 5","best under","budget phone","budget laptop",
            "worth buying","should you buy","recommendation","best for",
            "which to buy","affordable","cheap","value for money",
            "best deals","deal","discount","price drop","sale","gift guide","back to school","student laptop"
        ]
    },
    "comparisons": {
        "name": "Comparisons",
        "emoji": "⚖️",
        "priority": 2,
        "keywords": [
            " vs "," versus ","comparison","compared","compares to",
            "which is better","head to head","face-off","battle","showdown",
            "difference between","alternatives","best alternative",
            "upgrade from","should you upgrade",
            "iphone vs","android vs","mac vs pc"
        ]
    },
    "wearables": {
        "name": "Wearables & Accessories",
        "emoji": "⌚",
        "priority": 2,
        "keywords": [
            "wearable","smartwatch","fitness tracker","smart ring",
            "apple watch","galaxy watch","fitbit","garmin","oura","whoop",
            "earbuds","wireless earbuds","airpods",
            "headphones","earphones","noise cancelling",
            "charging case","spatial audio","smart glasses","xr","mixed reality"
        ]
    },
    "tech-industry": {
        "name": "Tech Industry News",
        "emoji": "🏢",
        "priority": 2,
        "keywords": [
            "acquisition","merger","layoff","ceo",
            "earnings","revenue","guidance","forecast",
            "stock price","ipo","funding","investment",
            "lawsuit","settlement","fine",
            "regulation","antitrust","probe","investigation",
            "partnership","deal",
            "sec","doj","ftc","cma","eu","dma","dsa","gdpr"
        ]
    },
    "privacy-security": {
        "name": "Privacy & Security",
        "emoji": "🔒",
        "priority": 2,
        "keywords": [
            "privacy","security","cybersecurity",
            "breach","data breach","data leak","stolen data",
            "hack","hacked","malware","ransomware","phishing",
            "vulnerability","exploit","zero-day","cve","critical vulnerability",
            "patch now","security patch","supply chain attack",
            "spyware","stalkerware",
            "encryption","vpn","password",
            "two-factor","2fa","mfa","passkey","passkeys","biometric","sim swap"
        ]
    },
    # NEW Tier 2
    "smart-home-iot": {
        "name": "Smart Home & IoT",
        "emoji": "🏠",
        "priority": 2,
        "keywords": [
            "smart home","iot","internet of things",
            "alexa","echo","google home","nest","homekit",
            "matter","thread","zigbee","z-wave",
            "smart speaker","smart display","smart thermostat",
            "smart lock","video doorbell","security camera",
            "smart lights","smart plug","robot vacuum",
            "smart tv","streaming device",
            "router","mesh wifi","wi-fi 6","wi-fi 7"
        ]
    },

    # TIER 3
    "leaks-rumors": {
        "name": "Leaks & Rumors",
        "emoji": "🔮",
        "priority": 3,
        "keywords": [
            "leak","leaked","rumor","rumored","reportedly",
            "upcoming","expected","could launch","might release",
            "could feature","sources say","insider","tipster","speculation",
            "render","cad","prototype","dummy unit","spotted","certification","tenaa","fcc","3c","bis","benchmark leak","geekbench"
        ]
    },
    "tech-events": {
        "name": "Tech Events",
        "emoji": "🎪",
        "priority": 3,
        "keywords": [
            "event","conference","keynote","developer conference","tech summit",
            "ces","mwc","ifa","computex","wwdc","google io","samsung unpacked","microsoft build",
            "re:invent","google cloud next","nvidia gtc","gdc","gamescom","siggraph"
        ]
    }
}

# =========================
# Publishing policy
# =========================
TIMEZONE = "Asia/Kolkata"
PUBLISH_WINDOW_START = "06:00"  # 24h format in TIMEZONE
PUBLISH_WINDOW_END   = "23:30"
MAX_PUBLISH_PER_DAY = 25
MIN_MINUTES_BETWEEN_PUBLISHES = 25

# Lead statuses used by this pipeline:
LEAD_STATUS_PENDING = "pending"      # discovered, waiting for Slack approval
LEAD_STATUS_QUEUED = "queued"        # approved, ready for publisher
LEAD_STATUS_PROCESSING = "processing"
LEAD_STATUS_PROCESSED = "processed"
LEAD_STATUS_REJECTED = "rejected"
LEAD_STATUS_FAILED = "failed"

ARTICLE_STATUS_PUBLISHED = "published"
ARTICLE_STATUS_DRAFT = "draft"

# Content requirements
ARTICLE_WORD_TARGET_MIN = 1000
ARTICLE_WORD_TARGET_MAX = 1500
HOOK_WORDS_MIN = 120
HOOK_WORDS_MAX = 150
H2_MIN = 4
H2_MAX = 6
TABLE_MAX = 1

# =========================
# Local state / caching
# =========================
DATA_DIR = "data"
STATE_DB_PATH = f"{DATA_DIR}/state.db"
LOCK_PATH = f"{DATA_DIR}/publisher.lock"

# =========================
# Networking / timeouts
# =========================
HTTP_TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (compatible; TechNewsAutomation/1.0; +https://example.com/bot)"