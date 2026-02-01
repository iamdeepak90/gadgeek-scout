"""
Multi-AI Content Generation - Maximum Humanization
Uses 3 different AI models for collaborative refinement
"""
import json
import requests
import time
import re

from config_multi_ai import (
    GROQ_API_KEY, OPENROUTER_API_KEY, GEMINI_API_KEY,
    PRIMARY_AI, SECONDARY_AI, FALLBACK_AI, extract_json
)

# ==================== AI CLIENTS ====================

class GroqClient:
    """Groq - Very fast and generous free tier"""
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://api.groq.com/openai/v1/chat/completions"
        # Best models: llama-3.3-70b-versatile, llama-3.1-70b-versatile
        self.model = "llama-3.3-70b-versatile"
    
    def generate(self, prompt, temperature=0.7, json_mode=False):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": 2000
        }
        
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        
        try:
            response = requests.post(self.base_url, headers=headers, json=payload, timeout=30)
            response.raise_for_status()
            return response.json()['choices'][0]['message']['content']
        except Exception as e:
            print(f"⚠️ Groq error: {e}", flush=True)
            return None


class OpenRouterClient:
    """OpenRouter - Multiple free models"""
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://openrouter.ai/api/v1/chat/completions"
        # Free models: google/gemini-flash-1.5-8b, meta-llama/llama-3.2-3b-instruct:free
        self.model = "google/gemini-flash-1.5-8b"  # Fast and free
    
    def generate(self, prompt, temperature=0.7, json_mode=False):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://gadgeek.in",
            "X-Title": "Gadgeek News System"
        }
        
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": 2000
        }
        
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        
        try:
            response = requests.post(self.base_url, headers=headers, json=payload, timeout=30)
            response.raise_for_status()
            return response.json()['choices'][0]['message']['content']
        except Exception as e:
            print(f"⚠️ OpenRouter error: {e}", flush=True)
            return None


class GeminiClient:
    """Gemini - Use Flash-8B for higher rate limits"""
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"
        self.model = "gemini-1.5-flash-8b"  # Higher limits than Pro
    
    def generate(self, prompt, temperature=0.7, json_mode=False):
        url = f"{self.base_url}/{self.model}:generateContent?key={self.api_key}"
        
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": 2000
            }
        }
        
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"
        
        try:
            response = requests.post(url, json=payload, timeout=30)
            response.raise_for_status()
            return response.json()['candidates'][0]['content']['parts'][0]['text']
        except Exception as e:
            print(f"⚠️ Gemini error: {e}", flush=True)
            return None


# Initialize clients
groq = GroqClient(GROQ_API_KEY) if GROQ_API_KEY and GROQ_API_KEY != "YOUR_GROQ_API_KEY_HERE" else None
openrouter = OpenRouterClient(OPENROUTER_API_KEY) if OPENROUTER_API_KEY and OPENROUTER_API_KEY != "YOUR_OPENROUTER_API_KEY_HERE" else None
gemini = GeminiClient(GEMINI_API_KEY) if GEMINI_API_KEY else None


# ==================== AI ROUTER ====================

def get_ai_client(priority="primary"):
    """Get available AI client based on priority"""
    if priority == "primary":
        if PRIMARY_AI == "groq" and groq:
            return groq, "Groq"
        elif PRIMARY_AI == "openrouter" and openrouter:
            return openrouter, "OpenRouter"
        elif PRIMARY_AI == "gemini" and gemini:
            return gemini, "Gemini"
    
    elif priority == "secondary":
        if SECONDARY_AI == "openrouter" and openrouter:
            return openrouter, "OpenRouter"
        elif SECONDARY_AI == "groq" and groq:
            return groq, "Groq"
        elif SECONDARY_AI == "gemini" and gemini:
            return gemini, "Gemini"
    
    # Fallback chain
    if groq:
        return groq, "Groq"
    if openrouter:
        return openrouter, "OpenRouter"
    if gemini:
        return gemini, "Gemini"
    
    return None, None


# ==================== STAGE 1: ANALYSIS ====================

def analyze_news_story(content_data):
    """Deep analysis using primary AI"""
    client, name = get_ai_client("primary")
    
    if not client:
        print("❌ No AI client available for analysis", flush=True)
        return create_fallback_analysis(content_data)
    
    prompt = f"""You are a veteran tech journalist analyzing a news story.

ARTICLE CONTENT:
Title: {content_data['title']}
Text: {content_data['text'][:5000]}

Analyze this story deeply. Output ONLY valid JSON with this exact structure:
{{
  "main_event": "What actually happened in one clear sentence",
  "significance": "Why readers should care",
  "key_players": ["Company1", "Person1"],
  "story_type": "breaking_news|product_launch|industry_analysis|controversy|rumor|review",
  "urgency_score": 7,
  "target_audience": "tech_enthusiasts|general_consumers|developers",
  "unique_angle": "Most interesting perspective",
  "context": "Bigger picture or trend",
  "reader_questions": ["What does this mean for me?"]
}}

Be specific. No corporate PR language. Return only JSON."""

    print(f"   🧠 Analyzing with {name}...", flush=True)
    
    try:
        response = client.generate(prompt, temperature=0.3, json_mode=True)
        if response:
            analysis = extract_json(response)
            if analysis and 'main_event' in analysis:
                return analysis
    except Exception as e:
        print(f"   ⚠️ Analysis failed: {e}", flush=True)
    
    return create_fallback_analysis(content_data)


def create_fallback_analysis(content_data):
    """Fallback analysis if AI fails"""
    return {
        "main_event": content_data['title'],
        "significance": "Important tech industry development",
        "key_players": [],
        "story_type": "industry_analysis",
        "urgency_score": 5,
        "target_audience": "tech_enthusiasts",
        "unique_angle": "Latest update",
        "context": "Ongoing development",
        "reader_questions": []
    }


# ==================== STAGE 2: HEADLINE GENERATION ====================

def generate_humanized_headline_and_summary(content_data, analysis):
    """
    Two-step generation:
    1. Primary AI creates initial version
    2. Secondary AI humanizes and refines
    """
    
    # Step 1: Initial generation with primary AI
    client1, name1 = get_ai_client("primary")
    
    if not client1:
        return create_fallback_content(analysis)
    
    prompt1 = f"""You're a master headline writer. No AI language allowed.

CONTEXT:
Event: {analysis['main_event']}
Why it matters: {analysis['significance']}
Angle: {analysis['unique_angle']}
Players: {', '.join(analysis['key_players'][:2]) if analysis['key_players'] else 'Tech companies'}

SOURCE (for facts):
{content_data['text'][:2000]}

CREATE (output as JSON):
1. HEADLINE - under 65 characters, punchy, specific
2. SUMMARY - exactly 2 sentences, under 220 characters total

BANNED PHRASES (never use):
- "The article discusses"
- "According to reports"
- "It's worth noting"
- "Interestingly"
- "Breaking"
- "Latest"

GOOD EXAMPLES:
{{"title": "iPhone 16 Leaks: No Physical Buttons?", "summary": "Apple's next flagship might ditch physical buttons for haptic sensors. The change could make it the most water-resistant iPhone yet."}}

{{"title": "Tesla Cuts Model 3 Price to $35,000", "summary": "Tesla slashed prices after weak Q1 sales. The move puts it in direct competition with Chevy's Bolt."}}

Return ONLY JSON with "title" and "summary". Be direct, specific, human."""

    print(f"   ✍️  Generating with {name1}...", flush=True)
    
    try:
        response1 = client1.generate(prompt1, temperature=0.7, json_mode=True)
        if not response1:
            return create_fallback_content(analysis)
        
        draft = extract_json(response1)
        if not draft or 'title' not in draft:
            return create_fallback_content(analysis)
        
        # Step 2: Humanize with secondary AI
        client2, name2 = get_ai_client("secondary")
        
        if client2 and name2 != name1:  # Only if different AI available
            print(f"   🎨 Humanizing with {name2}...", flush=True)
            
            prompt2 = f"""Refine this headline and summary to sound more human and natural.

CURRENT:
Title: {draft['title']}
Summary: {draft['summary']}

RULES:
1. Keep title under 65 chars
2. Keep summary under 220 chars
3. Make it sound like a human wrote it
4. Remove any corporate or AI language
5. Use contractions when natural (it's, that's, won't)
6. Vary sentence length
7. Be specific and direct

Output as JSON: {{"title": "...", "summary": "..."}}"""
            
            response2 = client2.generate(prompt2, temperature=0.6, json_mode=True)
            if response2:
                refined = extract_json(response2)
                if refined and 'title' in refined:
                    draft = refined
        
        # Validate and truncate
        title = draft.get('title', '')[:65]
        summary = draft.get('summary', '')[:220]
        
        # Remove any AI-isms that slipped through
        ai_phrases = [
            "the article discusses", "according to reports", "it appears that",
            "seems to suggest", "it's worth noting", "interestingly,", "notably,"
        ]
        
        summary_lower = summary.lower()
        for phrase in ai_phrases:
            if phrase in summary_lower:
                print(f"   ⚠️ AI phrase detected, using fallback", flush=True)
                return create_fallback_content(analysis)
        
        return {'title': title, 'summary': summary}
        
    except Exception as e:
        print(f"   ⚠️ Generation failed: {e}", flush=True)
        return create_fallback_content(analysis)


def create_fallback_content(analysis):
    """Fallback content if generation fails"""
    title = analysis['main_event'][:62]
    summary = f"{analysis['main_event'][:110]}. {analysis['significance'][:100]}."
    return {
        'title': title,
        'summary': summary[:220]
    }


# ==================== STAGE 3: ARTICLE WRITING ====================

def write_full_article(title, source_text, analysis):
    """
    Three-stage article writing:
    1. Primary AI writes draft
    2. Secondary AI removes AI-isms
    3. Final validation
    """
    
    # Stage 1: Draft with primary AI
    client1, name1 = get_ai_client("primary")
    
    if not client1:
        return create_fallback_article(title, analysis)
    
    prompt1 = f"""You're a tech journalist. Write a news article that sounds completely human.

ASSIGNMENT:
Title: {title}
Type: {analysis.get('story_type', 'tech news')}
Context: {analysis.get('context', 'Tech update')}

SOURCE:
{source_text[:8000]}

WRITE a 400-600 word article in HTML format.

STRUCTURE:
<p>Strong opening sentence with the news.</p>
<p>Context in 2-3 sentences.</p>
<h2>What's Actually Changing</h2>
<p>Details with specifics and numbers.</p>
<h2>Why This Matters</h2>
<p>Real-world impact.</p>
<p>Brief conclusion.</p>

ABSOLUTELY FORBIDDEN PHRASES:
❌ "It's worth noting"
❌ "Interestingly,"
❌ "Notably,"
❌ "In conclusion,"
❌ "Furthermore,"
❌ "Moreover,"
❌ "Additionally,"
❌ "This development underscores"
❌ "The significance cannot be overstated"

WRITE LIKE THIS:
✓ "Apple cut prices to $35,000 after weak sales."
✓ "That's not a typo—it's the largest fine ever."
✓ "The company has five months to comply."
✓ Use contractions: "it's", "that's", "won't"
✓ Vary sentence length
✓ Be direct and specific

NO markdown, NO code blocks. Just HTML. Return ONLY the HTML."""

    print(f"   📝 Writing with {name1}...", flush=True)
    
    try:
        response1 = client1.generate(prompt1, temperature=0.6)
        if not response1:
            return create_fallback_article(title, analysis)
        
        # Clean markdown artifacts
        article = response1.replace("```html", "").replace("```", "").strip()
        
        # Stage 2: Humanize with secondary AI if available
        client2, name2 = get_ai_client("secondary")
        
        if client2 and name2 != name1 and len(article) > 200:
            print(f"   🎨 Humanizing with {name2}...", flush=True)
            
            prompt2 = f"""Remove ALL AI-sounding phrases from this article. Make it sound like a human journalist wrote it.

ARTICLE:
{article[:6000]}

FIX:
1. Remove: "It's worth noting", "Interestingly", "Notably", etc.
2. Use contractions naturally
3. Vary sentence structure
4. Keep it conversational but professional
5. Be direct and specific

Return ONLY the improved HTML. No explanation."""
            
            response2 = client2.generate(prompt2, temperature=0.5)
            if response2:
                refined = response2.replace("```html", "").replace("```", "").strip()
                if len(refined) > 200:
                    article = refined
        
        # Final validation: remove any remaining AI-isms
        ai_patterns = [
            r"It'?s worth noting that\s*",
            r"Interestingly,?\s*",
            r"Notably,?\s*",
            r"In conclusion,?\s*",
            r"Furthermore,?\s*",
            r"Moreover,?\s*",
        ]
        
        for pattern in ai_patterns:
            article = re.sub(pattern, "", article, flags=re.IGNORECASE)
        
        if len(article) < 200:
            return create_fallback_article(title, analysis)
        
        return article
        
    except Exception as e:
        print(f"   ⚠️ Article writing failed: {e}", flush=True)
        return create_fallback_article(title, analysis)


def create_fallback_article(title, analysis):
    """Fallback article if generation fails"""
    return f"""<p><strong>{title}</strong></p>
<p>This story is developing. {analysis.get('significance', 'Important tech industry update.')}</p>
<h2>Background</h2>
<p>{analysis.get('context', 'This is part of ongoing developments in the tech industry.')}</p>
<p>Check the source link for full details and updates as this story develops.</p>"""