"""
Shared in-memory state for the API layer.

Two stores:
  _run_store   — scanner run records (owned by scanner.py)
  _lead_store  — CRM lead records, keyed by lead_id (str of property UUID)

Both are module-level dicts. Tests that need isolation should call
clear_stores() in a fixture.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.models.lead_review import LeadRecord, LeadReview, LeadReviewStatus

# ── Stores ────────────────────────────────────────────────────────────────────

# Scanner run records — populated by scanner.py
_run_store: dict[str, dict] = {}

# CRM lead records — populated when scanner completes
_lead_store: dict[str, LeadRecord] = {}


# ── Run store helpers ─────────────────────────────────────────────────────────

def get_run_store() -> dict[str, dict]:
    return _run_store


def upsert_run(run_id: str, data: dict) -> None:
    _run_store[run_id] = data


def update_run(run_id: str, patch: dict) -> None:
    if run_id in _run_store:
        _run_store[run_id].update(patch)


# ── Lead store helpers ────────────────────────────────────────────────────────

def register_leads(run_id: str, run_date: str, accepted_leads: list) -> int:
    """
    Add scanner-accepted leads to the CRM store. Skips leads already present
    (a lead_id collision means the same property was accepted in a prior run).
    Returns the number of newly registered leads.
    """
    added = 0
    for result in accepted_leads:
        lead_id = str(result.property.id)
        if lead_id not in _lead_store:
            _lead_store[lead_id] = LeadRecord(
                lead_id=lead_id,
                run_id=run_id,
                run_date=run_date,
                result=result,
            )
            added += 1
    return added


def get_all_leads() -> list[LeadRecord]:
    return list(_lead_store.values())


def get_lead(lead_id: str) -> Optional[LeadRecord]:
    return _lead_store.get(lead_id)


def update_lead_review(
    lead_id: str,
    review_status: Optional[LeadReviewStatus] = None,
    human_notes: Optional[str] = None,
    reviewed_by: Optional[str] = None,
) -> Optional[LeadRecord]:
    """
    Update the review state for a lead. Sets reviewed_at to now whenever
    any field changes. Returns the updated record, or None if not found.
    """
    record = _lead_store.get(lead_id)
    if record is None:
        return None

    changed = False
    if review_status is not None:
        record.review.review_status = review_status
        changed = True
    if human_notes is not None:
        record.review.human_notes = human_notes
        changed = True
    if reviewed_by is not None:
        record.review.reviewed_by = reviewed_by
        changed = True

    if changed:
        record.review.reviewed_at = datetime.utcnow()

    return record


def clear_stores() -> None:
    """Reset both stores. Intended for use in tests only."""
    _run_store.clear()
    _lead_store.clear()
