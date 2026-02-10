import threading, time
from common import *

_PUBLISHING, _LOCK = set(), threading.Lock()

def publish_lead_by_id(lid, slack_ctx=None):
    init_db()
    with _LOCK:
        if lid in _PUBLISHING: return {"ok": False, "error": "already_publishing"}
        _PUBLISHING.add(lid)
    try:
        lead = get_lead(lid)
        if lead.get("status") == "processed": return {"ok": True, "already": True}
        cat = get_category_by_id(lead["category"]) or {"name": "Tech"}
        art = create_article_from_lead(lead["title"], cat["name"], lead.get("source_url", ""), cat.get("prompt_generation", ""))
        publish_article_to_directus(art, lead["category"])
        update_lead_status(lid, "processed")
        if slack_ctx: slack_update_published(slack_ctx["channel"], slack_ctx["ts"], lead["title"])
        return {"ok": True}
    except Exception as e:
        LOG.exception(f"Publish failed {lid}: {e}"); return {"ok": False, "error": str(e)}
    finally:
        with _LOCK: _PUBLISHING.discard(lid)

def _publish_loop():
    while True:
        try:
            l = list_one_approved_lead_newest("approved")
            if l: publish_lead_by_id(str(l["id"]))
        except Exception as e: LOG.error(f"Loop error: {e}")
        time.sleep(int(float(get_setting("publish_interval_minutes", "20"))) * 60)

if __name__ == "__main__": setup_logging(); init_db(); _publish_loop()