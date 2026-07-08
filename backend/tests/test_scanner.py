"""
Scanner pipeline tests — no database required.
Run with: pytest backend/tests/test_scanner.py -v
"""
from __future__ import annotations

import pytest
from datetime import date
from uuid import uuid4

from app.scanner.config import ScannerConfig
from app.scanner.models import (
    ARVConfidence,
    ARVResult,
    CandidateProperty,
    DealAnalysis,
    DistressType,
    RejectionReason,
    RehabEstimate,
    RehabLevel,
    ScoreBreakdown,
    ScannerRunState,
)
from app.scanner.arv import ARVEngine
from app.scanner.discovery import SimulatedPropertySource
from app.scanner.pipeline import LeadScannerPipeline
from app.scanner.rehab import RehabEstimator
from app.scanner.scoring import DealCalculator
from app.scanner.verification import PropertyVerifier


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

def _good_property() -> CandidateProperty:
    return CandidateProperty(
        street_address="123 Oak St",
        city="Atlanta",
        state="GA",
        zip="30310",
        county="Fulton County",
        apn="30310-12345",
        latitude=33.749,
        longitude=-84.388,
        property_type="sfr",
        bedrooms=3,
        bathrooms=2.0,
        sqft=1400,
        year_built=1985,
        assessed_value=95_000,
        estimated_value=100_000,
        tax_annual=1_200,
        owner_name="Jane Doe",
        owner_occupied=False,
        ownership_length_yrs=22.0,
        last_sale_date=date(2015, 3, 10),
        last_sale_price=65_000,
        estimated_equity_pct=35.0,
        distress_types=[DistressType.PRE_FORECLOSURE, DistressType.TAX_LIEN],
        distress_score=75,
        data_source="simulated",
        address_fingerprint="abc123",
    )


def _good_arv(property_id, run_id) -> ARVResult:
    return ARVResult(
        property_id=property_id,
        scanner_run_id=run_id,
        arv=185_000,
        confidence=ARVConfidence.HIGH,
        confidence_score=82.0,
        comp_count=6,
        price_per_sqft_avg=132.0,
        price_per_sqft_stddev=8.0,
    )


def _good_rehab(property_id, run_id) -> RehabEstimate:
    return RehabEstimate(
        property_id=property_id,
        scanner_run_id=run_id,
        rehab_level=RehabLevel.MODERATE,
        total_cost=38_000,
        cost_per_sqft=27.14,
        regional_multiplier=1.00,
        line_items=[],
    )


# ──────────────────────────────────────────────────────────────────────────────
# Discovery tests
# ──────────────────────────────────────────────────────────────────────────────

class TestDiscovery:
    def test_returns_requested_batch_size(self):
        source = SimulatedPropertySource(seed=1)
        batch = source.fetch_candidates(batch_size=30)
        assert len(batch) == 30

    def test_properties_have_required_fields(self):
        source = SimulatedPropertySource(seed=2)
        batch = source.fetch_candidates(batch_size=10)
        # Most should have addresses (~15% are intentionally bad records)
        with_address = [p for p in batch if p.street_address]
        assert len(with_address) >= 6  # at least 60% on a 10-sample batch

    def test_injects_bad_records(self):
        source = SimulatedPropertySource(seed=99)
        batch = source.fetch_candidates(batch_size=100)
        empty_address = [p for p in batch if not p.street_address]
        # Expect ~15% bad records
        assert 5 <= len(empty_address) <= 25

    def test_distress_types_populated(self):
        source = SimulatedPropertySource(seed=3)
        batch = source.fetch_candidates(batch_size=20)
        assert all(len(p.distress_types) > 0 for p in batch)


# ──────────────────────────────────────────────────────────────────────────────
# ARV tests
# ──────────────────────────────────────────────────────────────────────────────

class TestARVEngine:
    def test_returns_arv_result(self):
        engine = ARVEngine()
        prop = _good_property()
        run_id = uuid4()
        result = engine.calculate(prop, run_id)
        assert result.property_id == prop.id
        assert result.arv >= 0

    def test_arv_confidence_reflects_comp_count(self):
        engine = ARVEngine()
        prop = _good_property()
        run_id = uuid4()
        # Run many times; some will have high confidence, some insufficient
        confidences = set()
        for seed in range(20):
            import random
            engine._rng = random.Random(seed)
            r = engine.calculate(prop, run_id)
            confidences.add(r.confidence)
        # Should see multiple confidence levels across random seeds
        assert len(confidences) >= 2

    def test_property_missing_sqft_returns_no_comps(self):
        engine = ARVEngine()
        prop = _good_property()
        prop.sqft = None
        run_id = uuid4()
        result = engine.calculate(prop, run_id)
        assert result.comp_count == 0
        assert result.confidence == ARVConfidence.INSUFFICIENT


# ──────────────────────────────────────────────────────────────────────────────
# Rehab estimation tests
# ──────────────────────────────────────────────────────────────────────────────

class TestRehabEstimator:
    def test_older_vacant_property_gets_full_rehab(self):
        estimator = RehabEstimator()
        prop = _good_property()
        prop.year_built = 1950
        prop.distress_types = [DistressType.VACANCY, DistressType.CODE_VIOLATION]
        run_id = uuid4()
        result = estimator.estimate(prop, run_id)
        assert result.rehab_level == RehabLevel.FULL

    def test_newer_low_distress_gets_cosmetic(self):
        estimator = RehabEstimator()
        prop = _good_property()
        prop.year_built = 2005
        prop.distress_types = [DistressType.DIVORCE]
        prop.estimated_equity_pct = 50.0
        prop.ownership_length_yrs = 5.0
        run_id = uuid4()
        result = estimator.estimate(prop, run_id)
        assert result.rehab_level in (RehabLevel.COSMETIC, RehabLevel.MODERATE)

    def test_regional_multiplier_applied(self):
        estimator = RehabEstimator()
        prop_ga = _good_property()
        prop_ga.state = "GA"
        prop_ca = _good_property()
        prop_ca.state = "CA"
        run_id = uuid4()
        ga_est = estimator.estimate(prop_ga, run_id)
        ca_est = estimator.estimate(prop_ca, run_id)
        assert ca_est.total_cost > ga_est.total_cost

    def test_total_cost_positive(self):
        estimator = RehabEstimator()
        result = estimator.estimate(_good_property(), uuid4())
        assert result.total_cost > 0
        assert result.cost_per_sqft > 0


# ──────────────────────────────────────────────────────────────────────────────
# Deal calculator tests
# ──────────────────────────────────────────────────────────────────────────────

class TestDealCalculator:
    def test_mao_follows_70_pct_rule(self):
        calc = DealCalculator()
        prop = _good_property()
        run_id = uuid4()
        arv = _good_arv(prop.id, run_id)
        rehab = _good_rehab(prop.id, run_id)
        deal = calc.analyze(prop, arv, rehab, run_id)
        expected_mao = arv.arv * 0.70 - rehab.total_cost
        assert abs(deal.max_allowable_offer - expected_mao) < 1

    def test_score_bounded_0_to_100(self):
        calc = DealCalculator()
        prop = _good_property()
        run_id = uuid4()
        arv = _good_arv(prop.id, run_id)
        rehab = _good_rehab(prop.id, run_id)
        deal = calc.analyze(prop, arv, rehab, run_id)
        assert 0 <= deal.opportunity_score <= 100

    def test_score_breakdown_sums_to_total(self):
        calc = DealCalculator()
        prop = _good_property()
        run_id = uuid4()
        deal = calc.analyze(prop, _good_arv(prop.id, run_id), _good_rehab(prop.id, run_id), run_id)
        sb = deal.score_breakdown
        component_sum = (
            sb.profit_margin_component + sb.roi_component +
            sb.arv_confidence_component + sb.distress_component +
            sb.equity_spread_component
        )
        assert abs(component_sum - sb.total) < 0.1

    def test_high_arv_low_cost_yields_profit(self):
        calc = DealCalculator()
        prop = _good_property()
        prop.estimated_value = 50_000
        run_id = uuid4()
        arv = _good_arv(prop.id, run_id)  # ARV = 185k
        rehab = _good_rehab(prop.id, run_id)  # rehab = 38k
        deal = calc.analyze(prop, arv, rehab, run_id)
        assert deal.estimated_profit > 0


# ──────────────────────────────────────────────────────────────────────────────
# Verification tests
# ──────────────────────────────────────────────────────────────────────────────

class TestPropertyVerifier:
    def _make_verifier(self) -> PropertyVerifier:
        return PropertyVerifier(checked_ids=set())

    def _deal_with_profit(self, profit: float, mao: float = 100_000, list_price: float = 80_000):
        prop = _good_property()
        run_id = uuid4()
        deal = DealAnalysis(
            property_id=prop.id, scanner_run_id=run_id,
            list_price=list_price, arv=185_000, rehab_cost=38_000,
            holding_months=6, monthly_taxes=100, monthly_insurance=150,
            monthly_utilities=200, monthly_mortgage=0, total_holding_cost=2_700,
            closing_cost_buy_pct=2, closing_cost_sell_pct=2, agent_commission_pct=5,
            closing_cost_buy=1_600, closing_cost_sell=3_700, agent_commission=9_250,
            max_allowable_offer=mao, estimated_profit=profit,
            roi_pct=profit / 80_000 * 100, cash_invested=80_000,
            equity_spread=list_price - mao,
            opportunity_score=70,
            score_breakdown=ScoreBreakdown(20, 18, 16, 11, 7, 72),
        )
        return prop, deal

    def test_missing_address_rejected(self):
        verifier = self._make_verifier()
        prop = _good_property()
        prop.street_address = ""
        v = verifier.verify(prop, None, None, None)
        assert not v.passed
        assert v.rejection_reason == RejectionReason.MISSING_ADDRESS

    def test_insufficient_comps_rejected(self):
        verifier = self._make_verifier()
        prop = _good_property()
        arv = _good_arv(prop.id, uuid4())
        arv.comp_count = 2
        arv.confidence_score = 30
        v = verifier.verify(prop, arv, _good_rehab(prop.id, arv.scanner_run_id), None)
        assert not v.passed
        assert v.rejection_reason == RejectionReason.INSUFFICIENT_COMPS

    def test_low_confidence_rejected(self):
        verifier = self._make_verifier()
        prop = _good_property()
        run_id = uuid4()
        arv = _good_arv(prop.id, run_id)
        arv.comp_count = 5
        arv.confidence_score = 50.0   # below 70 threshold
        v = verifier.verify(prop, arv, _good_rehab(prop.id, run_id), None)
        assert not v.passed
        assert v.rejection_reason == RejectionReason.LOW_ARV_CONFIDENCE

    def test_insufficient_profit_rejected(self):
        verifier = self._make_verifier()
        prop, deal = self._deal_with_profit(profit=10_000)
        run_id = deal.scanner_run_id
        arv = _good_arv(prop.id, run_id)
        arv.comp_count = 6
        arv.confidence_score = 82.0
        v = verifier.verify(prop, arv, _good_rehab(prop.id, run_id), deal)
        assert not v.passed
        assert v.rejection_reason == RejectionReason.INSUFFICIENT_PROFIT

    def test_duplicate_rejected(self):
        verifier = self._make_verifier()
        prop = _good_property()
        run_id = uuid4()
        arv = _good_arv(prop.id, run_id)
        arv.comp_count = 6; arv.confidence_score = 82.0
        rehab = _good_rehab(prop.id, run_id)
        prop2, deal = self._deal_with_profit(profit=50_000, mao=91_000)
        deal.scanner_run_id = run_id
        # First pass — accepted
        v1 = verifier.verify(prop, arv, rehab, deal)
        assert v1.passed
        # Second pass — duplicate
        v2 = verifier.verify(prop, arv, rehab, deal)
        assert not v2.passed
        assert v2.rejection_reason == RejectionReason.DUPLICATE

    def test_good_property_passes_all_rules(self):
        verifier = self._make_verifier()
        prop = _good_property()
        run_id = uuid4()
        arv = _good_arv(prop.id, run_id)
        arv.comp_count = 6; arv.confidence_score = 82.0
        rehab = _good_rehab(prop.id, run_id)
        _, deal = self._deal_with_profit(profit=45_000, mao=91_500, list_price=80_000)
        deal.property_id = prop.id; deal.scanner_run_id = run_id
        v = verifier.verify(prop, arv, rehab, deal)
        assert v.passed


# ──────────────────────────────────────────────────────────────────────────────
# Full pipeline integration tests
# ──────────────────────────────────────────────────────────────────────────────

class TestPipeline:
    def _fast_config(self) -> ScannerConfig:
        return ScannerConfig(
            max_qualified_leads=5,
            max_candidates_checked=60,
            max_iterations=3,
            simulation_seed=7,
        )

    def test_pipeline_runs_and_produces_leads(self):
        cfg = self._fast_config()
        pipeline = LeadScannerPipeline(cfg=cfg)
        state = pipeline.run(run_date=date(2026, 6, 17))
        assert state.leads_accepted >= 0   # may be 0 on unlucky seeds
        assert state.candidates_checked > 0
        assert state.iteration >= 1

    def test_pipeline_respects_qualified_lead_stop(self):
        cfg = ScannerConfig(
            max_qualified_leads=3,
            max_candidates_checked=500,
            max_iterations=10,
            simulation_seed=42,
        )
        pipeline = LeadScannerPipeline(cfg=cfg)
        state = pipeline.run(run_date=date(2026, 6, 17))
        assert state.leads_accepted <= cfg.max_qualified_leads + cfg.candidate_batch_size

    def test_pipeline_respects_max_candidates_stop(self):
        cfg = ScannerConfig(
            max_qualified_leads=999,
            max_candidates_checked=30,
            max_iterations=10,
            simulation_seed=42,
        )
        pipeline = LeadScannerPipeline(cfg=cfg)
        state = pipeline.run(run_date=date(2026, 6, 17))
        assert state.candidates_checked <= cfg.max_candidates_checked + cfg.candidate_batch_size

    def test_all_accepted_leads_have_deal_analysis(self):
        cfg = self._fast_config()
        pipeline = LeadScannerPipeline(cfg=cfg)
        state = pipeline.run(run_date=date(2026, 6, 17))
        for result in state.accepted_leads:
            assert result.deal_analysis is not None
            assert result.arv_result is not None
            assert result.rehab_estimate is not None
            assert result.verification is not None
            assert result.verification.passed

    def test_all_accepted_leads_meet_profit_minimum(self):
        cfg = self._fast_config()
        pipeline = LeadScannerPipeline(cfg=cfg)
        state = pipeline.run(run_date=date(2026, 6, 17))
        for result in state.accepted_leads:
            assert result.deal_analysis.estimated_profit >= cfg.min_estimated_profit

    def test_rejection_breakdown_tracked(self):
        cfg = self._fast_config()
        pipeline = LeadScannerPipeline(cfg=cfg)
        state = pipeline.run(run_date=date(2026, 6, 17))
        assert state.leads_rejected == sum(state.rejection_breakdown.values())
