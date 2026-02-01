"""
Multi-AI Content Generation - Maximum Humanization
Uses Groq (primary) + OpenRouter (refinement) for best results
"""
import json
import requests
import time
import re
from config import GROQ_API_KEY, OPENROUTER_API_KEY, GEMINI_API_KEY, extract_json

# ==================== AI CLIENTS ====================

class GroqClient:
    """Groq - Very fast, generous free tier"""
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://api.groq.com/openai/v1/chat/completions"
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
    """OpenRouter - Free models for refinement"""
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://openrouter.ai/api/v1/chat/completions"
        # Use confirmed working free model
        self.model = "nousresearch/hermes-3-llama-3.1-405b:free"
    
    def generate(self, prompt, temperature=0.7, json_mode=False):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://gadgeek.in"
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
    """Gemini Flash-8B - Fallback"""
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"
        self.model = "gemini-1.5-flash-8b"
    
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

# ==================== AI ROUTING ====================

def get_ai_client(priority="primary"):
    """Get available AI client"""
    if priority == "primary":
        if groq:
            return groq, "Groq"
        if gemini:
            return gemini, "Gemini"
        if openrouter:
            return openrouter, "OpenRouter"
    
    elif priority == "secondary":
        if openrouter:
            return openrouter, "OpenRouter"
        if gemini:
            return gemini, "Gemini"
        if groq:
            return groq, "Groq"
    
    # Fallback
    if groq:
        return groq, "Groq"
    if gemini:
        return gemini, "Gemini"
    if openrouter:
        return openrouter, "OpenRouter"
    
    return None, None


# ==================== ANALYSIS ====================

def analyze_news_story(content_data):
    """Deep analysis using AI"""
    client, name = get_ai_client("primary")
    
    if not client:
        return create_fallback_analysis(content_data)
    
    prompt = f"""Analyze this tech news story. Output ONLY valid JSON.

ARTICLE:
Title: {content_data['title']}
Text: {content_data['text'][:5000]}

OUTPUT JSON:
{{
  "main_event": "What happened in one sentence",
  "significance": "Why readers care",
  "key_players": ["Company1", "Person1"],
  "story_type": "breaking_news|product_launch|industry_analysis|controversy|rumor|review",
  "urgency_score": 7,
  "target_audience": "tech_enthusiasts|general_consumers|developers",
  "unique_angle": "Most interesting perspective",
  "context": "Bigger picture",
  "reader_questions": ["What does this mean?"]
}}

Be specific. No PR language. Return only JSON."""

    print(f"   🧠 Analyzing with {name}...", flush=True)
    
    try:
        response = client.generate(prompt, temperature=0.3, json_mode=True)
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
        "significance": "Tech industry development",
        "key_players": [],
        "story_type": "industry_analysis",
        "urgency_score": 5,
        "target_audience": "tech_enthusiasts",
        "unique_angle": "Latest update",
        "context": "Ongoing development",
        "reader_questions": []
    }


# ==================== HEADLINE GENERATION ====================

def generate_humanized_headline_and_summary(content_data, analysis):
    """Two-step: Draft with primary AI, humanize with secondary"""
    
    client1, name1 = get_ai_client("primary")
    
    if not client1:
        return create_fallback_content(analysis)
    
    prompt1 = f"""Create headline and summary. Output as JSON.

CONTEXT:
Event: {analysis['main_event']}
Impact: {analysis['significance']}
Players: {', '.join(analysis['key_players'][:2]) if analysis['key_players'] else 'Tech firms'}

SOURCE:
{content_data['text'][:2000]}

CREATE (JSON):
1. HEADLINE - under 65 characters
2. SUMMARY - 2 sentences, under 220 chars

BANNED: "The article", "According to", "It's worth noting", "Interestingly", "Breaking", "Latest"

EXAMPLES:
{{"title": "iPhone 16 Leaks: No Physical Buttons?", "summary": "Apple's next flagship might ditch physical buttons for haptic sensors. The change could make it the most water-resistant iPhone yet."}}

{{"title": "Tesla Cuts Model 3 Price to $35,000", "summary": "Tesla slashed prices after weak Q1 sales. This puts it in direct competition with Chevy's Bolt."}}

Return ONLY JSON with "title" and "summary"."""

    print(f"   ✍️  Generating with {name1}...", flush=True)
    
    try:
        response1 = client1.generate(prompt1, temperature=0.7, json_mode=True)
        if not response1:
            return create_fallback_content(analysis)
        
        draft = extract_json(response1)
        if not draft or 'title' not in draft:
            return create_fallback_content(analysis)
        
        # Try humanization with secondary AI
        client2, name2 = get_ai_client("secondary")
        
        if client2 and name2 != name1:
            print(f"   🎨 Humanizing with {name2}...", flush=True)
            
            prompt2 = f"""Make this sound more human. Output as JSON.

CURRENT:
Title: {draft['title']}
Summary: {draft['summary']}

RULES:
- Keep title under 65 chars
- Keep summary under 220 chars
- Sound like a human wrote it
- Use contractions (it's, that's)
- Be specific and direct

Output JSON: {{"title": "...", "summary": "..."}}"""
            
            response2 = client2.generate(prompt2, temperature=0.6, json_mode=True)
            if response2:
                refined = extract_json(response2)
                if refined and 'title' in refined:
                    draft = refined
        
        title = draft.get('title', '')[:65]
        summary = draft.get('summary', '')[:220]
        
        # Check for AI-isms
        ai_phrases = ["the article discusses", "according to reports", "it's worth noting"]
        if any(phrase in summary.lower() for phrase in ai_phrases):
            return create_fallback_content(analysis)
        
        return {'title': title, 'summary': summary}
        
    except:
        return create_fallback_content(analysis)


def create_fallback_content(analysis):
    title = analysis['main_event'][:62]
    summary = f"{analysis['main_event'][:110]}. {analysis['significance'][:100]}."
    return {'title': title, 'summary': summary[:220]}


# ==================== ARTICLE WRITING ====================

def write_full_article(title, source_text, analysis):
    """Three-stage: Draft, humanize, validate"""
    
    client1, name1 = get_ai_client("primary")
    
    if not client1:
        return create_fallback_article(title, analysis)
    
    prompt1 = f"""Write a tech news article in HTML. Sound completely human.

ASSIGNMENT:
Title: {title}
Type: {analysis.get('story_type', 'news')}

SOURCE:
{source_text[:8000]}

WRITE 400-600 words in HTML.

STRUCTURE:
<p>Strong opening with the news.</p>
<p>Context in 2-3 sentences.</p>
<h2>What's Changing</h2>
<p>Details with numbers.</p>
<h2>Why This Matters</h2>
<p>Real impact.</p>

FORBIDDEN:
❌ "It's worth noting"
❌ "Interestingly,"
❌ "In conclusion,"
❌ "Furthermore,"
❌ "Moreover,"

WRITE LIKE:
✓ "Apple cut prices after weak sales."
✓ "That's the largest fine ever."
✓ Use contractions
✓ Vary sentence length

NO markdown. Just HTML."""

    print(f"   📝 Writing with {name1}...", flush=True)
    
    try:
        response1 = client1.generate(prompt1, temperature=0.6)
        if not response1:
            return create_fallback_article(title, analysis)
        
        article = response1.replace("```html", "").replace("```", "").strip()
        
        # Humanize if possible
        client2, name2 = get_ai_client("secondary")
        
        if client2 and name2 != name1 and len(article) > 200:
            print(f"   🎨 Humanizing with {name2}...", flush=True)
            
            prompt2 = f"""Remove AI phrases. Make it sound human.

ARTICLE:
{article[:6000]}

FIX:
- Remove: "It's worth noting", "Interestingly", etc.
- Use contractions
- Vary sentences
- Be direct

Return ONLY improved HTML."""
            
            response2 = client2.generate(prompt2, temperature=0.5)
            if response2:
                refined = response2.replace("```html", "").replace("```", "").strip()
                if len(refined) > 200:
                    article = refined
        
        # Final cleanup
        patterns = [
            r"It'?s worth noting that\s*",
            r"Interestingly,?\s*",
            r"Notably,?\s*",
            r"In conclusion,?\s*"
        ]
        
        for pattern in patterns:
            article = re.sub(pattern, "", article, flags=re.IGNORECASE)
        
        if len(article) < 200:
            return create_fallback_article(title, analysis)
        
        return article
        
    except Exception as e:
        print(f"   ⚠️ Article writing failed: {e}", flush=True)
        return create_fallback_article(title, analysis)


def create_fallback_article(title, analysis):
    return f"""<p><strong>{title}</strong></p>
<p>This story is developing. {analysis.get('significance', 'Important update.')}</p>
<h2>Background</h2>
<p>{analysis.get('context', 'Part of ongoing tech industry developments.')}</p>
<p>Check the source for full details.</p>"""