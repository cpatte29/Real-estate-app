"""
Opportunity scoring and deal analysis engine.

Scoring formula (0-100):

  Component                  Weight   Basis
  ─────────────────────────  ──────   ───────────────────────────────────────
  Profit margin (%)           30 pts  profit / ARV — higher is better
  ROI (%)                     25 pts  profit / cash invested — benchmarked
  ARV confidence score        20 pts  normalized confidence_score (0-100)
  Distress score              15 pts  property distress signal strength
  Equity spread (MAO room)    10 pts  how far list price is below MAO

MAO (Max Allowable Offer) = ARV × 0.70 - rehab_cost

This is the "70% rule" standard in fix-and-flip investing.
"""
from __future__ import annotations

from uuid import UUID

from app.scanner.config import config
from app.scanner.models import (
    ARVResult,
    CandidateProperty,
    DealAnalysis,
    RehabEstimate,
    ScoreBreakdown,
)


# ──────────────────────────────────────────────────────────────────────────────
# Individual score component calculators
# ──────────────────────────────────────────────────────────────────────────────

def _profit_margin_score(profit: float, arv: float, max_pts: float = 30.0) -> float:
    """
    Maps profit margin (profit / ARV) → 0–30 points.
    0% margin → 0 pts, 30%+ margin → 30 pts (linear).
    """
    if arv <= 0:
        return 0.0
    margin = profit / arv
    # Full score at 30% margin
    return min(max_pts, max(0.0, (margin / 0.30) * max_pts))


def _roi_score(roi_pct: float, max_pts: float = 25.0) -> float:
    """
    Maps ROI % → 0–25 points.
    Benchmark: 20% ROI = good (20 pts), 40%+ = excellent (25 pts).
    """
    if roi_pct <= 0:
        return 0.0
    if roi_pct >= 40:
        return max_pts
    elif roi_pct >= 20:
        # 20-40% → 20-25 pts
        return 20.0 + ((roi_pct - 20) / 20) * 5.0
    else:
        # 0-20% → 0-20 pts
        return (roi_pct / 20) * 20.0


def _arv_confidence_score(confidence_score: float, max_pts: float = 20.0) -> float:
    """
    Maps numeric confidence score (0-100) → 0–20 points.
    """
    return (confidence_score / 100) * max_pts


def _distress_score_component(distress_score: int, max_pts: float = 15.0) -> float:
    """
    Maps property distress score (0-100) → 0–15 points.
    Higher distress = more motivated seller = better opportunity.
    """
    return (distress_score / 100) * max_pts


def _equity_spread_score(equity_spread: float, arv: float, max_pts: float = 10.0) -> float:
    """
    Equity spread = list_price - MAO.
    Negative spread means list price is BELOW MAO (excellent).
    Maps -$50k to +$10k spread → 10 pts to 0 pts.
    """
    if arv <= 0:
        return 0.0
    # Normalize by ARV to make it scale-independent
    spread_pct = equity_spread / arv
    # -0.20 (list $20k below MAO) → full 10 pts; +0.05 → 0 pts
    score = max_pts * ((-spread_pct + 0.05) / 0.25)
    return min(max_pts, max(0.0, score))


# ──────────────────────────────────────────────────────────────────────────────
# Deal calculator
# ──────────────────────────────────────────────────────────────────────────────

class DealCalculator:
    def analyze(
        self,
        prop: CandidateProperty,
        arv_result: ARVResult,
        rehab: RehabEstimate,
        scanner_run_id: UUID,
    ) -> DealAnalysis:
        arv = arv_result.arv
        rehab_cost = rehab.total_cost
        list_price = prop.estimated_value or prop.assessed_value or 0.0

        # ── Holding costs ───────────────────────────────────────────
        monthly_taxes = (prop.tax_annual or 0) / 12
        monthly_insurance = config.monthly_insurance
        monthly_utilities = config.monthly_utilities
        monthly_mortgage = 0.0  # all-cash assumption for MAO calc
        total_holding_cost = (
            (monthly_taxes + monthly_insurance + monthly_utilities + monthly_mortgage)
            * config.holding_months
        )

        # ── Transaction costs ────────────────────────────────────────
        buy_price = list_price  # cost basis for closing calc
        closing_cost_buy = buy_price * (config.closing_cost_buy_pct / 100)
        closing_cost_sell = arv * (config.closing_cost_sell_pct / 100)
        agent_commission = arv * (config.agent_commission_pct / 100)

        # ── Max Allowable Offer (70% Rule) ────────────────────────────
        max_allowable_offer = (arv * config.mao_arv_multiplier) - rehab_cost

        # ── Estimated profit ─────────────────────────────────────────
        #   (All-cash acquisition at list price)
        total_cost = (
            list_price
            + rehab_cost
            + total_holding_cost
            + closing_cost_buy
            + closing_cost_sell
            + agent_commission
        )
        estimated_profit = arv - total_cost

        # ── Return metrics ────────────────────────────────────────────
        cash_invested = list_price + rehab_cost + closing_cost_buy + total_holding_cost
        roi_pct = (estimated_profit / cash_invested * 100) if cash_invested > 0 else 0.0
        equity_spread = list_price - max_allowable_offer  # negative = deal

        # ── Opportunity score ─────────────────────────────────────────
        pm_score   = _profit_margin_score(estimated_profit, arv,
                                          config.score_weight_profit_margin * 100)
        roi_sc     = _roi_score(roi_pct, config.score_weight_roi * 100)
        conf_sc    = _arv_confidence_score(arv_result.confidence_score,
                                           config.score_weight_arv_confidence * 100)
        dist_sc    = _distress_score_component(prop.distress_score,
                                               config.score_weight_distress_score * 100)
        spread_sc  = _equity_spread_score(equity_spread, arv,
                                          config.score_weight_equity_spread * 100)

        opportunity_score = pm_score + roi_sc + conf_sc + dist_sc + spread_sc

        score_breakdown = ScoreBreakdown(
            profit_margin_component=round(pm_score, 2),
            roi_component=round(roi_sc, 2),
            arv_confidence_component=round(conf_sc, 2),
            distress_component=round(dist_sc, 2),
            equity_spread_component=round(spread_sc, 2),
            total=round(opportunity_score, 2),
        )

        return DealAnalysis(
            property_id=prop.id,
            scanner_run_id=scanner_run_id,
            list_price=round(list_price, 2),
            arv=round(arv, 2),
            rehab_cost=round(rehab_cost, 2),
            holding_months=config.holding_months,
            monthly_taxes=round(monthly_taxes, 2),
            monthly_insurance=monthly_insurance,
            monthly_utilities=monthly_utilities,
            monthly_mortgage=monthly_mortgage,
            total_holding_cost=round(total_holding_cost, 2),
            closing_cost_buy_pct=config.closing_cost_buy_pct,
            closing_cost_sell_pct=config.closing_cost_sell_pct,
            agent_commission_pct=config.agent_commission_pct,
            closing_cost_buy=round(closing_cost_buy, 2),
            closing_cost_sell=round(closing_cost_sell, 2),
            agent_commission=round(agent_commission, 2),
            max_allowable_offer=round(max_allowable_offer, 2),
            estimated_profit=round(estimated_profit, 2),
            roi_pct=round(roi_pct, 2),
            cash_invested=round(cash_invested, 2),
            equity_spread=round(equity_spread, 2),
            opportunity_score=round(opportunity_score, 2),
            score_breakdown=score_breakdown,
        )
