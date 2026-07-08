"""
Celery task — runs the daily lead scanner on a schedule.
Scheduled: 06:00 UTC every day (02:00 ET).
"""
from __future__ import annotations

import logging
from datetime import date

from celery import Celery
from celery.schedules import crontab

from app.scanner.config import config

logger = logging.getLogger(__name__)

celery = Celery("rei_scanner")
celery.config_from_object({
    "broker_url": "redis://localhost:6379/0",
    "result_backend": "redis://localhost:6379/0",
    "task_serializer": "json",
    "accept_content": ["json"],
    "timezone": "UTC",
    "task_track_started": True,
    "task_acks_late": True,       # re-queue on worker crash
    "worker_prefetch_multiplier": 1,
})

celery.conf.beat_schedule = {
    "daily-lead-scan": {
        "task": "app.workers.daily_scan.run_daily_scan",
        "schedule": crontab(hour=6, minute=0),
        "args": [],
    },
}


@celery.task(
    name="app.workers.daily_scan.run_daily_scan",
    bind=True,
    max_retries=2,
    default_retry_delay=300,   # retry after 5 minutes on transient failure
    soft_time_limit=3600,      # 1-hour hard cap
)
def run_daily_scan(self, run_date_str: str = None):
    """
    Celery task: run the full lead scanner pipeline and generate the report.

    Args:
        run_date_str: ISO date string, defaults to today. Useful for backfills.
    """
    from app.scanner.pipeline import LeadScannerPipeline
    from app.reports.generator import ReportGenerator

    run_date = date.fromisoformat(run_date_str) if run_date_str else date.today()

    logger.info("Celery daily scan starting | run_date=%s | task_id=%s",
                run_date, self.request.id)

    try:
        pipeline = LeadScannerPipeline()
        state = pipeline.run(run_date=run_date)

        gen = ReportGenerator()
        paths = gen.generate(state)

        result = {
            "run_id": str(state.run_id),
            "run_date": state.run_date.isoformat(),
            "leads_accepted": state.leads_accepted,
            "leads_rejected": state.leads_rejected,
            "candidates_checked": state.candidates_checked,
            "iterations": state.iteration,
            "stop_reason": state.stop_reason,
            "report_paths": paths,
        }

        logger.info(
            "Daily scan complete | leads=%d checked=%d report=%s",
            state.leads_accepted, state.candidates_checked, paths["html"],
        )
        return result

    except Exception as exc:
        logger.exception("Daily scan failed | run_date=%s", run_date)
        raise self.retry(exc=exc)
