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
)

# Simple in-process lock to avoid double publishes on repeated clicks.
_PUBLISH_LOCK = set()
_LOCK = threading.Lock()

def _get_category_name_map():
    cats = get_categories()
    return {c["slug"]: c["name"] for c in cats}

def publish_lead_by_id(lead_id: int, slack_ctx: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Publishes an approved lead into Articles.
    If slack_ctx is provided: {"channel": "...", "ts": "...", "title": "..."} then updates Slack after success.
    On failure: lead status is set back to 'approved' (safe default).
    """
    init_db()
    with _LOCK:
        if lead_id in _PUBLISH_LOCK:
            LOG.warning("Lead %s already publishing in this process.", lead_id)
            return {"ok": False, "error": "already_publishing"}
        _PUBLISH_LOCK.add(lead_id)

    try:
        lead = get_lead(lead_id)
        status = (lead.get("status") or "").lower()
        if status == "published":
            return {"ok": True, "already": True}

        title = lead.get("title") or ""
        category_slug = lead.get("category_slug") or ""
        if not title or not category_slug:
            raise RuntimeError("Lead missing title or category_slug.")

        cat_names = _get_category_name_map()
        category_name = cat_names.get(category_slug, category_slug)

        # Build article with configured models/providers
        article = create_article_from_lead(title=title, category_name=category_name)
        created = publish_article_to_directus(article, category_slug=category_slug)

        update_lead_status(lead_id, "published")

        if slack_ctx and slack_ctx.get("channel") and slack_ctx.get("ts"):
            try:
                slack_update_published(slack_ctx["channel"], slack_ctx["ts"], title)
            except Exception as e:
                LOG.warning("Slack update failed for lead %s: %s", lead_id, e)

        return {"ok": True, "article": created}
    except Exception as e:
        LOG.exception("Publish failed for lead %s: %s", lead_id, e)
        # revert back to approved as requested
        try:
            update_lead_status(lead_id, "approved")
        except Exception as ee:
            LOG.warning("Failed to revert lead %s to approved: %s", lead_id, ee)
        return {"ok": False, "error": str(e)}
    finally:
        with _LOCK:
            _PUBLISH_LOCK.discard(lead_id)

def _publish_loop():
    interval_min = int(float(get_setting("publish_interval_minutes", "20") or "20"))
    sleep_s = max(60, interval_min * 60)
    LOG.info("Publisher loop started. Will publish 1 approved lead every %s minutes.", interval_min)

    while True:
        try:
            lead = list_one_approved_lead_newest()
            if lead:
                lead_id = int(lead["id"])
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
