"""
Pydantic data models for the scanner pipeline.
These are in-memory transfer objects, separate from ORM models.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4


class DistressType(str, Enum):
    PRE_FORECLOSURE = "pre_foreclosure"
    TAX_LIEN = "tax_lien"
    AUCTION = "auction"
    CODE_VIOLATION = "code_violation"
    VACANCY = "vacancy"
    PROBATE = "probate"
    BANKRUPTCY = "bankruptcy"
    DIVORCE = "divorce"


class ARVConfidence(str, Enum):
    HIGH = "high"           # 6+ comps
    MEDIUM = "medium"       # 3-5 comps
    LOW = "low"             # 1-2 comps
    INSUFFICIENT = "insufficient"  # 0 comps


class RehabLevel(str, Enum):
    COSMETIC = "cosmetic"
    MODERATE = "moderate"
    FULL = "full"
    UNKNOWN = "unknown"


class RejectionReason(str, Enum):
    MISSING_ADDRESS = "missing_address"
    INSUFFICIENT_COMPS = "insufficient_comps"
    LOW_ARV_CONFIDENCE = "low_arv_confidence"
    INSUFFICIENT_PROFIT = "insufficient_profit"
    MISSING_REPAIR_ESTIMATE = "missing_repair_estimate"
    DUPLICATE = "duplicate"
    NEGATIVE_EQUITY = "negative_equity"
    BAD_DATA = "bad_data"
    FAILED_MAO_RULE = "failed_mao_rule"


# ──────────────────────────────────────────────────────────────────────────────
# Property
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class CandidateProperty:
    street_address: str
    city: str
    state: str
    zip: str
    county: str = ""
    apn: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    property_type: str = "sfr"
    bedrooms: Optional[int] = None
    bathrooms: Optional[float] = None
    sqft: Optional[int] = None
    lot_sqft: Optional[int] = None
    year_built: Optional[int] = None
    garage_spaces: int = 0
    pool: bool = False

    assessed_value: Optional[float] = None
    estimated_value: Optional[float] = None
    tax_annual: Optional[float] = None

    owner_name: str = ""
    owner_occupied: Optional[bool] = None
    ownership_length_yrs: Optional[float] = None

    last_sale_date: Optional[date] = None
    last_sale_price: Optional[float] = None
    estimated_equity_pct: Optional[float] = None

    distress_types: list[DistressType] = field(default_factory=list)
    distress_score: int = 0   # 0-100

    data_source: str = "simulated"
    source_record_id: str = ""

    # assigned at pipeline entry
    id: UUID = field(default_factory=uuid4)
    address_fingerprint: str = ""


# ──────────────────────────────────────────────────────────────────────────────
# Comparable Sale
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ComparableSale:
    subject_property_id: UUID
    address: str
    city: str
    state: str
    zip: str
    sale_date: date
    sale_price: float
    sqft: Optional[int] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[float] = None
    year_built: Optional[int] = None
    property_type: str = "sfr"
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    price_per_sqft: Optional[float] = None
    distance_miles: Optional[float] = None
    similarity_score: float = 0.0
    price_adjustment: float = 0.0
    adjusted_price: float = 0.0
    data_source: str = "simulated"
    id: UUID = field(default_factory=uuid4)


# ──────────────────────────────────────────────────────────────────────────────
# ARV Calculation Result
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ARVResult:
    property_id: UUID
    scanner_run_id: UUID
    arv: float
    confidence: ARVConfidence
    confidence_score: float          # 0-100
    comp_count: int
    price_per_sqft_avg: float
    price_per_sqft_stddev: float
    comps: list[ComparableSale] = field(default_factory=list)
    radius_miles: float = 0.5
    date_range_months: int = 6
    sqft_tolerance_pct: int = 20
    id: UUID = field(default_factory=uuid4)


# ──────────────────────────────────────────────────────────────────────────────
# Rehab Estimate
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class RehabLineItem:
    category: str
    description: str
    cost: float


@dataclass
class RehabEstimate:
    property_id: UUID
    scanner_run_id: UUID
    rehab_level: RehabLevel
    total_cost: float
    cost_per_sqft: float
    regional_multiplier: float
    line_items: list[RehabLineItem] = field(default_factory=list)
    condition_signals: dict = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)


# ──────────────────────────────────────────────────────────────────────────────
# Deal Analysis
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ScoreBreakdown:
    profit_margin_component: float   # 0-30
    roi_component: float             # 0-25
    arv_confidence_component: float  # 0-20
    distress_component: float        # 0-15
    equity_spread_component: float   # 0-10
    total: float                     # 0-100


@dataclass
class DealAnalysis:
    property_id: UUID
    scanner_run_id: UUID

    list_price: Optional[float]
    arv: float
    rehab_cost: float
    holding_months: int

    # Holding cost components
    monthly_taxes: float
    monthly_insurance: float
    monthly_utilities: float
    monthly_mortgage: float
    total_holding_cost: float

    # Transaction costs (pct inputs)
    closing_cost_buy_pct: float
    closing_cost_sell_pct: float
    agent_commission_pct: float

    # Transaction costs (dollar amounts)
    closing_cost_buy: float
    closing_cost_sell: float
    agent_commission: float

    # Core outputs
    max_allowable_offer: float
    estimated_profit: float
    roi_pct: float
    cash_invested: float
    equity_spread: float          # list_price - MAO (negative = deal)

    # Score
    opportunity_score: float      # 0-100
    score_breakdown: ScoreBreakdown

    id: UUID = field(default_factory=uuid4)


# ──────────────────────────────────────────────────────────────────────────────
# Verification Result
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class VerificationResult:
    passed: bool
    rejection_reason: Optional[RejectionReason] = None
    rejection_detail: str = ""


# ──────────────────────────────────────────────────────────────────────────────
# Pipeline Result — one per property evaluated
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PropertyPipelineResult:
    property: CandidateProperty
    arv_result: Optional[ARVResult] = None
    rehab_estimate: Optional[RehabEstimate] = None
    deal_analysis: Optional[DealAnalysis] = None
    verification: Optional[VerificationResult] = None
    accepted: bool = False
    processed_at: datetime = field(default_factory=datetime.utcnow)


# ──────────────────────────────────────────────────────────────────────────────
# Scanner Run State — in-memory across iterations
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ScannerRunState:
    run_id: UUID
    run_date: date
    iteration: int = 0
    candidates_found: int = 0
    candidates_checked: int = 0
    leads_accepted: int = 0
    leads_rejected: int = 0
    checked_property_ids: set[str] = field(default_factory=set)
    accepted_leads: list[PropertyPipelineResult] = field(default_factory=list)
    rejected_leads: list[tuple[PropertyPipelineResult, RejectionReason]] = field(default_factory=list)
    rejection_breakdown: dict[str, int] = field(default_factory=dict)
    stop_reason: Optional[str] = None
    started_at: datetime = field(default_factory=datetime.utcnow)

    def record_rejection(self, reason: RejectionReason):
        self.leads_rejected += 1
        key = reason.value
        self.rejection_breakdown[key] = self.rejection_breakdown.get(key, 0) + 1

    def should_stop(self, config) -> tuple[bool, str]:
        if self.leads_accepted >= config.max_qualified_leads:
            return True, "qualified_limit"
        if self.candidates_checked >= config.max_candidates_checked:
            return True, "candidate_limit"
        if self.iteration >= config.max_iterations:
            return True, "iteration_limit"
        return False, ""
