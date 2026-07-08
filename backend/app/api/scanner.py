"""
FastAPI endpoints for the lead scanner.

GET  /api/v1/scanner/runs          — list historical runs
GET  /api/v1/scanner/runs/{run_id} — single run detail + stats
POST /api/v1/scanner/run           — trigger a new scan (async via Celery)
GET  /api/v1/scanner/leads         — paginated accepted leads across all runs
GET  /api/v1/scanner/leads/{id}    — single lead detail
GET  /api/v1/scanner/state         — current persistent scanner state
GET  /api/v1/scanner/runs/{run_id}/report  — HTML report for a run
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel

from app.scanner.config import config
from app.scanner.pipeline import LeadScannerPipeline
from app.reports.generator import ReportGenerator
import app.api.state as _state

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/scanner", tags=["scanner"])

_last_state = None


# ──────────────────────────────────────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────────────────────────────────────

class RunSummary(BaseModel):
    run_id: str
    run_date: str
    status: str
    candidates_checked: int
    leads_accepted: int
    leads_rejected: int
    iterations: int
    stop_reason: Optional[str]


class TriggerRunRequest(BaseModel):
    run_date: Optional[date] = None
    max_qualified_leads: Optional[int] = None
    max_candidates_checked: Optional[int] = None
    dry_run: bool = False


class TriggerRunResponse(BaseModel):
    run_id: str
    status: str
    message: str


# ──────────────────────────────────────────────────────────────────────────────
# Background task executor
# ──────────────────────────────────────────────────────────────────────────────

def _execute_scan(run_id: str, run_date: date, cfg_overrides: dict):
    global _last_state
    logger.info("Background scan starting | run_id=%s", run_id)
    _state.update_run(run_id, {"status": "running"})

    try:
        from app.scanner.config import ScannerConfig
        merged = {**config.model_dump(), **cfg_overrides}
        cfg = ScannerConfig(**merged)

        pipeline = LeadScannerPipeline(cfg=cfg)
        state = pipeline.run(run_date=run_date)
        _last_state = state

        gen = ReportGenerator()
        paths = gen.generate(state)

        run_date_str = run_date.isoformat() if hasattr(run_date, "isoformat") else str(run_date)
        added = _state.register_leads(run_id, run_date_str, state.accepted_leads)
        logger.info("Registered %d new CRM leads | run_id=%s", added, run_id)

        _state.update_run(run_id, {
            "status": "completed",
            "state": state,
            "report_paths": paths,
            "candidates_checked": state.candidates_checked,
            "leads_accepted": state.leads_accepted,
            "leads_rejected": state.leads_rejected,
            "iterations": state.iteration,
            "stop_reason": state.stop_reason,
            "rejection_breakdown": state.rejection_breakdown,
        })
        logger.info("Scan completed | run_id=%s | accepted=%d", run_id, state.leads_accepted)

    except Exception as exc:
        logger.exception("Scan failed | run_id=%s", run_id)
        _state.update_run(run_id, {"status": "failed", "error": str(exc)})


# ──────────────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────────────

@router.post("/run", response_model=TriggerRunResponse, status_code=202)
async def trigger_scan(
    req: TriggerRunRequest,
    background_tasks: BackgroundTasks,
):
    """Trigger a daily lead scanner run asynchronously."""
    from uuid import uuid4
    run_id = str(uuid4())
    run_date = req.run_date or date.today()

    overrides = {}
    if req.max_qualified_leads:
        overrides["max_qualified_leads"] = req.max_qualified_leads
    if req.max_candidates_checked:
        overrides["max_candidates_checked"] = req.max_candidates_checked

    _state.upsert_run(run_id, {
        "run_id": run_id,
        "run_date": run_date.isoformat(),
        "status": "queued",
        "candidates_checked": 0,
        "leads_accepted": 0,
        "leads_rejected": 0,
        "iterations": 0,
        "stop_reason": None,
    })

    if not req.dry_run:
        background_tasks.add_task(_execute_scan, run_id, run_date, overrides)

    return TriggerRunResponse(
        run_id=run_id,
        status="queued",
        message=f"Scan queued for {run_date}. Poll /runs/{run_id} for status.",
    )


@router.get("/runs")
async def list_runs(
    limit: int = Query(20, le=100),
    offset: int = 0,
):
    """List scanner runs, newest first."""
    runs = list(reversed(list(_state.get_run_store().values())))
    return {
        "total": len(runs),
        "runs": [
            {k: v for k, v in r.items() if k != "state"}
            for r in runs[offset : offset + limit]
        ],
    }


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    """Get detailed status for a specific run."""
    run = _state.get_run_store().get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    response = {k: v for k, v in run.items() if k not in ("state",)}
    return response


@router.get("/runs/{run_id}/report")
async def get_run_report(run_id: str):
    """Return the HTML report for a completed run."""
    from fastapi.responses import HTMLResponse

    run = _state.get_run_store().get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run["status"] != "completed":
        raise HTTPException(status_code=400, detail=f"Run status is '{run['status']}', not completed")

    state = run.get("state")
    if not state:
        raise HTTPException(status_code=500, detail="No state available for this run")

    from app.reports.generator import build_html_report
    html = build_html_report(state)
    return HTMLResponse(content=html)


@router.get("/leads")
async def list_leads(
    limit: int = Query(25, le=100),
    offset: int = 0,
    min_score: float = 0.0,
    state_filter: Optional[str] = None,
):
    """List accepted leads across all completed runs, sorted by score."""
    all_leads = []
    for run in _state.get_run_store().values():
        run_state = run.get("state")
        if not run_state:
            continue
        for result in run_state.accepted_leads:
            if result.deal_analysis and result.deal_analysis.opportunity_score >= min_score:
                if state_filter and result.property.state != state_filter.upper():
                    continue
                all_leads.append(result)

    all_leads.sort(
        key=lambda r: r.deal_analysis.opportunity_score if r.deal_analysis else 0,
        reverse=True,
    )

    from app.reports.generator import _lead_to_dict
    return {
        "total": len(all_leads),
        "leads": [_lead_to_dict(r) for r in all_leads[offset : offset + limit]],
    }


@router.get("/state")
async def get_scanner_state():
    """Return current scanner state across all runs."""
    run_store = _state.get_run_store()
    total_accepted = sum(r.get("leads_accepted", 0) for r in run_store.values())
    total_checked = sum(r.get("candidates_checked", 0) for r in run_store.values())
    completed_runs = [r for r in run_store.values() if r["status"] == "completed"]

    return {
        "total_runs": len(_run_store),
        "completed_runs": len(completed_runs),
        "total_candidates_checked_all_time": total_checked,
        "total_leads_accepted_all_time": total_accepted,
        "last_run_date": completed_runs[-1]["run_date"] if completed_runs else None,
        "config": {
            "max_qualified_leads": config.max_qualified_leads,
            "max_candidates_checked": config.max_candidates_checked,
            "max_iterations": config.max_iterations,
            "min_comp_count": config.min_comp_count,
            "min_arv_confidence_score": config.min_arv_confidence_score,
            "min_estimated_profit": config.min_estimated_profit,
            "mao_arv_multiplier": config.mao_arv_multiplier,
        },
    }
