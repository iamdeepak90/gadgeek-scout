import threading
import time
from typing import Any, Dict, Optional

from common import (
    LOG,
    setup_logging,
    init_db,
    get_lead,
    update_lead_status,
    list_one_approved_lead_newest,
    create_article_from_lead,
    publish_article_to_directus,
    slack_update_published,
    get_setting,
    get_category_by_id,
)

# Directus lead status values (your enum)
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_APPROVED_HIGH = "approved_high"
STATUS_REJECTED = "rejected"
STATUS_PROCESSED = "processed"
STATUS_EXPIRED = "expired"

# Per-process publish lock to avoid accidental double publish in the same process.
_PUBLISHING: set[str] = set()
_LOCK = threading.Lock()


def publish_lead_by_id(lead_id: str, slack_ctx: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Publish a single lead into Articles.

    Slack context (optional): {"channel": "...", "ts": "...", "title": "..."}
    """
    init_db()

    with _LOCK:
        if lead_id in _PUBLISHING:
            LOG.warning("Lead %s is already publishing in this process.", lead_id)
            return {"ok": False, "error": "already_publishing"}
        _PUBLISHING.add(lead_id)

    try:
        lead = get_lead(lead_id)
        status = (lead.get("status") or "").strip().lower()

        if status == STATUS_PROCESSED:
            return {"ok": True, "already": True}

        if status in (STATUS_REJECTED, STATUS_EXPIRED):
            return {"ok": False, "error": f"not_publishable_status:{status}"}

        title = (lead.get("title") or "").strip()
        category_id = (lead.get("category") or "").strip()
        source_url = (lead.get("source_url") or "").strip()

        if not title or not category_id:
            raise RuntimeError("Lead missing title or category.")

        cat = get_category_by_id(category_id) or {}
        category_name = (cat.get("name") or "").strip() or "Tech"
        category_prompt = (cat.get("prompt_generation") or "").strip()

        # Generate full article (includes image pipeline)
        article = create_article_from_lead(
            title=title,
            category_name=category_name,
            source_url=source_url,
            category_prompt_generation=category_prompt,
        )

        created = publish_article_to_directus(article, category_id=category_id)

        # Mark processed
        update_lead_status(lead_id, STATUS_PROCESSED)

        # Update Slack message if context present
        if slack_ctx and slack_ctx.get("channel") and slack_ctx.get("ts"):
            try:
                slack_update_published(slack_ctx["channel"], slack_ctx["ts"], title)
            except Exception as e:
                LOG.warning("Slack update failed for lead %s: %s", lead_id, e)

        return {"ok": True, "article": created}
    except Exception as e:
        LOG.exception("Publish failed for lead %s: %s", lead_id, e)
        return {"ok": False, "error": str(e)}
    finally:
        with _LOCK:
            _PUBLISHING.discard(lead_id)


def _publish_loop() -> None:
    interval_min = int(float(get_setting("publish_interval_minutes", "20") or "20"))
    sleep_s = max(30, interval_min * 60)
    LOG.info("Publisher loop started. Will publish 1 approved lead every %s minutes.", interval_min)

    while True:
        try:
            lead = list_one_approved_lead_newest(status=STATUS_APPROVED)
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


def main() -> None:
    setup_logging()
    init_db()
    _publish_loop()


if __name__ == "__main__":
    main()
