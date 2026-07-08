"""
ARV (After-Repair Value) calculation engine.

Selects comparable sales, scores them by similarity, applies adjustments,
and computes a weighted-average ARV with a confidence level.
"""
from __future__ import annotations

import math
import random
import statistics
from datetime import date, timedelta
from typing import Optional
from uuid import UUID

from app.scanner.config import config
from app.scanner.models import (
    ARVConfidence,
    ARVResult,
    CandidateProperty,
    ComparableSale,
)


# ──────────────────────────────────────────────────────────────────────────────
# Regional price-per-sqft benchmarks ($/sqft, SFR, as-repaired)
# ──────────────────────────────────────────────────────────────────────────────

MARKET_PPF = {
    "GA": {"Atlanta": 175},
    "TN": {"Memphis": 130},
    "AL": {"Birmingham": 120},
    "OH": {"Cleveland": 110},
    "MO": {"Kansas City": 145},
    "MI": {"Detroit": 95},
    "IN": {"Indianapolis": 140},
    "FL": {"Jacksonville": 160},
}


def _get_market_ppf(state: str, city: str) -> float:
    state_map = MARKET_PPF.get(state, {})
    for mkt_city, ppf in state_map.items():
        if mkt_city.lower() in city.lower():
            return ppf
    return 140.0   # national fallback


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ──────────────────────────────────────────────────────────────────────────────
# Comp similarity scoring (0-100)
# ──────────────────────────────────────────────────────────────────────────────

def _similarity_score(subject: CandidateProperty, comp: ComparableSale) -> float:
    score = 100.0

    # Distance penalty (up to -30 points)
    if comp.distance_miles is not None:
        score -= min(30, comp.distance_miles * 60)

    # Sqft difference penalty (up to -25 points)
    if subject.sqft and comp.sqft:
        diff_pct = abs(subject.sqft - comp.sqft) / subject.sqft
        score -= min(25, diff_pct * 100)

    # Bedroom difference (-5 per bed)
    if subject.bedrooms is not None and comp.bedrooms is not None:
        score -= abs(subject.bedrooms - comp.bedrooms) * 5

    # Age difference (-1 per 10 years)
    if subject.year_built and comp.year_built:
        score -= abs(subject.year_built - comp.year_built) / 10

    # Recency bonus: sold within 3 months = 0 penalty, 6 months = -10
    months_ago = (date.today() - comp.sale_date).days / 30
    score -= max(0, (months_ago - 3) * (10 / 3))

    return max(0.0, min(100.0, score))


# ──────────────────────────────────────────────────────────────────────────────
# Adjustment grid (per-unit cost adjustments applied to comp price)
# ──────────────────────────────────────────────────────────────────────────────

def _apply_adjustments(subject: CandidateProperty, comp: ComparableSale) -> float:
    adjustment = 0.0

    # Bedroom adjustment (~$5k per bedroom delta)
    if subject.bedrooms is not None and comp.bedrooms is not None:
        adjustment += (subject.bedrooms - comp.bedrooms) * 5_000

    # Bathroom adjustment (~$3k per half-bath)
    if subject.bathrooms is not None and comp.bathrooms is not None:
        adjustment += (subject.bathrooms - comp.bathrooms) * 3_000

    # Sqft adjustment: price/sqft of comp × sqft delta
    if subject.sqft and comp.sqft and comp.price_per_sqft:
        adjustment += (subject.sqft - comp.sqft) * (comp.price_per_sqft * 0.5)

    # Garage adjustment (~$5k per space)
    if subject.garage_spaces is not None and comp.sqft:
        adjustment += subject.garage_spaces * 3_000

    # Pool adjustment
    if subject.pool:
        adjustment += 8_000

    return round(adjustment, 2)


# ──────────────────────────────────────────────────────────────────────────────
# Simulated comparable sales generator
# ──────────────────────────────────────────────────────────────────────────────

def _generate_simulated_comps(
    subject: CandidateProperty,
    scanner_run_id: UUID,
    rng: random.Random,
    max_comps: int,
    radius_miles: float,
    date_range_months: int,
    sqft_tolerance_pct: int,
) -> list[ComparableSale]:
    if not subject.sqft or not subject.latitude or not subject.longitude:
        return []

    market_ppf = _get_market_ppf(subject.state, subject.city)
    comps = []

    # Number of comps: sometimes low to exercise VERIFY rejection
    n_comps = rng.choices(
        [0, 1, 2, 3, 4, 5, 6, 7, 8],
        weights=[5, 5, 8, 15, 20, 20, 15, 8, 4],
        k=1,
    )[0]
    n_comps = min(n_comps, max_comps)

    for j in range(n_comps):
        dist = rng.uniform(0.05, radius_miles)
        angle = rng.uniform(0, 2 * math.pi)
        lat = subject.latitude + (dist / 69.0) * math.cos(angle)
        lng = subject.longitude + (dist / (69.0 * math.cos(math.radians(subject.latitude)))) * math.sin(angle)

        sqft_var = rng.uniform(1 - sqft_tolerance_pct / 100, 1 + sqft_tolerance_pct / 100)
        comp_sqft = max(500, int(subject.sqft * sqft_var))

        days_ago = rng.randint(7, date_range_months * 30)
        sale_date = date.today() - timedelta(days=days_ago)

        ppf_var = market_ppf * rng.uniform(0.85, 1.18)
        sale_price = round(comp_sqft * ppf_var, -2)  # round to $100

        street_num = rng.randint(100, 9999)
        street = rng.choice(["Oak", "Maple", "Pine", "Elm", "Cedar"])
        street_type = rng.choice(["St", "Ave", "Dr"])

        comp = ComparableSale(
            subject_property_id=subject.id,
            address=f"{street_num} {street} {street_type}",
            city=subject.city,
            state=subject.state,
            zip=subject.zip,
            sale_date=sale_date,
            sale_price=sale_price,
            sqft=comp_sqft,
            bedrooms=rng.choice([
                subject.bedrooms or 3,
                max(1, (subject.bedrooms or 3) - 1),
                (subject.bedrooms or 3) + 1,
            ]),
            bathrooms=rng.choice([1.0, 1.5, 2.0, 2.5]),
            year_built=subject.year_built or 1975,
            property_type="sfr",
            latitude=lat,
            longitude=lng,
            price_per_sqft=round(sale_price / comp_sqft, 2),
            distance_miles=round(dist, 3),
            data_source="simulated",
        )
        comp.similarity_score = _similarity_score(subject, comp)
        comp.price_adjustment = _apply_adjustments(subject, comp)
        comp.adjusted_price = sale_price + comp.price_adjustment

        comps.append(comp)

    return sorted(comps, key=lambda c: c.similarity_score, reverse=True)


# ──────────────────────────────────────────────────────────────────────────────
# Confidence scoring
# ──────────────────────────────────────────────────────────────────────────────

def _confidence(comp_count: int, stddev_pct: float) -> tuple[ARVConfidence, float]:
    """
    Returns (confidence_enum, numeric_score 0-100).
    High comp count and low price variance = high confidence.
    """
    if comp_count == 0:
        return ARVConfidence.INSUFFICIENT, 0.0

    # Base from count
    if comp_count >= 6:
        base = 85.0
    elif comp_count >= 4:
        base = 72.0
    elif comp_count >= 3:
        base = 60.0
    else:
        base = 35.0

    # Adjust for variance (stddev as % of mean)
    variance_penalty = min(25, stddev_pct * 0.8)
    score = base - variance_penalty

    if score >= 75:
        conf = ARVConfidence.HIGH
    elif score >= 60:
        conf = ARVConfidence.MEDIUM
    else:
        conf = ARVConfidence.LOW

    return conf, round(score, 1)


# ──────────────────────────────────────────────────────────────────────────────
# ARV Engine — public interface
# ──────────────────────────────────────────────────────────────────────────────

class ARVEngine:
    def __init__(self, rng: Optional[random.Random] = None):
        self._rng = rng or random.Random(config.simulation_seed)

    def calculate(
        self,
        subject: CandidateProperty,
        scanner_run_id: UUID,
        radius_miles: float = None,
        date_range_months: int = None,
        sqft_tolerance_pct: int = None,
        max_comps: int = None,
    ) -> ARVResult:
        radius_miles = radius_miles or config.arv_radius_miles
        date_range_months = date_range_months or config.arv_date_range_months
        sqft_tolerance_pct = sqft_tolerance_pct or config.arv_sqft_tolerance_pct
        max_comps = max_comps or config.arv_max_comps

        comps = _generate_simulated_comps(
            subject, scanner_run_id, self._rng,
            max_comps, radius_miles, date_range_months, sqft_tolerance_pct,
        )

        if not comps:
            return ARVResult(
                property_id=subject.id,
                scanner_run_id=scanner_run_id,
                arv=0.0,
                confidence=ARVConfidence.INSUFFICIENT,
                confidence_score=0.0,
                comp_count=0,
                price_per_sqft_avg=0.0,
                price_per_sqft_stddev=0.0,
                comps=[],
            )

        # Weighted average using similarity scores
        total_weight = sum(c.similarity_score for c in comps)
        if total_weight == 0:
            arv = statistics.mean(c.adjusted_price for c in comps)
        else:
            arv = sum(c.adjusted_price * c.similarity_score for c in comps) / total_weight

        prices = [c.adjusted_price for c in comps]
        mean_price = statistics.mean(prices)
        stddev = statistics.stdev(prices) if len(prices) > 1 else 0.0
        stddev_pct = (stddev / mean_price * 100) if mean_price else 0.0

        ppf_values = [c.price_per_sqft for c in comps if c.price_per_sqft]
        ppf_avg = statistics.mean(ppf_values) if ppf_values else 0.0
        ppf_std = statistics.stdev(ppf_values) if len(ppf_values) > 1 else 0.0

        confidence, confidence_score = _confidence(len(comps), stddev_pct)

        return ARVResult(
            property_id=subject.id,
            scanner_run_id=scanner_run_id,
            arv=round(arv, 2),
            confidence=confidence,
            confidence_score=confidence_score,
            comp_count=len(comps),
            price_per_sqft_avg=round(ppf_avg, 2),
            price_per_sqft_stddev=round(ppf_std, 2),
            comps=comps,
            radius_miles=radius_miles,
            date_range_months=date_range_months,
            sqft_tolerance_pct=sqft_tolerance_pct,
        )
