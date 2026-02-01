"""
Advanced AI Content Generation Module
Produces highly humanized, natural-sounding content without AI fingerprints
"""
import json
from config import analysis_model, creative_model, article_model, extract_json


def analyze_news_story(content_data):
    """
    STAGE 1: Deep journalistic analysis
    Understands what the story is really about
    """
    prompt = f"""
You are a veteran tech journalist with 15 years of experience analyzing breaking news.

ARTICLE CONTENT:
Title: {content_data['title']}
Full Text: {content_data['text'][:5000]}

YOUR ANALYSIS TASK:
Think like a seasoned editor. Answer these questions:

1. What's the ACTUAL news here? (Not just what the headline says)
2. Why should readers care? What's the real-world impact?
3. Who are the key players and what are their motives?
4. What's the broader context or trend this fits into?
5. Is this breaking news, analysis, rumor, or product coverage?
6. On a scale of 1-10, how urgent/important is this story?
7. What's the most interesting or surprising angle?
8. What questions would readers want answered?

OUTPUT FORMAT (JSON):
{{
  "main_event": "The core news in one clear sentence",
  "significance": "Why this matters to readers",
  "key_players": ["Company/Person 1", "Company/Person 2"],
  "story_type": "breaking_news | product_launch | industry_analysis | controversy | rumor | review | trend_piece",
  "urgency_score": 7,
  "target_audience": "tech_enthusiasts | general_consumers | developers | business_leaders | gamers",
  "unique_angle": "The most interesting perspective on this story",
  "context": "What's the bigger picture or trend?",
  "reader_questions": ["What does this mean for me?", "When can I get it?"]
}}

Think deeply. Be specific. Avoid corporate PR language.
"""
    
    try:
        response = analysis_model.generate_content(prompt)
        analysis = extract_json(response.text)
        
        if analysis and 'main_event' in analysis:
            return analysis
        else:
            raise ValueError("Incomplete analysis")
            
    except Exception as e:
        print(f"⚠️ Analysis failed: {e}", flush=True)
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


def generate_humanized_headline_and_summary(content_data, analysis):
    """
    STAGE 2: Generate natural, engaging headlines
    Writes like a human journalist, not a robot
    """
    prompt = f"""
You are a master headline writer for a major tech publication. Your headlines get clicks while staying factual.

STORY CONTEXT:
Main Event: {analysis['main_event']}
Significance: {analysis['significance']}
Unique Angle: {analysis['unique_angle']}
Key Players: {', '.join(analysis['key_players'][:3]) if analysis['key_players'] else 'Various'}
Target Audience: {analysis['target_audience']}
Story Type: {analysis['story_type']}

ORIGINAL CONTENT (for reference):
{content_data['text'][:2000]}

YOUR TASK - WRITE LIKE A HUMAN JOURNALIST:

1. HEADLINE (Maximum 65 characters):
   CRITICAL RULES:
   - Must be under 65 characters total (spaces count!)
   - Write conversationally - how would you tell a friend?
   - Use active voice, present tense
   - Include specifics (numbers, names, products)
   - Create curiosity without being clickbait
   - NO generic words: "Latest", "New Update", "Breaking"
   - NO corporate speak: "Announces", "Unveils", "Launches"
   
   GOOD EXAMPLES:
   ✓ "iPhone 16 Leaks: No Physical Buttons?" (40 chars)
   ✓ "Tesla Cuts Model 3 Price to $35,000" (37 chars)
   ✓ "Google's Gemini Can Now Code in Python" (42 chars)
   ✓ "Meta Fined €1.2B for Privacy Violations" (43 chars)
   
   BAD EXAMPLES:
   ✗ "Apple Announces New iPhone Features" (too generic, corporate)
   ✗ "Latest Update: Tesla Model 3 Gets Price Drop" (too long, generic)
   ✗ "Breaking: Google Unveils New AI Model" (clickbait, corporate)

2. SUMMARY (Maximum 220 characters):
   CRITICAL RULES:
   - Exactly 2 sentences, both meaningful
   - First sentence: What happened (be specific)
   - Second sentence: Why it matters (real impact)
   - Write like explaining to a curious friend
   - Include numbers, dates, or specifics when available
   - NO phrases: "The article discusses", "According to reports", "It appears that"
   - NO hedging: "seems to", "might be", "could potentially"
   - Be confident and direct
   
   GOOD EXAMPLES:
   ✓ "Apple's iPhone 16 Pro will reportedly ditch all physical buttons for haptic sensors. The change could make it the most water-resistant iPhone ever built."
   
   ✓ "Tesla slashed Model 3 prices to $35,000 after facing weak Q1 sales. The move puts it in direct competition with Chevy's Bolt and Nissan Leaf."
   
   ✓ "Google's Gemini 1.5 can now process 1 million tokens in one go. That's roughly equivalent to 10 full-length novels analyzed simultaneously."
   
   BAD EXAMPLES:
   ✗ "The article discusses how Apple might be considering removing buttons. This could potentially impact the design." (vague, hedging)
   ✗ "According to reports, Tesla has announced price changes. It seems this will affect competition." (too vague, passive)

OUTPUT FORMAT (JSON):
{{
  "title": "Your headline here",
  "summary": "First sentence with specific details. Second sentence explaining real impact.",
  "char_count_title": 42,
  "char_count_summary": 156,
  "writing_notes": "Why this headline works"
}}

BEFORE YOU OUTPUT:
1. Count every character in your title (spaces included)
2. Is it over 65? REWRITE IT SHORTER
3. Does it sound like a human wrote it?
4. Would you click on this?
5. Is it specific enough?

Now write the most engaging, human-sounding headline and summary possible.
"""
    
    try:
        response = creative_model.generate_content(prompt)
        generated = extract_json(response.text)
        
        if not generated or 'title' not in generated:
            raise ValueError("Invalid generation")
        
        # Strict validation
        title = generated['title']
        summary = generated['summary']
        
        # Truncate if needed
        if len(title) > 65:
            print(f"⚠️ Title too long ({len(title)} chars): '{title}'", flush=True)
            title = title[:62] + "..."
        
        if len(summary) > 220:
            summary = summary[:217] + "..."
        
        # Remove any AI-isms that slipped through
        ai_phrases = [
            "the article discusses",
            "according to reports",
            "it appears that",
            "seems to suggest",
            "could potentially",
            "it's worth noting",
            "interestingly,",
            "notably,"
        ]
        
        summary_lower = summary.lower()
        for phrase in ai_phrases:
            if phrase in summary_lower:
                print(f"⚠️ AI phrase detected: '{phrase}' - regenerating...", flush=True)
                # Try one more time
                return generate_humanized_headline_and_summary(content_data, analysis)
        
        return {
            'title': title,
            'summary': summary
        }
        
    except Exception as e:
        print(f"⚠️ Generation failed: {e}", flush=True)
        # Smart fallback
        fallback_title = analysis['main_event'][:62]
        fallback_summary = f"{analysis['main_event'][:110]}. {analysis['significance'][:100]}."
        return {
            'title': fallback_title,
            'summary': fallback_summary[:220]
        }


def write_full_article(title, source_text, analysis):
    """
    STAGE 3: Write complete article in natural, human style
    NO AI fingerprints, NO robotic language
    """
    prompt = f"""
You are an experienced tech journalist writing for a major publication. Your articles are known for being informative, engaging, and sounding completely natural.

ASSIGNMENT DETAILS:
Article Title: {title}
Story Type: {analysis.get('story_type', 'tech news')}
Key Context: {analysis.get('context', 'Technology update')}

SOURCE MATERIAL:
{source_text[:8000]}

YOUR WRITING TASK:

Write a complete news article in HTML format that sounds like it was written by a human journalist, NOT an AI.

CRITICAL REQUIREMENTS:

1. STRUCTURE (HTML format only):
   - Start with <p> containing a strong opening sentence
   - Use <h2> for major sections (2-3 sections max)
   - Use <p> for all body text
   - Include <strong> for emphasis (sparingly)
   - NO markdown, NO backticks, NO code blocks
   - Return ONLY the HTML body content

2. WRITING STYLE - HUMANIZE EVERYTHING:
   ❌ AVOID THESE AI PATTERNS:
   - "It's worth noting that..."
   - "Interestingly,"
   - "Notably,"
   - "In conclusion,"
   - "It's important to highlight..."
   - "This development underscores..."
   - "The significance of this cannot be overstated..."
   - "As we delve deeper..."
   - "In today's rapidly evolving..."
   - Starting sentences with "Furthermore," "Moreover," "Additionally,"
   
   ✅ WRITE LIKE THIS INSTEAD:
   - Direct, active sentences: "Apple cut prices" not "Apple has announced price cuts"
   - Vary sentence length: Short punchy sentences. Followed by longer explanatory ones.
   - Use contractions naturally: "it's", "that's", "won't"
   - Ask rhetorical questions occasionally
   - Include specific details and numbers
   - Use everyday comparisons when explaining tech
   - Write like you're explaining to a smart friend

3. CONTENT STRUCTURE:
   <p>STRONG OPENING: Lead with the most newsworthy fact. Make it punchy.</p>
   
   <p>CONTEXT: Explain the background in 2-3 sentences. Why does this matter?</p>
   
   <h2>What's Actually Changing</h2>
   <p>Detail the specifics. Use numbers. Be concrete. No fluff.</p>
   
   <p>Continue with more details, but keep it readable. Break up long paragraphs.</p>
   
   <h2>Why This Matters</h2>
   <p>Real-world impact. How does this affect users/industry? Be specific.</p>
   
   <p>Final thoughts or what comes next. Keep it brief.</p>

4. TONE:
   - Authoritative but conversational
   - Informed but not pretentious  
   - Engaging but not overly casual
   - Skeptical when appropriate (especially for rumors)
   - Professional but human

5. LENGTH:
   - 400-600 words total
   - Paragraphs: 2-4 sentences each
   - Avoid walls of text

EXAMPLE OF GOOD HUMAN WRITING:
<p>Meta just got hit with a €1.2 billion fine from European regulators over how it handles user data transfers. That's not a typo—it's the largest GDPR penalty ever issued.</p>

<p>The ruling specifically targets Meta's practice of sending European users' data to US servers. Regulators argue this violates EU privacy protections, since American surveillance laws don't offer the same safeguards.</p>

<h2>What Meta Has to Change</h2>
<p>The company has five months to stop transferring EU user data to the United States. That's a massive operational challenge for a platform with 250 million European users.</p>

<p>Meta says it'll appeal, calling the decision "flawed." But similar cases against Google and Amazon suggest the tide is turning against big tech's data practices.</p>

Now write the article. Make it sound completely human. No AI tells.
"""
    
    try:
        response = article_model.generate_content(prompt)
        html_content = response.text
        
        # Clean up any markdown artifacts
        html_content = html_content.replace("```html", "").replace("```", "").strip()
        
        # Remove AI-ism patterns that might have slipped through
        ai_patterns = [
            r"It'?s worth noting that\s*",
            r"Interestingly,?\s*",
            r"Notably,?\s*",
            r"In conclusion,?\s*",
            r"It'?s important to highlight\s*",
            r"This development underscores\s*",
            r"As we delve deeper\s*",
            r"In today'?s rapidly evolving\s*",
        ]
        
        for pattern in ai_patterns:
            html_content = re.sub(pattern, "", html_content, flags=re.IGNORECASE)
        
        # Ensure we have actual content
        if len(html_content) < 200:
            raise ValueError("Article too short")
        
        return html_content
        
    except Exception as e:
        print(f"⚠️ Article writing failed: {e}", flush=True)
        # Fallback article
        return f"""<p><strong>Breaking:</strong> {title}</p>
<p>This story is developing. Check back for updates or read the original source for full details.</p>
<h2>Background</h2>
<p>{analysis.get('context', 'This is part of ongoing developments in the tech industry.')}</p>
<p>{analysis.get('significance', 'The impact of this development is still being assessed.')}</p>"""