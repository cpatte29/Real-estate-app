"""
Lead review / CRM models.

These live outside the scanner package so the scanner pipeline stays
unaware of review workflow — review state is purely an API/CRM concern.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class LeadReviewStatus(str, Enum):
    NEW             = "new"
    REVIEWED        = "reviewed"
    REJECTED_BY_HUMAN = "rejected_by_human"
    CONTACTED       = "contacted"
    UNDER_CONTRACT  = "under_contract"
    CLOSED          = "closed"


# Human-readable labels for the dashboard
REVIEW_STATUS_LABELS: dict[LeadReviewStatus, str] = {
    LeadReviewStatus.NEW:               "New",
    LeadReviewStatus.REVIEWED:          "Reviewed",
    LeadReviewStatus.REJECTED_BY_HUMAN: "Passed",
    LeadReviewStatus.CONTACTED:         "Contacted",
    LeadReviewStatus.UNDER_CONTRACT:    "Under Contract",
    LeadReviewStatus.CLOSED:            "Closed / Won",
}

# Badge colours for the dashboard (CSS colour strings)
REVIEW_STATUS_COLORS: dict[LeadReviewStatus, str] = {
    LeadReviewStatus.NEW:               "#6b7280",  # grey
    LeadReviewStatus.REVIEWED:          "#3b82f6",  # blue
    LeadReviewStatus.REJECTED_BY_HUMAN: "#ef4444",  # red
    LeadReviewStatus.CONTACTED:         "#f59e0b",  # amber
    LeadReviewStatus.UNDER_CONTRACT:    "#8b5cf6",  # purple
    LeadReviewStatus.CLOSED:            "#22c55e",  # green
}


@dataclass
class LeadReview:
    """Mutable review state attached to a scanner lead."""
    review_status: LeadReviewStatus = LeadReviewStatus.NEW
    human_notes:   str = ""
    reviewed_at:   Optional[datetime] = None
    reviewed_by:   str = ""


@dataclass
class LeadRecord:
    """
    A scanner-accepted lead plus its CRM review state.

    ``lead_id`` equals ``str(result.property.id)`` — the UUID the pipeline
    assigned to the candidate property. It is stable for the lifetime of the
    in-memory store and used as the REST resource identifier.
    """
    lead_id:    str
    run_id:     str
    run_date:   str
    result:     object          # PropertyPipelineResult (avoid circular import)
    review:     LeadReview = field(default_factory=LeadReview)
    created_at: datetime   = field(default_factory=datetime.utcnow)
