import threading
import time
from typing import Any, Dict, Optional

from common import (
    LOG,
    setup_logging,
    init_db,
    get_categories,
    get_lead,
    update_lead_status,
    list_one_approved_lead_newest,
    create_article_from_lead,
    publish_article_to_directus,
    slack_update_published,
    get_setting,
    DEFAULT_CATEGORY_UUID,
)

_LOCK = threading.Lock()
_PUBLISHING: set[str] = set()

def _category_maps():
    cats = get_categories()
    by_id = {c["id"]: c for c in cats if c.get("id")}
    return by_id

def publish_lead_by_id(lead_id: str, slack_ctx: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    init_db()

    with _LOCK:
        if lead_id in _PUBLISHING:
            LOG.warning("Lead %s already publishing in this process.", lead_id)
            return {"ok": False, "error": "already_publishing"}
        _PUBLISHING.add(lead_id)

    try:
        lead = get_lead(lead_id)
        status = (lead.get("status") or "").lower()
        if status == "published":
            return {"ok": True, "already": True}
        if status == "publishing":
            return {"ok": True, "already": True}

        # Move to publishing ASAP to avoid duplicates
        try:
            update_lead_status(lead_id, "publishing")
        except Exception:
            pass

        title = (lead.get("title") or "").strip()
        category_id = (lead.get("category") or "").strip() or DEFAULT_CATEGORY_UUID
        if not title:
            raise RuntimeError("Lead missing title")

        cats_by_id = _category_maps()
        cat = cats_by_id.get(category_id) or {}
        category_name = cat.get("name") or "Uncategorized"
        category_prompt = cat.get("prompt_generation") or ""

        article = create_article_from_lead(
            title=title,
            category_name=category_name,
            category_id=category_id,
            category_prompt=category_prompt,
        )
        created = publish_article_to_directus(article)

        update_lead_status(lead_id, "published")

        if slack_ctx and slack_ctx.get("channel") and slack_ctx.get("ts"):
            try:
                slack_update_published(slack_ctx["channel"], slack_ctx["ts"], title)
            except Exception as e:
                LOG.warning("Slack update failed for lead %s: %s", lead_id, e)

        return {"ok": True, "article": created}
    except Exception as e:
        LOG.exception("Publish failed for lead %s: %s", lead_id, e)
        # revert to approved for later retry
        try:
            update_lead_status(lead_id, "approved")
        except Exception:
            pass
        return {"ok": False, "error": str(e)}
    finally:
        with _LOCK:
            _PUBLISHING.discard(lead_id)

def _publish_loop():
    interval_min = int(float(get_setting("publish_interval_minutes", "20") or "20"))
    sleep_s = max(60, interval_min * 60)
    LOG.info("Publisher loop started. Will publish 1 approved lead every %s minutes.", interval_min)

    while True:
        try:
            lead = list_one_approved_lead_newest()
            if lead:
                lead_id = str(lead.get("id") or "")
                if lead_id:
                    LOG.info("Publishing approved lead %s...", lead_id)
                    publish_lead_by_id(lead_id)
            else:
                LOG.info("No approved leads to publish.")
        except Exception as e:
            LOG.exception("Publisher loop error: %s", e)
        time.sleep(sleep_s)

def main():
    setup_logging()
    init_db()
    _publish_loop()

if __name__ == "__main__":
    main()
