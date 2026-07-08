"""
CompSource interface and simulated implementation.

To add a new provider (ATTOM, MLS export, PropStream, Zillow):
    1. Subclass CompSource
    2. Implement fetch_candidates()
    3. Pass an instance to ARVEngine(comp_source=...)

Similarity scoring, price adjustments, and ARV calculation all live in
ARVEngine — CompSource implementations are pure data retrieval.
"""
from __future__ import annotations

import math
import random
from abc import ABC, abstractmethod
from datetime import date, timedelta
from typing import Optional

from app.scanner.geo import haversine_miles
from app.scanner.models import CandidateProperty, ComparableSale


# ──────────────────────────────────────────────────────────────────────────────
# Regional price-per-sqft benchmarks used by SimulatedCompSource
# ──────────────────────────────────────────────────────────────────────────────

_MARKET_PPF: dict[str, dict[str, float]] = {
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
    state_map = _MARKET_PPF.get(state, {})
    for mkt_city, ppf in state_map.items():
        if mkt_city.lower() in city.lower():
            return ppf
    return 140.0  # national fallback


# ──────────────────────────────────────────────────────────────────────────────
# Abstract interface
# ──────────────────────────────────────────────────────────────────────────────

class CompSource(ABC):
    """
    Data provider for comparable sales.

    A CompSource returns raw candidate comps pre-filtered by the hard criteria
    (radius, property type, date range, sqft tolerance). Similarity scoring,
    price adjustments, and ARV calculation are handled by ARVEngine — not here.

    Implementations MUST:
    - Set ``distance_miles`` on each returned ComparableSale.
    - Set ``subject_property_id = subject.id`` on each returned comp.
    - NOT set ``similarity_score``, ``price_adjustment``, or ``adjusted_price``
      (those default to 0.0 and are populated by ARVEngine after fetch).

    Implementations SHOULD:
    - Return at most ``max_candidates`` comps.
    - Apply hard filters: radius_miles, date_range_months, sqft_tolerance_pct (±%),
      and subject.property_type before returning.
    """

    @abstractmethod
    def fetch_candidates(
        self,
        subject: CandidateProperty,
        *,
        radius_miles: float,
        date_range_months: int,
        sqft_tolerance_pct: int,
        max_candidates: int,
    ) -> list[ComparableSale]:
        """Return raw ComparableSale candidates satisfying the hard filters."""


# ──────────────────────────────────────────────────────────────────────────────
# Simulated implementation
# ──────────────────────────────────────────────────────────────────────────────

class SimulatedCompSource(CompSource):
    """
    Generates synthetic comparable sales from regional price-per-sqft benchmarks.
    Used for development, demos, and tests that do not need real comp data.
    """

    def __init__(
        self,
        rng: Optional[random.Random] = None,
        seed: int = 42,
    ):
        self._rng = rng or random.Random(seed)

    def fetch_candidates(
        self,
        subject: CandidateProperty,
        *,
        radius_miles: float,
        date_range_months: int,
        sqft_tolerance_pct: int,
        max_candidates: int,
    ) -> list[ComparableSale]:
        if not subject.sqft or not subject.latitude or not subject.longitude:
            return []

        market_ppf = _get_market_ppf(subject.state, subject.city)
        rng = self._rng

        n_comps = rng.choices(
            [0, 1, 2, 3, 4, 5, 6, 7, 8],
            weights=[5, 5, 8, 15, 20, 20, 15, 8, 4],
            k=1,
        )[0]
        n_comps = min(n_comps, max_candidates)

        comps: list[ComparableSale] = []
        for _ in range(n_comps):
            dist = rng.uniform(0.05, radius_miles)
            angle = rng.uniform(0, 2 * math.pi)
            lat = subject.latitude + (dist / 69.0) * math.cos(angle)
            lng = subject.longitude + (
                dist / (69.0 * math.cos(math.radians(subject.latitude)))
            ) * math.sin(angle)

            sqft_var = rng.uniform(
                1 - sqft_tolerance_pct / 100,
                1 + sqft_tolerance_pct / 100,
            )
            comp_sqft = max(500, int(subject.sqft * sqft_var))
            days_ago = rng.randint(7, date_range_months * 30)
            sale_date = date.today() - timedelta(days=days_ago)
            ppf_var = market_ppf * rng.uniform(0.85, 1.18)
            sale_price = round(comp_sqft * ppf_var, -2)

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
                distance_miles=round(dist, 4),
                data_source="simulated",
            )
            comps.append(comp)

        return comps
