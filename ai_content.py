"""
AI Content Generation Module
Uses Gemini Deep Research + OpenAI for high-quality content
"""
import requests
import json
import time
from config import GEMINI_API_KEY, OPENAI_API_KEY, extract_json, fetch_image_from_pexels


# ==================== GEMINI DEEP RESEARCH ====================

def deep_research_with_gemini(title):
    """
    Use Gemini Deep Research to gather comprehensive information
    
    Args:
        title: Article title to research
    
    Returns:
        str: Deep research results (comprehensive text)
    """
    if not GEMINI_API_KEY:
        print(f"   ⚠️ No Gemini API key", flush=True)
        return f"Research topic: {title}"
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-thinking-exp:generateContent?key={GEMINI_API_KEY}"
    
    prompt = f"""Research this tech news topic comprehensively:

TOPIC: {title}

Provide:
1. What happened (main event with details)
2. Background and context
3. Technical details and specifications
4. Industry impact and implications
5. Expert opinions and perspectives
6. Comparisons with competitors
7. Future outlook and predictions
8. Key statistics and data points

Be thorough and factual. Include specific numbers, dates, and names where available."""

    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 8000
        }
    }
    
    try:
        print(f"   🔬 Deep research with Gemini...", flush=True)
        response = requests.post(url, json=payload, timeout=90)
        response.raise_for_status()
        
        data = response.json()
        research_text = data['candidates'][0]['content']['parts'][0]['text']
        
        word_count = len(research_text.split())
        print(f"   ✅ Research complete: {word_count} words", flush=True)
        
        return research_text
        
    except Exception as e:
        print(f"   ⚠️ Gemini research failed: {e}", flush=True)
        return f"Research topic: {title}\n\nPlease refer to the source for detailed information."


# ==================== OPENAI CONTENT GENERATION ====================

def generate_article_with_openai(title, research_content, category):
    """
    Generate comprehensive article using OpenAI ChatGPT
    
    Args:
        title: Article title
        research_content: Deep research results from Gemini
        category: Article category
    
    Returns:
        dict: {
            'title': str,
            'short_description': str,
            'content': str (HTML),
            'slug': str,
            'featured_image': str
        }
    """
    if not OPENAI_API_KEY or OPENAI_API_KEY == "YOUR_OPENAI_API_KEY_HERE":
        print(f"   ⚠️ No OpenAI API key, using fallback", flush=True)
        return create_fallback_article(title, research_content, category)
    
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    
    prompt = f"""You are a professional tech journalist writing for a major publication.

RESEARCH DATA:
{research_content[:15000]}

WRITE A COMPREHENSIVE ARTICLE:

OUTPUT AS JSON:
{{
  "title": "60-70 character engaging title",
  "short_description": "200 character SEO description",
  "slug": "seo-friendly-slug-5-6-keywords",
  "content": "Full HTML article (see structure below)",
  "image_keywords": "keywords for image search"
}}

TITLE REQUIREMENTS (60-70 chars):
- Natural and engaging
- Include key details
- No clickbait
- Active voice

SHORT DESCRIPTION (200 chars):
- 2 sentences
- First: What happened
- Second: Why it matters
- Include key stat or detail

CONTENT STRUCTURE (1500-2000 words):

<p><strong>Hook paragraph:</strong> Start with the most compelling fact. Make readers want to continue. 2-3 sentences.</p>

<h2>Context-Specific Main Heading (NOT generic)</h2>
<p>Explain what happened. Use short paragraphs (2-3 sentences each).</p>

<h3>Relevant Subheading</h3>
<p>Break down details. Include specific numbers and facts.</p>

<ul>
<li>Bullet point with specific detail</li>
<li>Another key feature or fact</li>
<li>Third important point</li>
</ul>

<h2>Why This Matters to Readers</h2>
<p>Real-world impact. How does this affect users?</p>

<h3>For Consumers</h3>
<p>Practical implications.</p>

<h3>For Industry</h3>
<p>Business impact.</p>

<h2>Technical Deep Dive (if applicable)</h2>

<h3>Specifications</h3>
<table style="width:100%;border-collapse:collapse;margin:20px 0;">
<tr style="background:#f5f5f5;"><th style="padding:10px;text-align:left;border:1px solid #ddd;">Feature</th><th style="padding:10px;text-align:left;border:1px solid #ddd;">Details</th></tr>
<tr><td style="padding:10px;border:1px solid #ddd;">Key spec 1</td><td style="padding:10px;border:1px solid #ddd;">Value</td></tr>
<tr><td style="padding:10px;border:1px solid #ddd;">Key spec 2</td><td style="padding:10px;border:1px solid #ddd;">Value</td></tr>
</table>

<h2>How It Compares</h2>
<p>Competition analysis.</p>

<ol>
<li><strong>Competitor 1:</strong> Comparison details</li>
<li><strong>Competitor 2:</strong> Comparison details</li>
<li><strong>Market position:</strong> Where this stands</li>
</ol>

<h2>Expert Perspectives</h2>
<blockquote style="border-left:4px solid #007bff;padding-left:20px;margin:20px 0;font-style:italic;">
Key insight or important takeaway
</blockquote>

<h3>Industry Analysis</h3>
<p>Broader context.</p>

<h2>What Happens Next</h2>

<h3>Timeline</h3>
<p>Upcoming dates and milestones.</p>

<h3>Future Implications</h3>
<p>Long-term impact.</p>

<h2>Summary: Key Takeaways</h2>
<ul>
<li>Main point 1 with specific detail</li>
<li>Main point 2 with impact</li>
<li>Main point 3 with forward look</li>
</ul>

<p>Final paragraph. Conclude with perspective or question.</p>

CRITICAL REQUIREMENTS:

1. LENGTH: 1500-2000 words minimum

2. HEADINGS: 6-8 H2 headings, 4-5 H3 subheadings
   - Make them specific to content
   - Use keywords naturally
   - NOT generic (no "Introduction", "Conclusion")

3. FORMATTING:
   - <strong> for key terms (8-12 times)
   - <em> for emphasis (3-5 times)
   - <ul><li> for features/points (3-4 lists)
   - <ol><li> for steps/rankings (2-3 lists)
   - <table> for specs/comparisons (1-2 tables)
   - <blockquote> for key quotes (2-3)
   - Short paragraphs (2-4 sentences)

4. HUMANIZATION (CRITICAL):
   ✓ Use contractions: "it's", "that's", "won't", "they're"
   ✓ Vary sentence length dramatically
   ✓ Ask questions: "What does this mean?", "Why now?"
   ✓ Use transitions: "But here's the thing", "What's more"
   ✓ Personal touch: "Let's break this down", "Here's what matters"
   ✓ Specific numbers and examples
   ✓ Active voice always

5. ABSOLUTELY FORBIDDEN:
   ❌ "It's worth noting"
   ❌ "Interestingly"
   ❌ "In conclusion"
   ❌ "Furthermore"
   ❌ "Moreover"
   ❌ "Additionally"
   ❌ Generic intro paragraphs
   ❌ Passive voice
   ❌ Starting sentences the same way

6. SEO SLUG:
   - 5-6 main keywords from title/description
   - Lowercase, hyphens
   - No filler words
   - Under 60 characters

Return ONLY valid JSON. Make it sound completely human."""

    payload = {
        "model": "gpt-4-turbo-preview",  # or "gpt-4o" if available
        "messages": [
            {"role": "system", "content": "You are an expert tech journalist who writes engaging, human-sounding articles."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 4000,
        "response_format": {"type": "json_object"}
    }
    
    try:
        print(f"   ✍️  Generating article with OpenAI...", flush=True)
        response = requests.post(url, headers=headers, json=payload, timeout=120)
        response.raise_for_status()
        
        data = response.json()
        content = data['choices'][0]['message']['content']
        article = extract_json(content)
        
        if article and 'content' in article:
            word_count = len(article['content'].split())
            print(f"   ✅ Article generated: {word_count} words", flush=True)
            
            # Fetch image if keywords provided
            if 'image_keywords' in article:
                article['featured_image'] = fetch_image_from_pexels(article['image_keywords'])
            
            return article
        else:
            raise ValueError("Invalid article structure")
            
    except Exception as e:
        print(f"   ⚠️ OpenAI generation failed: {e}", flush=True)
        return create_fallback_article(title, research_content, category)


# ==================== FALLBACK ARTICLE ====================

def create_fallback_article(title, research_content, category):
    """Create basic article if AI generation fails"""
    
    # Extract first 200 chars for description
    description = research_content[:200].strip()
    if not description.endswith('.'):
        description += '...'
    
    # Create basic HTML
    content = f"""<p><strong>{title}</strong></p>

<p>{description}</p>

<h2>Key Information</h2>
{research_content[:1000]}

<h2>What This Means</h2>
<p>This development has significant implications for the tech industry. For more detailed information and updates, please refer to the original source.</p>

<h2>Summary</h2>
<ul>
<li>This is a developing story in {category}</li>
<li>More details available at the source link</li>
<li>We'll update as more information becomes available</li>
</ul>"""
    
    from config import create_slug_from_text
    
    return {
        'title': title,
        'short_description': description,
        'content': content,
        'slug': create_slug_from_text(title),
        'featured_image': None
    }


# ==================== MAIN WORKFLOW ====================

def create_complete_article(title, category):
    """
    Complete article generation workflow
    
    Args:
        title: Article title
        category: Article category
    
    Returns:
        dict: Complete article data
    """
    print(f"\n{'='*60}", flush=True)
    print(f"📝 Creating article: {title[:60]}...", flush=True)
    print(f"{'='*60}", flush=True)
    
    # Step 1: Deep research
    research = deep_research_with_gemini(title)
    time.sleep(2)  # Rate limit buffer
    
    # Step 2: Generate article
    article = generate_article_with_openai(title, research, category)
    
    print(f"{'='*60}\n", flush=True)
    
    return article