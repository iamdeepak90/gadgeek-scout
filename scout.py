import sys
from typing import Any, Dict, List, Tuple
from common import (
    LOG, setup_logging, init_db, list_feeds, parse_feed,
    extract_entry_fields, lead_exists_by_url, create_lead,
    slack_post_lead, get_categories, DEFAULT_CATEGORY_UUID,
    keyword_score
)

def match_category(entry: Dict[str, str], categories: List[Dict[str, Any]], hint: str = "") -> Tuple[str, str, int]:
    if hint:
        for c in categories:
            if c["slug"] == hint: return str(c.get("id", "")), c["name"], 999
    
    blob = " ".join([entry.get("title",""), entry.get("description",""), entry.get("category","")]).lower()
    best = (DEFAULT_CATEGORY_UUID, "General", -1, 999)
    
    for c in categories:
        score = keyword_score(blob, c.get("keywords", []))
        if score > best[2] or (score == best[2] and c.get("priority", 999) < best[3]):
            best = (str(c["id"]), c["name"], score, c.get("priority", 999))
    return best[0], best[1], best[2]

def scout_once():
    init_db()
    feeds, cats = [f for f in list_feeds() if f.get("enabled")], get_categories()
    if not feeds or not cats: return 0
    
    created = 0
    for f_cfg in feeds:
        try:
            parsed = parse_feed(f_cfg["url"])
            for ent in (parsed.entries or [])[:25]:
                fields = extract_entry_fields(ent, f_cfg)
                if not fields.get("title") or lead_exists_by_url(fields["link"]): continue
                
                cid, cname, _ = match_category(fields, cats, f_cfg.get("category_hint"))
                lid = create_lead(fields["title"], fields["link"], cid)
                slack_post_lead(fields["title"], cname, lid)
                created += 1
        except Exception as e: LOG.error(f"Feed error {f_cfg['url']}: {e}")
    return created

if __name__ == "__main__":
    setup_logging(); scout_once()