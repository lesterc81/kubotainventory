import logging
import atexit
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger("itsystem.ai.scheduler")
_scheduler = None
_mongo_db = None


def init_scheduler(app, mongo_db):
    global _scheduler, _mongo_db
    if _scheduler is not None:
        return

    _mongo_db = mongo_db
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        func=_check_anomalies,
        trigger="interval",
        minutes=15,
        id="anomaly_check",
        name="Anomaly detection every 15 minutes",
        replace_existing=True,
    )

    _scheduler.add_job(
        func=_cleanup_anomalies,
        trigger="cron",
        hour=3,
        minute=0,
        id="anomaly_cleanup",
        name="Clean old anomalies at 3AM",
        replace_existing=True,
    )

    try:
        _scheduler.start()
        logger.info("AI scheduler started")
    except Exception:
        logger.exception("Failed to start AI scheduler")

    atexit.register(lambda: _scheduler.shutdown(wait=False) if _scheduler else None)


def _check_anomalies():
    db = _mongo_db
    if db is None:
        return
    try:
        from .detector import run_anomaly_detection
        anomalies = run_anomaly_detection(db)
        if anomalies:
            for a in anomalies:
                db.ai_anomalies.insert_one({
                    **a,
                    "detected_at": datetime.utcnow(),
                    "acknowledged": False,
                })
            logger.info("Logged %d anomalies", len(anomalies))
    except Exception:
        logger.exception("Scheduled anomaly check failed")


def _cleanup_anomalies():
    db = _mongo_db
    if db is None:
        return
    try:
        cutoff = datetime.utcnow() - timedelta(days=30)
        result = db.ai_anomalies.delete_many({"detected_at": {"$lt": cutoff}})
        logger.info("Cleaned %d old anomaly records", result.deleted_count)
    except Exception:
        logger.exception("Scheduled anomaly cleanup failed")
