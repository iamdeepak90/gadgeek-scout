import json
import re
from typing import List, Dict, Any, Optional
from common import LOG, get_setting, get_model_routes, request_with_retry

TOGETHER_CHAT_URL = "https://api.together.xyz/v1/chat/completions"
TOGETHER_IMAGES_URL = "https://api.together.xyz/v1/images/generations"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

def chat_stage(stage: str, messages: List[Dict[str, str]]) -> str:
    """Centralized routing for text stages with System/User role isolation."""
    routes = get_model_routes()
    route = routes.get(stage)
    if not route:
        LOG.error(f"Missing model route for stage: {stage}")
        raise RuntimeError(f"No model route configured for stage: {stage}")
    
    provider = route["provider"]
    model = route["model"]
    temperature = route.get("temperature", 0.7)
    max_tokens = route.get("max_tokens", 2200)
    
    if provider == "together":
        return _chat_together(model, messages, temperature, max_tokens)
    elif provider == "openrouter":
        return _chat_openrouter(model, messages, temperature, max_tokens)
    else:
        raise RuntimeError(f"Unknown provider: {provider}")

def _chat_together(model: str, messages: List[Dict[str, str]], temp: float, tokens: int) -> str:
    key = get_setting("together_api_key")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "temperature": temp, "max_tokens": tokens}
    resp = request_with_retry("POST", TOGETHER_CHAT_URL, headers=headers, json_body=payload, timeout=120)
    return (resp.json().get("choices", [{}])[0].get("message") or {}).get("content", "")

def _chat_openrouter(model: str, messages: List[Dict[str, str]], temp: float, tokens: int) -> str:
    key = get_setting("openrouter_api_key")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "X-Title": "Gadgeek AI"}
    payload = {"model": model, "messages": messages, "temperature": temp, "max_tokens": tokens}
    resp = request_with_retry("POST", OPENROUTER_CHAT_URL, headers=headers, json_body=payload, timeout=120)
    return (resp.json().get("choices", [{}])[0].get("message") or {}).get("content", "")

def generate_image_logic(prompt: str) -> Optional[Dict[str, str]]:
    """Handles AI image generation with configured routing and automatic fallback."""
    routes = get_model_routes()
    route = routes.get("image") or {"provider": "together", "model": "black-forest-labs/FLUX.1-schnell"}
    
    provider, model = route["provider"], route["model"]
    width, height = route.get("width", 1024), route.get("height", 768)

    if provider == "together":
        key = get_setting("together_api_key")
        if not key: return None
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        payload = {"model": model, "prompt": prompt, "width": width, "height": height, "steps": 4, "response_format": "url"}
        try:
            resp = request_with_retry("POST", TOGETHER_IMAGES_URL, headers=headers, json_body=payload)
            url = resp.json().get("data", [{}])[0].get("url")
            if url: return {"url": url, "credit": f"AI ({model})", "caption": "Tech Illustration"}
        except Exception as e:
            LOG.error(f"Image generation failed: {e}")
    return None

def strip_code_fences(text: str) -> str:
    """Removes markdown code fences like ```html."""
    t = text.strip()
    t = re.sub(r"^```(?:html|HTML)?\s*\n?", "", t)
    t = re.sub(r"\n?```\s*$", "", t)
    return t.strip()