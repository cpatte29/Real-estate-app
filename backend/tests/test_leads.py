"""
Tests for the lead review / investor CRM workflow.

Covers:
  - LeadReviewStatus enum values
  - LeadReview dataclass defaults
  - state module CRUD helpers (register_leads, get_lead, update_lead_review)
  - All 5 FastAPI endpoints via TestClient
  - Status filtering, pagination, PATCH validation
  - reviewed_at auto-set on update
  - Stats counts
  - Dashboard HTML response
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api import state as _state
from app.models.lead_review import (
    REVIEW_STATUS_COLORS,
    REVIEW_STATUS_LABELS,
    LeadRecord,
    LeadReview,
    LeadReviewStatus,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers — minimal fake pipeline result that satisfies serialisation
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class _FakeProp:
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    street_address: str = "123 Main St"
    city: str = "Memphis"
    state: str = "TN"
    zip: str = "38104"
    bedrooms: int = 3
    bathrooms: float = 2.0
    sqft: int = 1400
    year_built: int = 1985
    list_price: float = 95_000.0
    distress_score: int = 75
    distress_types: list = field(default_factory=list)
    latitude: Optional[float] = 35.15
    longitude: Optional[float] = -90.05
    address_fingerprint: str = "abc123"


@dataclass
class _FakeDeal:
    opportunity_score: float = 72.0
    estimated_profit: float = 28_000.0
    max_allowable_offer: float = 82_000.0
    arv: float = 160_000.0
    rehab_cost: float = 30_000.0
    roi_pct: float = 25.0


@dataclass
class _FakeARV:
    arv: float = 160_000.0
    confidence: str = "high"
    confidence_score: float = 85.0
    comp_count: int = 5
    comps: list = field(default_factory=list)


@dataclass
class _FakeRehab:
    total_cost: float = 30_000.0
    rehab_level: str = "moderate"
    cost_per_sqft: float = 21.43


@dataclass
class _FakeResult:
    property: _FakeProp = field(default_factory=_FakeProp)
    deal_analysis: _FakeDeal = field(default_factory=_FakeDeal)
    arv_result: _FakeARV = field(default_factory=_FakeARV)
    rehab_estimate: _FakeRehab = field(default_factory=_FakeRehab)
    accepted: bool = True
    verification: object = None


def _make_result(**kwargs) -> _FakeResult:
    prop = _FakeProp(**{k: v for k, v in kwargs.items() if hasattr(_FakeProp, k) or k == "id"})
    return _FakeResult(property=prop)


def _make_lead(run_id: str = "run-1", run_date: str = "2026-01-01") -> tuple[str, _FakeResult]:
    result = _FakeResult()
    lead_id = str(result.property.id)
    return lead_id, result


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_stores():
    """Isolate each test — clear both stores before and after."""
    _state.clear_stores()
    yield
    _state.clear_stores()


@pytest.fixture
def client():
    from app.main import create_app
    app = create_app()
    return TestClient(app)


@pytest.fixture
def one_lead():
    """Register a single lead and return (lead_id, result)."""
    result = _FakeResult()
    lead_id = str(result.property.id)
    _state.register_leads("run-1", "2026-01-01", [result])
    return lead_id, result


@pytest.fixture
def three_leads():
    """Register 3 leads with distinct scores across two runs."""
    results = [_FakeResult() for _ in range(3)]
    results[0].deal_analysis.opportunity_score = 90.0
    results[1].deal_analysis.opportunity_score = 60.0
    results[2].deal_analysis.opportunity_score = 45.0
    _state.register_leads("run-1", "2026-01-01", [results[0], results[1]])
    _state.register_leads("run-2", "2026-01-02", [results[2]])
    return [(str(r.property.id), r) for r in results]


# ──────────────────────────────────────────────────────────────────────────────
# LeadReviewStatus enum
# ──────────────────────────────────────────────────────────────────────────────

class TestLeadReviewStatus:
    def test_all_values_present(self):
        values = {s.value for s in LeadReviewStatus}
        assert values == {
            "new", "reviewed", "rejected_by_human",
            "contacted", "under_contract", "closed"
        }

    def test_is_str_enum(self):
        assert isinstance(LeadReviewStatus.NEW, str)
        assert LeadReviewStatus.NEW == "new"

    def test_labels_cover_all_statuses(self):
        for s in LeadReviewStatus:
            assert s in REVIEW_STATUS_LABELS

    def test_colors_cover_all_statuses(self):
        for s in LeadReviewStatus:
            assert s in REVIEW_STATUS_COLORS

    def test_color_is_hex_string(self):
        for color in REVIEW_STATUS_COLORS.values():
            assert color.startswith("#"), f"Expected hex color, got {color}"


# ──────────────────────────────────────────────────────────────────────────────
# LeadReview dataclass defaults
# ──────────────────────────────────────────────────────────────────────────────

class TestLeadReviewDefaults:
    def test_default_status_is_new(self):
        r = LeadReview()
        assert r.review_status == LeadReviewStatus.NEW

    def test_default_notes_empty(self):
        assert LeadReview().human_notes == ""

    def test_default_reviewed_at_none(self):
        assert LeadReview().reviewed_at is None

    def test_default_reviewed_by_empty(self):
        assert LeadReview().reviewed_by == ""


# ──────────────────────────────────────────────────────────────────────────────
# state module — register_leads / get_lead
# ──────────────────────────────────────────────────────────────────────────────

class TestRegisterLeads:
    def test_returns_count_of_added(self):
        results = [_FakeResult(), _FakeResult()]
        added = _state.register_leads("run-1", "2026-01-01", results)
        assert added == 2

    def test_skip_duplicate_lead_id(self):
        result = _FakeResult()
        _state.register_leads("run-1", "2026-01-01", [result])
        added = _state.register_leads("run-2", "2026-01-02", [result])
        assert added == 0

    def test_lead_id_equals_property_uuid(self):
        result = _FakeResult()
        _state.register_leads("run-1", "2026-01-01", [result])
        lead_id = str(result.property.id)
        record = _state.get_lead(lead_id)
        assert record is not None
        assert record.lead_id == lead_id

    def test_get_lead_returns_none_for_unknown(self):
        assert _state.get_lead("not-a-real-id") is None

    def test_get_all_leads_empty_initially(self):
        assert _state.get_all_leads() == []

    def test_get_all_leads_returns_all(self):
        results = [_FakeResult(), _FakeResult(), _FakeResult()]
        _state.register_leads("run-1", "2026-01-01", results)
        assert len(_state.get_all_leads()) == 3

    def test_lead_record_has_new_status(self):
        result = _FakeResult()
        _state.register_leads("run-1", "2026-01-01", [result])
        record = _state.get_lead(str(result.property.id))
        assert record.review.review_status == LeadReviewStatus.NEW

    def test_lead_record_stores_run_metadata(self):
        result = _FakeResult()
        _state.register_leads("run-42", "2026-06-15", [result])
        record = _state.get_lead(str(result.property.id))
        assert record.run_id == "run-42"
        assert record.run_date == "2026-06-15"


# ──────────────────────────────────────────────────────────────────────────────
# state module — update_lead_review
# ──────────────────────────────────────────────────────────────────────────────

class TestUpdateLeadReview:
    def test_update_status(self, one_lead):
        lead_id, _ = one_lead
        record = _state.update_lead_review(lead_id, review_status=LeadReviewStatus.REVIEWED)
        assert record.review.review_status == LeadReviewStatus.REVIEWED

    def test_update_notes(self, one_lead):
        lead_id, _ = one_lead
        _state.update_lead_review(lead_id, human_notes="Great deal!")
        record = _state.get_lead(lead_id)
        assert record.review.human_notes == "Great deal!"

    def test_update_reviewed_by(self, one_lead):
        lead_id, _ = one_lead
        _state.update_lead_review(lead_id, reviewed_by="alice@example.com")
        record = _state.get_lead(lead_id)
        assert record.review.reviewed_by == "alice@example.com"

    def test_reviewed_at_set_on_change(self, one_lead):
        lead_id, _ = one_lead
        before = datetime.utcnow()
        _state.update_lead_review(lead_id, review_status=LeadReviewStatus.CONTACTED)
        after = datetime.utcnow()
        record = _state.get_lead(lead_id)
        assert record.review.reviewed_at is not None
        assert before <= record.review.reviewed_at <= after

    def test_reviewed_at_not_set_when_nothing_changed(self, one_lead):
        lead_id, _ = one_lead
        _state.update_lead_review(lead_id)
        record = _state.get_lead(lead_id)
        assert record.review.reviewed_at is None

    def test_returns_none_for_unknown_lead(self):
        result = _state.update_lead_review("unknown-id", review_status=LeadReviewStatus.CLOSED)
        assert result is None

    def test_multiple_fields_updated_atomically(self, one_lead):
        lead_id, _ = one_lead
        _state.update_lead_review(
            lead_id,
            review_status=LeadReviewStatus.UNDER_CONTRACT,
            human_notes="Signed contract",
            reviewed_by="bob@example.com",
        )
        record = _state.get_lead(lead_id)
        assert record.review.review_status == LeadReviewStatus.UNDER_CONTRACT
        assert record.review.human_notes == "Signed contract"
        assert record.review.reviewed_by == "bob@example.com"

    def test_status_transition_sequence(self, one_lead):
        lead_id, _ = one_lead
        for status in [
            LeadReviewStatus.REVIEWED,
            LeadReviewStatus.CONTACTED,
            LeadReviewStatus.UNDER_CONTRACT,
            LeadReviewStatus.CLOSED,
        ]:
            _state.update_lead_review(lead_id, review_status=status)
            assert _state.get_lead(lead_id).review.review_status == status


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/v1/leads/stats
# ──────────────────────────────────────────────────────────────────────────────

class TestStatsEndpoint:
    def test_empty_stats(self, client):
        resp = client.get("/api/v1/leads/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0

    def test_counts_by_status(self, client, three_leads):
        ids = [lid for lid, _ in three_leads]
        _state.update_lead_review(ids[0], review_status=LeadReviewStatus.REVIEWED)
        _state.update_lead_review(ids[1], review_status=LeadReviewStatus.CONTACTED)
        # ids[2] stays NEW

        resp = client.get("/api/v1/leads/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert data["by_status"]["new"] == 1
        assert data["by_status"]["reviewed"] == 1
        assert data["by_status"]["contacted"] == 1

    def test_all_statuses_present_in_response(self, client, one_lead):
        resp = client.get("/api/v1/leads/stats")
        data = resp.json()
        for s in LeadReviewStatus:
            assert s.value in data["by_status"]


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/v1/leads/dashboard
# ──────────────────────────────────────────────────────────────────────────────

class TestDashboardEndpoint:
    def test_returns_html(self, client):
        resp = client.get("/api/v1/leads/dashboard")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_html_contains_key_elements(self, client, one_lead):
        resp = client.get("/api/v1/leads/dashboard")
        html = resp.text
        assert "<html" in html.lower() or "<!doctype" in html.lower() or "<body" in html.lower()

    def test_empty_dashboard_loads(self, client):
        resp = client.get("/api/v1/leads/dashboard")
        assert resp.status_code == 200


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/v1/leads  (list)
# ──────────────────────────────────────────────────────────────────────────────

class TestListLeadsEndpoint:
    def test_empty_list(self, client):
        resp = client.get("/api/v1/leads")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["leads"] == []

    def test_returns_all_leads(self, client, three_leads):
        resp = client.get("/api/v1/leads")
        assert resp.status_code == 200
        assert resp.json()["total"] == 3

    def test_filter_by_status(self, client, three_leads):
        lead_id = three_leads[0][0]
        _state.update_lead_review(lead_id, review_status=LeadReviewStatus.REVIEWED)

        resp = client.get("/api/v1/leads?status=reviewed")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["leads"][0]["lead_id"] == lead_id

    def test_filter_by_status_no_match(self, client, three_leads):
        resp = client.get("/api/v1/leads?status=closed")
        data = resp.json()
        assert data["total"] == 0

    def test_filter_by_run_id(self, client, three_leads):
        resp = client.get("/api/v1/leads?run_id=run-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2

    def test_filter_by_min_score(self, client, three_leads):
        resp = client.get("/api/v1/leads?min_score=70")
        data = resp.json()
        assert data["total"] == 1
        assert data["leads"][0]["deal"]["opportunity_score"] >= 70

    def test_pagination_limit(self, client, three_leads):
        resp = client.get("/api/v1/leads?limit=2")
        data = resp.json()
        assert len(data["leads"]) == 2
        assert data["total"] == 3

    def test_pagination_offset(self, client, three_leads):
        resp = client.get("/api/v1/leads?limit=10&offset=2")
        data = resp.json()
        assert len(data["leads"]) == 1

    def test_lead_fields_present(self, client, one_lead):
        resp = client.get("/api/v1/leads")
        lead = resp.json()["leads"][0]
        assert "lead_id" in lead
        assert "review" in lead
        assert "deal" in lead
        assert "property" in lead

    def test_review_status_in_lead(self, client, one_lead):
        resp = client.get("/api/v1/leads")
        lead = resp.json()["leads"][0]
        assert lead["review"]["review_status"] == "new"


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/v1/leads/{lead_id}
# ──────────────────────────────────────────────────────────────────────────────

class TestGetLeadEndpoint:
    def test_get_existing_lead(self, client, one_lead):
        lead_id, _ = one_lead
        resp = client.get(f"/api/v1/leads/{lead_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["lead_id"] == lead_id

    def test_get_unknown_lead_404(self, client):
        resp = client.get("/api/v1/leads/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404

    def test_get_lead_includes_comps_key(self, client, one_lead):
        lead_id, _ = one_lead
        resp = client.get(f"/api/v1/leads/{lead_id}")
        data = resp.json()
        assert "comps" in data

    def test_get_lead_includes_arv(self, client, one_lead):
        lead_id, _ = one_lead
        resp = client.get(f"/api/v1/leads/{lead_id}")
        data = resp.json()
        assert "arv" in data


# ──────────────────────────────────────────────────────────────────────────────
# PATCH /api/v1/leads/{lead_id}/review
# ──────────────────────────────────────────────────────────────────────────────

class TestPatchReviewEndpoint:
    def test_update_status(self, client, one_lead):
        lead_id, _ = one_lead
        resp = client.patch(
            f"/api/v1/leads/{lead_id}/review",
            json={"review_status": "reviewed"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["review"]["review_status"] == "reviewed"

    def test_update_notes(self, client, one_lead):
        lead_id, _ = one_lead
        resp = client.patch(
            f"/api/v1/leads/{lead_id}/review",
            json={"human_notes": "Looks promising!"},
        )
        assert resp.status_code == 200
        assert resp.json()["review"]["human_notes"] == "Looks promising!"

    def test_update_reviewed_by(self, client, one_lead):
        lead_id, _ = one_lead
        resp = client.patch(
            f"/api/v1/leads/{lead_id}/review",
            json={"reviewed_by": "investor@example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["review"]["reviewed_by"] == "investor@example.com"

    def test_patch_sets_reviewed_at(self, client, one_lead):
        lead_id, _ = one_lead
        resp = client.patch(
            f"/api/v1/leads/{lead_id}/review",
            json={"review_status": "contacted"},
        )
        assert resp.status_code == 200
        assert resp.json()["review"]["reviewed_at"] is not None

    def test_patch_unknown_lead_404(self, client):
        resp = client.patch(
            "/api/v1/leads/no-such-id/review",
            json={"review_status": "reviewed"},
        )
        assert resp.status_code == 404

    def test_patch_invalid_status_422(self, client, one_lead):
        lead_id, _ = one_lead
        resp = client.patch(
            f"/api/v1/leads/{lead_id}/review",
            json={"review_status": "not_a_real_status"},
        )
        assert resp.status_code == 422

    def test_patch_empty_body_rejected(self, client, one_lead):
        lead_id, _ = one_lead
        resp = client.patch(f"/api/v1/leads/{lead_id}/review", json={})
        assert resp.status_code == 422

    def test_patch_persists_across_get(self, client, one_lead):
        lead_id, _ = one_lead
        client.patch(
            f"/api/v1/leads/{lead_id}/review",
            json={"review_status": "under_contract", "human_notes": "Offer accepted"},
        )
        resp = client.get(f"/api/v1/leads/{lead_id}")
        review = resp.json()["review"]
        assert review["review_status"] == "under_contract"
        assert review["human_notes"] == "Offer accepted"

    def test_all_valid_statuses_accepted(self, client, one_lead):
        lead_id, _ = one_lead
        for status in LeadReviewStatus:
            resp = client.patch(
                f"/api/v1/leads/{lead_id}/review",
                json={"review_status": status.value},
            )
            assert resp.status_code == 200, f"Failed for status={status.value}"
