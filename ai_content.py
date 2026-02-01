"""
Advanced Multi-AI Content Generation
Creates 1000+ word articles with rich formatting and zero AI detection
"""
import json
import requests
import time
import re
from config import (
    GROQ_API_KEY, OPENROUTER_API_KEY, TOGETHER_API_KEY, 
    HUGGINGFACE_API_KEY, GEMINI_API_KEY, extract_json,
    fetch_relevant_image
)

# ==================== AI CLIENTS ====================

class GroqClient:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://api.groq.com/openai/v1/chat/completions"
        self.model = "llama-3.3-70b-versatile"
    
    def generate(self, prompt, temperature=0.7, max_tokens=4000, json_mode=False):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        
        try:
            response = requests.post(self.base_url, headers=headers, json=payload, timeout=45)
            response.raise_for_status()
            return response.json()['choices'][0]['message']['content']
        except Exception as e:
            print(f"⚠️ Groq error: {e}", flush=True)
            return None


class TogetherAIClient:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://api.together.xyz/v1/chat/completions"
        self.model = "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo"
    
    def generate(self, prompt, temperature=0.7, max_tokens=4000, json_mode=False):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        
        try:
            response = requests.post(self.base_url, headers=headers, json=payload, timeout=45)
            response.raise_for_status()
            return response.json()['choices'][0]['message']['content']
        except Exception as e:
            print(f"⚠️ Together error: {e}", flush=True)
            return None


class OpenRouterClient:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://openrouter.ai/api/v1/chat/completions"
        self.model = "nousresearch/hermes-3-llama-3.1-405b:free"
    
    def generate(self, prompt, temperature=0.7, max_tokens=4000, json_mode=False):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://gadgeek.in"
        }
        
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        
        try:
            response = requests.post(self.base_url, headers=headers, json=payload, timeout=45)
            response.raise_for_status()
            return response.json()['choices'][0]['message']['content']
        except Exception as e:
            print(f"⚠️ OpenRouter error: {e}", flush=True)
            return None


class GeminiClient:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"
        self.model = "gemini-1.5-flash-8b"
    
    def generate(self, prompt, temperature=0.7, max_tokens=4000, json_mode=False):
        url = f"{self.base_url}/{self.model}:generateContent?key={self.api_key}"
        
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens
            }
        }
        
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"
        
        try:
            response = requests.post(url, json=payload, timeout=45)
            response.raise_for_status()
            return response.json()['candidates'][0]['content']['parts'][0]['text']
        except Exception as e:
            print(f"⚠️ Gemini error: {e}", flush=True)
            return None


# Initialize clients
groq = GroqClient(GROQ_API_KEY) if GROQ_API_KEY and GROQ_API_KEY != "YOUR_GROQ_API_KEY_HERE" else None
together = TogetherAIClient(TOGETHER_API_KEY) if TOGETHER_API_KEY and TOGETHER_API_KEY != "YOUR_TOGETHER_API_KEY_HERE" else None
openrouter = OpenRouterClient(OPENROUTER_API_KEY) if OPENROUTER_API_KEY and OPENROUTER_API_KEY != "YOUR_OPENROUTER_API_KEY_HERE" else None
gemini = GeminiClient(GEMINI_API_KEY) if GEMINI_API_KEY else None

# ==================== AI ROUTING ====================

def get_ai_client(priority="primary"):
    """Smart AI routing with fallbacks"""
    if priority == "primary":
        if groq:
            return groq, "Groq"
        if together:
            return together, "Together"
        if gemini:
            return gemini, "Gemini"
        if openrouter:
            return openrouter, "OpenRouter"
    
    elif priority == "secondary":
        if openrouter:
            return openrouter, "OpenRouter"
        if together:
            return together, "Together"
        if gemini:
            return gemini, "Gemini"
        if groq:
            return groq, "Groq"
    
    # Fallback chain
    for client, name in [(groq, "Groq"), (together, "Together"), (gemini, "Gemini"), (openrouter, "OpenRouter")]:
        if client:
            return client, name
    
    return None, None


# ==================== DEEP ANALYSIS ====================

def analyze_news_story(content_data):
    """Comprehensive content analysis"""
    client, name = get_ai_client("primary")
    
    if not client:
        return create_fallback_analysis(content_data)
    
    prompt = f"""Analyze this tech article comprehensively. Output ONLY valid JSON.

ARTICLE:
Title: {content_data['title']}
Content: {content_data['text'][:8000]}

ANALYZE DEEPLY:
1. What's the actual news? (not just title)
2. Why does this matter to readers?
3. Who are the key players?
4. What's the broader context/trend?
5. What category does this fit? (smartphones, AI, gaming, software, hardware, business, privacy, etc.)
6. Urgency level?
7. Target audience?
8. What questions would readers have?
9. What are the implications?
10. Any controversies or debates?

OUTPUT JSON:
{{
  "main_event": "Precise description of what happened",
  "significance": "Why readers should care",
  "key_players": ["Apple", "Google"],
  "story_type": "breaking_news|product_launch|industry_analysis|controversy|rumor|review|tutorial|opinion",
  "category": "smartphones|AI|gaming|software|hardware|business|privacy|security|wearables|laptops",
  "urgency_score": 7,
  "target_audience": "tech_enthusiasts|general_consumers|developers|gamers|business_leaders",
  "unique_angle": "Most interesting perspective",
  "context": "Bigger industry trend or backstory",
  "reader_questions": ["Question 1", "Question 2", "Question 3"],
  "implications": "Future impact or consequences",
  "controversies": "Any debates or opposing views",
  "key_stats": ["Stat 1", "Stat 2"],
  "meta_description": "SEO-friendly 150 char description"
}}

Be thorough. Return only JSON."""

    print(f"   🧠 Deep analysis with {name}...", flush=True)
    
    try:
        response = client.generate(prompt, temperature=0.3, max_tokens=2000, json_mode=True)
        if response:
            analysis = extract_json(response)
            if analysis and 'main_event' in analysis:
                return analysis
    except:
        pass
    
    return create_fallback_analysis(content_data)


def create_fallback_analysis(content_data):
    return {
        "main_event": content_data['title'],
        "significance": "Important tech development",
        "key_players": [],
        "story_type": "industry_analysis",
        "category": "technology",
        "urgency_score": 5,
        "target_audience": "tech_enthusiasts",
        "unique_angle": "Latest update",
        "context": "Ongoing tech industry development",
        "reader_questions": ["What does this mean?", "How does this work?", "When is it available?"],
        "implications": "Could impact the tech industry",
        "controversies": "",
        "key_stats": [],
        "meta_description": content_data['title'][:150]
    }


# ==================== HEADLINE GENERATION ====================

def generate_humanized_headline_and_summary(content_data, analysis):
    """Generate click-worthy headlines - NO TRUNCATION"""
    
    client1, name1 = get_ai_client("primary")
    
    if not client1:
        return create_fallback_content(analysis)
    
    prompt1 = f"""Create an engaging headline and summary. Output JSON.

CONTEXT:
Event: {analysis['main_event']}
Impact: {analysis['significance']}
Players: {', '.join(analysis['key_players'][:3]) if analysis['key_players'] else 'Tech companies'}
Category: {analysis.get('category', 'technology')}

SOURCE:
{content_data['text'][:3000]}

CREATE (JSON):
1. HEADLINE - Natural, engaging, NO LENGTH LIMIT (aim for 50-70 chars but can be longer if needed for clarity)
2. SUMMARY - 2-3 sentences, 200-250 chars, specific and engaging

RULES FOR HEADLINE:
- Make it conversational and natural
- Include key details (numbers, names, products)
- Create curiosity without clickbait
- Use active voice
- CAN be longer than 65 chars if story requires it for clarity
- NO generic words: "Latest", "New", "Just", "Breaking"

RULES FOR SUMMARY:
- First sentence: What happened (with specifics)
- Second sentence: Why it matters or impact
- Optional third: Additional key detail
- Use numbers and facts
- Be direct and confident

BANNED PHRASES:
❌ "The article discusses"
❌ "According to reports"
❌ "It's worth noting"
❌ "Interestingly"
❌ "In this article"

GOOD EXAMPLES:
{{"title": "Apple's iPhone 16 Pro Ditches Physical Buttons for Haptic Touch", "summary": "Apple is removing all physical buttons from the iPhone 16 Pro lineup, replacing them with capacitive touch sensors. The design change could make it the most water-resistant iPhone ever while reducing manufacturing costs by 15%."}}

{{"title": "Tesla Slashes Model 3 Prices to $35,000 After Missing Q1 Sales Targets", "summary": "Tesla dropped Model 3 prices by $7,000 following weak first-quarter deliveries. This puts the base model below Chevy's Bolt and directly challenges traditional automakers in the budget EV space."}}

Return ONLY JSON with "title" and "summary". Make it sound completely human."""

    print(f"   ✍️  Generating with {name1}...", flush=True)
    
    try:
        response1 = client1.generate(prompt1, temperature=0.7, max_tokens=500, json_mode=True)
        if not response1:
            return create_fallback_content(analysis)
        
        draft = extract_json(response1)
        if not draft or 'title' not in draft:
            return create_fallback_content(analysis)
        
        # Try humanization with secondary AI
        client2, name2 = get_ai_client("secondary")
        
        if client2 and name2 != name1:
            print(f"   🎨 Humanizing with {name2}...", flush=True)
            
            prompt2 = f"""Make this more natural and human-sounding. Output JSON.

CURRENT:
Title: {draft['title']}
Summary: {draft['summary']}

IMPROVE:
- Sound like a human journalist wrote it
- Use contractions (it's, that's, won't)
- Keep the facts and numbers
- Make it engaging
- NO TRUNCATION - full title is fine

Output JSON: {{"title": "...", "summary": "..."}}"""
            
            response2 = client2.generate(prompt2, temperature=0.6, max_tokens=400, json_mode=True)
            if response2:
                refined = extract_json(response2)
                if refined and 'title' in refined:
                    draft = refined
        
        title = draft.get('title', '').strip()
        summary = draft.get('summary', '')[:250]
        
        # Check for AI-isms
        ai_phrases = ["the article discusses", "according to reports", "it's worth noting", "in this article"]
        if any(phrase in summary.lower() for phrase in ai_phrases):
            print(f"   ⚠️ AI phrase detected, using fallback", flush=True)
            return create_fallback_content(analysis)
        
        # NO TRUNCATION - return full title
        return {'title': title, 'summary': summary}
        
    except Exception as e:
        print(f"   ⚠️ Generation error: {e}", flush=True)
        return create_fallback_content(analysis)


def create_fallback_content(analysis):
    title = analysis['main_event']
    summary = f"{analysis['main_event'][:120]}. {analysis['significance'][:120]}."
    return {'title': title, 'summary': summary[:250]}


# ==================== CONTINUE IN NEXT FILE DUE TO LENGTH ====================

# ==================== ADVANCED ARTICLE WRITING (1000+ WORDS) ====================

def write_full_article(title, source_text, analysis):
    """
    Write comprehensive 1000-1500 word article with rich formatting
    Uses multiple sections, bullets, numbers, varied headings
    """
    
    client1, name1 = get_ai_client("primary")
    
    if not client1:
        return create_fallback_article(title, analysis)
    
    # Extract keywords for image
    keywords = ' '.join(analysis.get('key_players', [])[:2] + [analysis.get('category', 'technology')])
    image_url = fetch_relevant_image(keywords)
    
    prompt1 = f"""You are a professional tech journalist writing for a major publication. Write a comprehensive, engaging article that sounds completely human.

ASSIGNMENT:
Title: {title}
Category: {analysis.get('category', 'technology')}
Story Type: {analysis.get('story_type', 'news')}
Target Audience: {analysis.get('target_audience', 'tech enthusiasts')}

CONTEXT:
Main Event: {analysis['main_event']}
Significance: {analysis['significance']}
Context/Trend: {analysis.get('context', 'Tech development')}
Key Players: {', '.join(analysis.get('key_players', [])[:3])}
Reader Questions: {', '.join(analysis.get('reader_questions', [])[:3])}
Implications: {analysis.get('implications', 'Industry impact')}

SOURCE MATERIAL:
{source_text[:12000]}

WRITE A 1000-1500 WORD ARTICLE IN HTML:

CRITICAL REQUIREMENTS:

1. LENGTH: Minimum 1000 words, target 1200-1500 words

2. STRUCTURE (Use these sections, adapt headings to content):
   - Opening paragraph (no heading) - Strong hook, main news
   - Section 1: What's Actually Happening (adapt heading to topic)
   - Section 2: Why This Matters / Impact / Implications (adapt heading)
   - Section 3: Technical Details / How It Works / Specifications (if applicable)
   - Section 4: Industry Context / Competition / Market Impact
   - Section 5: What's Next / Future Outlook / Timeline
   - Conclusion paragraph

3. HEADINGS - ADAPT TO CONTENT:
   Don't use generic H2s. Make them specific to the story:
   
   Examples:
   For iPhone: <h2>Three Major Design Changes Coming to iPhone 16</h2>
   For Tesla: <h2>How Tesla's Price Cut Shakes Up the EV Market</h2>
   For AI: <h2>Breaking Down Google's New AI Capabilities</h2>
   For Privacy: <h2>What This Means for Your Data Privacy</h2>
   
4. FORMATTING (CRITICAL for human feel):
   - Use <strong> for key terms (5-10 times)
   - Use <em> for emphasis (2-3 times)
   - Include numbered lists <ol><li> for steps, timelines, rankings
   - Include bullet points <ul><li> for features, pros/cons, key points
   - Use <blockquote> for important quotes or key takeaways (1-2 times)
   - Short paragraphs (2-4 sentences each)
   - Vary paragraph length for rhythm

5. WRITING STYLE - SOUND HUMAN:
   ✓ Use contractions: "it's", "that's", "won't", "they're"
   ✓ Vary sentence length: Mix short punchy sentences with longer explanatory ones.
   ✓ Ask rhetorical questions occasionally
   ✓ Use everyday comparisons: "That's like comparing a bicycle to a sports car"
   ✓ Include specific numbers, percentages, dates
   ✓ Use transition words naturally: "But here's the thing", "What's more"
   ✓ Show personality: "Let's be honest", "The real kicker?", "Impressive, right?"

6. ABSOLUTELY FORBIDDEN (AI Detection Triggers):
   ❌ "It's worth noting that"
   ❌ "Interestingly,"
   ❌ "Notably,"
   ❌ "In conclusion,"
   ❌ "Furthermore,"
   ❌ "Moreover,"
   ❌ "Additionally,"
   ❌ "It's important to highlight"
   ❌ "This development underscores"
   ❌ "The significance cannot be overstated"
   ❌ "In today's rapidly evolving"
   ❌ "As we delve deeper"
   ❌ Starting consecutive sentences with "The company", "The device", "The feature"

7. INCLUDE THESE ELEMENTS:
   - At least 2 numbered lists
   - At least 2 bullet lists
   - At least 1 blockquote
   - Specific statistics or numbers (minimum 5)
   - Real-world examples or comparisons
   - Questions answered from analysis

EXAMPLE STRUCTURE:

<p>Strong opening paragraph with the news. Include the most important detail right away. No fluff.</p>

<p>Context paragraph. Explain briefly why this matters or what led to this moment.</p>

<h2>Three Ways This Changes Everything</h2>

<p>Short intro to the list.</p>

<ol>
<li><strong>First Major Point</strong>: Detailed explanation with specifics. Include numbers or examples. Make it conversational.</li>
<li><strong>Second Major Point</strong>: Another key detail. Maybe compare to something familiar. Add a stat if you have one.</li>
<li><strong>Third Major Point</strong>: The kicker. What makes this really significant?</li>
</ol>

<p>Transition paragraph. Connect the dots between sections.</p>

<h2>Why Tech Companies Are Worried</h2>

<p>Explain the broader impact. Use real examples.</p>

<blockquote>The key takeaway: [Important insight or implication]</blockquote>

<p>More analysis. Include specific details:</p>

<ul>
<li>Key point one with specific detail</li>
<li>Key point two with a number or stat</li>
<li>Key point three with real impact</li>
</ul>

<h2>What Happens Next</h2>

<p>Timeline or future expectations. Be specific about dates if known.</p>

<p>Final paragraph. Brief wrap-up. Maybe end with a forward-looking statement or question.</p>

NOW WRITE THE ARTICLE:
- Minimum 1000 words
- Rich formatting (lists, bold, emphasis, quotes)
- Headings adapted to the specific content
- Completely human-sounding
- Zero AI detection phrases
- Return ONLY HTML, no markdown, no code blocks"""

    print(f"   📝 Writing 1000+ word article with {name1}...", flush=True)
    
    try:
        response1 = client1.generate(prompt1, temperature=0.65, max_tokens=4000)
        if not response1:
            return create_fallback_article(title, analysis)
        
        article = response1.replace("```html", "").replace("```", "").strip()
        
        # Try secondary AI for humanization
        client2, name2 = get_ai_client("secondary")
        
        if client2 and name2 != name1 and len(article) > 500:
            print(f"   🎨 Humanizing with {name2}...", flush=True)
            
            prompt2 = f"""Remove ALL AI-sounding language. Make this article sound like a human journalist wrote it.

ARTICLE:
{article[:8000]}

FIX THESE:
1. Remove phrases like "It's worth noting", "Interestingly", "Furthermore"
2. Add contractions naturally (it's, that's, won't)
3. Vary sentence structure
4. Make it conversational
5. Keep all formatting, lists, and HTML structure

Return ONLY the improved HTML. No explanations."""
            
            response2 = client2.generate(prompt2, temperature=0.5, max_tokens=4000)
            if response2:
                refined = response2.replace("```html", "").replace("```", "").strip()
                if len(refined) > 500:
                    article = refined
        
        # Final cleanup - remove AI-isms
        ai_patterns = [
            r"It'?s worth noting that\s+",
            r"Interestingly,?\s+",
            r"Notably,?\s+",
            r"In conclusion,?\s+",
            r"Furthermore,?\s+",
            r"Moreover,?\s+",
            r"Additionally,?\s+",
            r"It'?s important to highlight\s+",
            r"This development underscores\s+",
            r"The significance.*?cannot be overstated\.?",
            r"In today'?s rapidly evolving\s+",
            r"As we delve deeper\s+",
        ]
        
        for pattern in ai_patterns:
            article = re.sub(pattern, "", article, flags=re.IGNORECASE)
        
        # Ensure minimum length
        word_count = len(article.split())
        print(f"   📊 Article length: {word_count} words", flush=True)
        
        if word_count < 800:
            print(f"   ⚠️ Article too short, using fallback", flush=True)
            return create_fallback_article(title, analysis)
        
        # Add image if available
        if image_url:
            article = f'<img src="{image_url}" alt="{title}" style="width:100%;max-width:1200px;height:auto;margin-bottom:20px;"/>\n\n' + article
        
        return article
        
    except Exception as e:
        print(f"   ⚠️ Article writing failed: {e}", flush=True)
        return create_fallback_article(title, analysis)


def create_fallback_article(title, analysis):
    """Enhanced fallback with better structure"""
    return f"""<p><strong>{title}</strong></p>

<p>{analysis.get('main_event', 'This is a developing story in the tech industry.')} {analysis.get('significance', 'This development has significant implications.')}</p>

<h2>What We Know So Far</h2>

<p>{analysis.get('context', 'This development is part of broader industry trends.')}</p>

<ul>
<li>Key players involved: {', '.join(analysis.get('key_players', ['Various tech companies']))}</li>
<li>Category: {analysis.get('category', 'Technology')}</li>
<li>Impact level: {analysis.get('urgency_score', 5)}/10</li>
</ul>

<h2>Why This Matters</h2>

<p>{analysis.get('implications', 'This could have significant implications for the tech industry and consumers.')}</p>

<h2>What Happens Next</h2>

<p>This story is developing. Check the source link for the most up-to-date information and additional details.</p>"""