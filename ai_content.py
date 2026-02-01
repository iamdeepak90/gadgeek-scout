"""
AI Content Generation Module v7.0
Enhanced with humanization, better prompting, and multi-model support
Uses Gemini 2.0 for research + OpenAI for generation + Humanization layer
"""
import requests
import json
import time
import re
from config import GEMINI_API_KEY, OPENAI_API_KEY, extract_json, fetch_image_from_pexels, retry_on_api_error


# ==================== GEMINI 2.0 DEEP RESEARCH ====================

@retry_on_api_error
def deep_research_with_gemini(title):
    """
    Use Gemini 2.0 Flash Thinking Exp for comprehensive research
    
    Args:
        title: Article title to research
    
    Returns:
        str: Deep research results with structured information
    """
    if not GEMINI_API_KEY:
        print(f"   ⚠️ No Gemini API key", flush=True)
        return f"Research topic: {title}"
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-thinking-exp:generateContent?key={GEMINI_API_KEY}"
    
    # Enhanced research prompt for tech news
    prompt = f"""You are a professional tech journalist conducting deep research.

TOPIC TO RESEARCH: {title}

Provide comprehensive, fact-based research covering:

1. MAIN EVENT & WHAT HAPPENED
   - Core details and facts
   - When, where, and who is involved
   - Key announcements or changes

2. BACKGROUND & CONTEXT
   - Why this matters now
   - Previous related developments
   - Industry trends leading to this

3. TECHNICAL DETAILS
   - Specifications, features, or capabilities
   - Technical implications
   - How it works (if applicable)

4. MARKET IMPACT & ANALYSIS
   - Who benefits and who loses
   - Market positioning
   - Competitive landscape
   - Pricing and availability

5. EXPERT PERSPECTIVES & OPINIONS
   - Industry analyst views
   - User reactions
   - Critical assessments

6. COMPARISONS & ALTERNATIVES
   - How it compares to competitors
   - Key differentiators
   - Similar products or services

7. FUTURE OUTLOOK
   - What happens next
   - Expected developments
   - Long-term implications

8. KEY DATA POINTS
   - Specific numbers, dates, and statistics
   - Release dates or timelines
   - Important metrics

Be specific, factual, and comprehensive. Include concrete details like model numbers, prices, dates, and quantitative data where available. Avoid speculation unless clearly labeled as such."""

    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }],
        "generationConfig": {
            "temperature": 0.4,  # Lower for more factual content
            "maxOutputTokens": 8000,
            "topP": 0.95,
            "topK": 40
        }
    }
    
    try:
        print(f"   🔬 Starting deep research with Gemini 2.0...", flush=True)
        response = requests.post(url, json=payload, timeout=90)
        response.raise_for_status()
        
        data = response.json()
        
        # Extract text from response
        if 'candidates' in data and len(data['candidates']) > 0:
            candidate = data['candidates'][0]
            if 'content' in candidate and 'parts' in candidate['content']:
                research_text = candidate['content']['parts'][0]['text']
                
                word_count = len(research_text.split())
                print(f"   ✅ Research complete: {word_count} words", flush=True)
                
                return research_text
        
        raise ValueError("Unexpected response format from Gemini")
        
    except requests.exceptions.Timeout:
        print(f"   ⏱️ Gemini request timeout", flush=True)
        return f"Research topic: {title}\n\nDeep research unavailable due to timeout."
    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code
        print(f"   ⚠️ Gemini HTTP {status_code}", flush=True)
        
        if status_code == 429:
            print(f"      Rate limit hit - backing off", flush=True)
            time.sleep(10)
        
        return f"Research topic: {title}\n\nPlease refer to source for details."
    except Exception as e:
        print(f"   ⚠️ Gemini research failed: {str(e)[:150]}", flush=True)
        return f"Research topic: {title}\n\nResearch unavailable."


# ==================== OPENAI CONTENT GENERATION ====================

@retry_on_api_error
def generate_article_with_openai(title, research_content, category):
    """
    Generate comprehensive, humanized article using OpenAI GPT-4
    
    Args:
        title: Article title
        research_content: Deep research from Gemini
        category: Article category
    
    Returns:
        dict: Complete article data with humanized content
    """
    if not OPENAI_API_KEY or OPENAI_API_KEY == "YOUR_OPENAI_API_KEY_HERE":
        print(f"   ⚠️ No OpenAI API key, using fallback", flush=True)
        return create_fallback_article(title, research_content, category)
    
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # ENHANCED PROMPT with HUMANIZATION techniques
    prompt = f"""You are an experienced tech journalist writing for a major publication like The Verge or TechCrunch.

RESEARCH BRIEF:
{research_content[:15000]}

YOUR TASK: Write a comprehensive, engaging article that sounds 100% human-written.

OUTPUT FORMAT (JSON):
{{
  "title": "engaging 60-70 char title",
  "short_description": "compelling 200 char SEO description",
  "slug": "seo-slug-5-6-keywords",
  "content": "full HTML article",
  "image_keywords": "3-4 keywords for image search"
}}

=== TITLE GUIDELINES (60-70 chars) ===
✓ Natural and conversational
✓ Include key details (company, product, feature)
✓ Use active voice
✓ Make it slightly intriguing
✗ NO clickbait
✗ NO "You won't believe" style

GOOD: "Google's Gemini 2.0 Beats GPT-4 in Latest Benchmarks"
BAD: "Google Just Released Something That Will CHANGE EVERYTHING"

=== SHORT DESCRIPTION (200 chars, 2 sentences) ===
First sentence: What happened (main news)
Second sentence: Why it matters or key detail

MUST include:
- Key company/product name
- Main action or announcement
- One compelling stat or detail

=== CONTENT STRUCTURE (1500-2000 words) ===

<p><strong>Opening hook:</strong> Start with the most compelling fact. Make it punchy. 2-3 sentences max.</p>

<h2>What Just Happened</h2>
<p>Explain the news clearly. What did [Company] announce? When? What's new? Keep paragraphs short (2-3 sentences).</p>

<p>Add context. Why now? What problem does this solve?</p>

<h3>The Key Details</h3>
<ul>
<li>Specific feature or spec with numbers</li>
<li>Another concrete detail with data</li>
<li>Third important point with context</li>
</ul>

<h2>Why This Actually Matters</h2>
<p>Real-world impact. How does this change things for users?</p>

<h3>For Consumers</h3>
<p>Practical implications. What can people actually do with this?</p>

<h3>For the Industry</h3>
<p>Bigger picture. How does this shift the market?</p>

<h2>Breaking Down the Tech</h2>

<h3>How It Works</h3>
<p>Explain the technology without jargon. If you must use technical terms, explain them.</p>

<h3>The Numbers</h3>
<table style="width:100%;border-collapse:collapse;margin:20px 0;">
<tr style="background:#f5f5f5;"><th style="padding:10px;text-align:left;border:1px solid #ddd;">Metric</th><th style="padding:10px;text-align:left;border:1px solid #ddd;">Performance</th></tr>
<tr><td style="padding:10px;border:1px solid #ddd;">Key spec 1</td><td style="padding:10px;border:1px solid #ddd;">Actual data</td></tr>
<tr><td style="padding:10px;border:1px solid #ddd;">Key spec 2</td><td style="padding:10px;border:1px solid #ddd;">Actual data</td></tr>
</table>

<h2>How It Stacks Up</h2>

<h3>Against the Competition</h3>
<p>Real comparison with specific competitors.</p>

<ol>
<li><strong>Competitor 1:</strong> How it compares with specifics</li>
<li><strong>Competitor 2:</strong> Advantages and disadvantages</li>
<li><strong>Market position:</strong> Where this fits in</li>
</ol>

<blockquote style="border-left:4px solid #007bff;padding-left:20px;margin:20px 0;font-style:italic;">
Key insight or important takeaway that deserves emphasis
</blockquote>

<h2>What Happens Next</h2>

<h3>Release Timeline</h3>
<p>When people can actually get this. Dates, availability, regions.</p>

<h3>Pricing</h3>
<p>Cost details. How it compares price-wise. Any deals or tiers.</p>

<h3>Looking Ahead</h3>
<p>Future plans. What the company says is coming. Industry expectations.</p>

<h2>The Bottom Line</h2>

<p>Synthesize everything. What's the verdict? Who should care? What's the smart take?</p>

<h3>Key Takeaways</h3>
<ul>
<li>First major point with specific impact</li>
<li>Second important conclusion with context</li>
<li>Third forward-looking takeaway</li>
</ul>

<p>Final paragraph. Bring it home with a strong conclusion or thought-provoking angle.</p>

=== HUMANIZATION RULES (CRITICAL) ===

1. CONVERSATIONAL TONE
   ✓ Use contractions: "it's", "that's", "won't", "they're", "we've"
   ✓ Ask rhetorical questions: "What does this mean?", "Why now?", "Who benefits?"
   ✓ Use transitions: "Here's the thing", "But wait", "What's more interesting"
   ✓ Personal touches: "Let's dig in", "Here's what matters", "Think about it"
   
2. VARIED SENTENCE STRUCTURE
   ✓ Mix short punchy sentences with longer detailed ones
   ✓ Start sentences differently - avoid patterns
   ✓ Use sentence fragments occasionally for emphasis
   ✗ Never use the same sentence pattern 3+ times in a row

3. NATURAL PHRASING
   ✓ "Apple says" not "Apple announced that"
   ✓ "The new chip is fast" not "The new chip exhibits enhanced performance"
   ✓ "This changes everything" not "This represents a paradigm shift"
   
4. FORBIDDEN AI PHRASES (NEVER USE):
   ✗ "It's worth noting"
   ✗ "Interestingly"
   ✗ "In conclusion"
   ✗ "Furthermore" / "Moreover" / "Additionally"
   ✗ "It's no secret that"
   ✗ "In today's digital landscape"
   ✗ "At the end of the day"
   ✗ "The bottom line is" (except in actual conclusion)
   ✗ "Dive deep into"
   ✗ "Shed light on"
   
5. HUMAN TOUCHES
   ✓ Use specific numbers and examples
   ✓ Include real-world comparisons
   ✓ Show, don't tell ("The phone felt premium" vs "The build quality is high")
   ✓ Use active voice exclusively
   ✓ Add casual asides in parentheses when appropriate
   
6. PARAGRAPH FLOW
   ✓ Keep paragraphs short (2-4 sentences usually)
   ✓ Each paragraph should flow naturally to the next
   ✓ Use transition sentences
   ✗ Avoid formulaic structures

7. WORD CHOICE
   ✓ Use simple, concrete words
   ✓ Prefer "use" over "utilize"
   ✓ Prefer "help" over "facilitate"
   ✓ Prefer "show" over "demonstrate"
   ✓ Mix formal and informal appropriately

8. FORMATTING
   <strong> for key terms (8-12 times)
   <em> for subtle emphasis (3-5 times)
   <ul> for features/lists (3-4 lists)
   <ol> for ranked/sequential items (2-3 lists)
   <table> for specs/data (1-2 tables)
   <blockquote> for key quotes/insights (2-3)

=== SEO SLUG ===
- Extract 5-6 main keywords from title
- Remove filler words (the, a, an, is, for, etc.)
- Lowercase with hyphens
- Under 60 characters

EXAMPLE: "Google Gemini 2.0 Beats GPT-4" → "google-gemini-20-beats-gpt4"

Return ONLY valid JSON. Make the article sound completely human - natural, engaging, and authentic. No robotic phrasing whatsoever."""

    payload = {
        "model": "gpt-4-turbo-preview",
        "messages": [
            {
                "role": "system", 
                "content": "You are an expert tech journalist who writes engaging, human-sounding articles. Your writing style is conversational yet informative, with varied sentence structures and natural phrasing. You avoid AI clichés and robotic language."
            },
            {
                "role": "user", 
                "content": prompt
            }
        ],
        "temperature": 0.8,  # Higher for more creative, human-like writing
        "max_tokens": 4000,
        "top_p": 0.95,
        "frequency_penalty": 0.3,  # Reduce repetition
        "presence_penalty": 0.3,   # Encourage diverse topics
        "response_format": {"type": "json_object"}
    }
    
    try:
        print(f"   ✍️ Generating article with OpenAI GPT-4...", flush=True)
        response = requests.post(url, headers=headers, json=payload, timeout=120)
        response.raise_for_status()
        
        data = response.json()
        content = data['choices'][0]['message']['content']
        article = extract_json(content)
        
        if article and 'content' in article:
            word_count = len(article['content'].split())
            print(f"   ✅ Article generated: {word_count} words", flush=True)
            
            # Fetch image if keywords provided
            if 'image_keywords' in article and article['image_keywords']:
                article['featured_image'] = fetch_image_from_pexels(article['image_keywords'])
            else:
                # Use title for image search as fallback
                article['featured_image'] = fetch_image_from_pexels(title[:50])
            
            # Post-process: Additional humanization pass
            article['content'] = apply_humanization_polish(article['content'])
            
            return article
        else:
            raise ValueError("Invalid article structure from OpenAI")
            
    except requests.exceptions.Timeout:
        print(f"   ⏱️ OpenAI request timeout", flush=True)
        return create_fallback_article(title, research_content, category)
    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code
        print(f"   ⚠️ OpenAI HTTP {status_code}", flush=True)
        
        if status_code == 429:
            print(f"      Rate limit exceeded", flush=True)
        elif status_code == 401:
            print(f"      Invalid API key", flush=True)
        
        return create_fallback_article(title, research_content, category)
    except Exception as e:
        print(f"   ⚠️ OpenAI generation failed: {str(e)[:150]}", flush=True)
        return create_fallback_article(title, research_content, category)


# ==================== HUMANIZATION POLISH ====================

def apply_humanization_polish(content):
    """
    Apply final humanization touches to content
    Removes common AI patterns and adds natural variation
    
    Args:
        content: HTML content string
    
    Returns:
        str: Polished content
    """
    
    # Remove common AI tell-tale phrases (case-insensitive)
    ai_phrases = [
        r"it'?s? worth noting that",
        r"interestingly,?",
        r"in conclusion,?",
        r"furthermore,?",
        r"moreover,?",
        r"additionally,?",
        r"it'?s? no secret that",
        r"in today'?s? digital landscape",
        r"at the end of the day,?",
        r"dive deep(?:er)? into",
        r"sheds? light on",
        r"it'?s? important to (?:note|remember|understand) that",
        r"one of the key (?:aspects|features|benefits) is"
    ]
    
    for phrase in ai_phrases:
        # Replace with nothing or minimal transition
        content = re.sub(phrase, '', content, flags=re.IGNORECASE)
    
    # Clean up any double spaces or punctuation issues
    content = re.sub(r'\s+', ' ', content)  # Multiple spaces → single space
    content = re.sub(r'\s+([.,;:!?])', r'\1', content)  # Space before punctuation
    content = re.sub(r'([.,;:!?])([A-Z])', r'\1 \2', content)  # Add space after punctuation
    
    # Clean up extra paragraph breaks
    content = re.sub(r'</p>\s*<p>', '</p>\n<p>', content)
    
    return content.strip()


# ==================== FALLBACK ARTICLE ====================

def create_fallback_article(title, research_content, category):
    """
    Create basic article if AI generation fails
    Uses research content to create a simple but complete article
    
    Args:
        title: Article title
        research_content: Research data from Gemini
        category: Article category
    
    Returns:
        dict: Basic article structure
    """
    from config import create_slug_from_text
    
    # Extract first 200 chars for description
    description_text = research_content[:300] if research_content else title
    description_text = re.sub(r'<[^>]+>', '', description_text)  # Remove HTML
    description = ' '.join(description_text.split()[:30])  # First 30 words
    
    if not description.endswith('.'):
        # Find last sentence end
        last_period = description.rfind('.')
        if last_period > 50:
            description = description[:last_period + 1]
        else:
            description += '...'
    
    # Create basic HTML content
    research_excerpt = research_content[:2000] if research_content else "Please refer to the source for full details."
    
    content = f"""<p><strong>{title}</strong></p>

<p>{description}</p>

<h2>Overview</h2>
<p>{research_excerpt}</p>

<h2>Key Points</h2>
<ul>
<li>This story is developing in the {category} category</li>
<li>More detailed information available at the source link</li>
<li>Check back for updates as more details emerge</li>
</ul>

<h2>What This Means</h2>
<p>This development has significant implications for the tech industry. For comprehensive coverage and the latest updates, please refer to the original source article.</p>"""
    
    return {
        'title': title,
        'short_description': description[:250],
        'content': content,
        'slug': create_slug_from_text(title),
        'featured_image': None
    }


# ==================== MAIN WORKFLOW ====================

def create_complete_article(title, category):
    """
    Complete article generation workflow with research + generation + humanization
    
    Args:
        title: Article title
        category: Article category
    
    Returns:
        dict: Complete article data
    """
    print(f"\n{'='*60}", flush=True)
    print(f"📝 Creating article: {title[:60]}...", flush=True)
    print(f"📁 Category: {category}", flush=True)
    print(f"{'='*60}", flush=True)
    
    # Step 1: Deep research with Gemini 2.0
    print(f"\n[1/3] Research Phase", flush=True)
    research = deep_research_with_gemini(title)
    time.sleep(2)  # Rate limit buffer
    
    # Step 2: Generate article with OpenAI
    print(f"\n[2/3] Generation Phase", flush=True)
    article = generate_article_with_openai(title, research, category)
    time.sleep(1)
    
    # Step 3: Final validation
    print(f"\n[3/3] Validation Phase", flush=True)
    
    if article and article.get('content'):
        word_count = len(article['content'].split())
        
        # Quality checks
        has_headings = '<h2>' in article['content']
        has_lists = '<ul>' in article['content'] or '<ol>' in article['content']
        is_long_enough = word_count >= 800
        
        print(f"   Word count: {word_count}", flush=True)
        print(f"   Has headings: {'✅' if has_headings else '❌'}", flush=True)
        print(f"   Has lists: {'✅' if has_lists else '❌'}", flush=True)
        print(f"   Length OK: {'✅' if is_long_enough else '⚠️'}", flush=True)
        
        if not is_long_enough:
            print(f"   ⚠️ Article may be shorter than ideal", flush=True)
    
    print(f"{'='*60}\n", flush=True)
    
    return article


# ==================== ALTERNATIVE: GEMINI-ONLY GENERATION ====================

@retry_on_api_error
def generate_article_with_gemini_only(title, research_content, category):
    """
    Fallback: Generate article using only Gemini (if OpenAI unavailable)
    This is a backup option if OpenAI key is invalid or rate-limited
    
    Args:
        title: Article title
        research_content: Research data
        category: Article category
    
    Returns:
        dict: Article data
    """
    if not GEMINI_API_KEY:
        return create_fallback_article(title, research_content, category)
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-thinking-exp:generateContent?key={GEMINI_API_KEY}"
    
    prompt = f"""You are writing a tech article based on research.

RESEARCH DATA:
{research_content[:10000]}

Write a complete article in JSON format:
{{
  "title": "60-70 character title",
  "short_description": "200 character description",
  "slug": "seo-friendly-slug",
  "content": "Full HTML article with h2, h3, p, ul, ol, strong tags",
  "image_keywords": "keywords for image"
}}

Make it 1200+ words, natural sounding, and engaging. Return ONLY valid JSON."""
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 8000,
            "responseMimeType": "application/json"
        }
    }
    
    try:
        response = requests.post(url, json=payload, timeout=90)
        response.raise_for_status()
        
        data = response.json()
        content_text = data['candidates'][0]['content']['parts'][0]['text']
        article = extract_json(content_text)
        
        if article and 'content' in article:
            return article
        
    except Exception as e:
        print(f"   ⚠️ Gemini generation failed: {str(e)[:100]}", flush=True)
    
    return create_fallback_article(title, research_content, category)