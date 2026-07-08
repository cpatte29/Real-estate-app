"""
DISCOVER phase — generates candidate distressed property leads.

Production: replace SimulatedPropertySource with real data adapters
(ATTOM, county scraper, MLS feed, etc.).
"""
from __future__ import annotations

import hashlib
import random
from abc import ABC, abstractmethod
from datetime import date, timedelta
from typing import Iterator

from app.scanner.models import CandidateProperty, DistressType


# ──────────────────────────────────────────────────────────────────────────────
# Base interface
# ──────────────────────────────────────────────────────────────────────────────

class PropertySource(ABC):
    @abstractmethod
    def fetch_candidates(self, batch_size: int, offset: int = 0) -> list[CandidateProperty]:
        """Return a batch of candidate distressed properties."""


# ──────────────────────────────────────────────────────────────────────────────
# Simulated source — realistic synthetic data for MVP / dev / testing
# ──────────────────────────────────────────────────────────────────────────────

MARKETS = [
    # (city, state, zip, lat_base, lng_base, price_mult, cost_mult)
    ("Atlanta",      "GA", "30310", 33.7490, -84.3880, 1.00, 1.00),
    ("Memphis",      "TN", "38109", 35.1495, -90.0490, 0.85, 0.90),
    ("Birmingham",   "AL", "35204", 33.5186, -86.8104, 0.80, 0.88),
    ("Cleveland",    "OH", "44105", 41.4993, -81.6944, 0.90, 0.95),
    ("Kansas City",  "MO", "64130", 39.0997, -94.5786, 0.95, 0.92),
    ("Detroit",      "MI", "48208", 42.3314, -83.0457, 0.75, 0.85),
    ("Indianapolis", "IN", "46201", 39.7684, -86.1581, 0.92, 0.93),
    ("Jacksonville", "FL", "32209", 30.3322, -81.6557, 1.05, 1.02),
]

STREET_NAMES = [
    "Oak", "Maple", "Cedar", "Pine", "Elm", "Walnut", "Birch",
    "Washington", "Lincoln", "Jefferson", "Martin Luther King",
    "Peachtree", "Highland", "Park", "Lake",
]

STREET_TYPES = ["St", "Ave", "Blvd", "Dr", "Ln", "Ct", "Way", "Pl"]

DISTRESS_SCENARIOS = [
    ([DistressType.PRE_FORECLOSURE],                    65),
    ([DistressType.TAX_LIEN],                           50),
    ([DistressType.TAX_LIEN, DistressType.VACANCY],     75),
    ([DistressType.AUCTION],                            80),
    ([DistressType.PRE_FORECLOSURE, DistressType.TAX_LIEN], 85),
    ([DistressType.PROBATE],                            55),
    ([DistressType.CODE_VIOLATION],                     45),
    ([DistressType.DIVORCE],                            40),
    ([DistressType.BANKRUPTCY],                         60),
    ([DistressType.VACANCY, DistressType.CODE_VIOLATION], 70),
]


def _address_fingerprint(street: str, city: str, state: str) -> str:
    normalized = f"{street.lower().strip()},{city.lower().strip()},{state.lower()}"
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


class SimulatedPropertySource(PropertySource):
    """
    Generates realistic synthetic distressed property candidates.
    Intentionally injects bad records (~15%) to exercise VERIFY phase.
    """

    def __init__(self, seed: int = 42):
        self._rng = random.Random(seed)

    def fetch_candidates(self, batch_size: int, offset: int = 0) -> list[CandidateProperty]:
        candidates = []
        self._rng.seed(self._rng.randint(0, 99999) + offset)  # vary per batch

        for i in range(batch_size):
            market = self._rng.choice(MARKETS)
            city, state, zip_code, lat_base, lng_base, price_mult, cost_mult = market

            sqft = self._rng.randint(800, 2800)
            beds = self._rng.choice([2, 3, 3, 3, 4, 4])
            baths = self._rng.choice([1.0, 1.0, 1.5, 2.0, 2.0])
            year_built = self._rng.randint(1940, 2005)
            assessed = self._rng.uniform(60_000, 220_000) * price_mult
            last_sale = self._rng.uniform(40_000, 180_000) * price_mult
            equity_pct = self._rng.uniform(-10, 70)  # some negative equity

            distress_types, distress_score = self._rng.choice(DISTRESS_SCENARIOS)
            distress_score = min(100, distress_score + self._rng.randint(-10, 10))

            street_num = self._rng.randint(100, 9999)
            street_name = self._rng.choice(STREET_NAMES)
            street_type = self._rng.choice(STREET_TYPES)
            street_address = f"{street_num} {street_name} {street_type}"

            lat = lat_base + self._rng.uniform(-0.05, 0.05)
            lng = lng_base + self._rng.uniform(-0.05, 0.05)

            last_sale_days_ago = self._rng.randint(180, 3650)
            last_sale_date = date.today() - timedelta(days=last_sale_days_ago)

            # Inject bad records to test VERIFY phase
            inject_bad = self._rng.random() < 0.15
            bad_type = self._rng.choice(["missing_address", "missing_sqft"]) if inject_bad else None

            candidates.append(CandidateProperty(
                street_address="" if bad_type == "missing_address" else street_address,
                city=city,
                state=state,
                zip=zip_code,
                county=f"{city} County",
                apn=f"{zip_code}-{self._rng.randint(10000, 99999)}",
                latitude=lat,
                longitude=lng,
                property_type="sfr",
                bedrooms=beds,
                bathrooms=baths,
                sqft=None if bad_type == "missing_sqft" else sqft,
                lot_sqft=self._rng.randint(3000, 12000),
                year_built=year_built,
                garage_spaces=self._rng.choice([0, 0, 1, 2]),
                pool=self._rng.random() < 0.08,
                assessed_value=round(assessed, 2),
                estimated_value=round(assessed * self._rng.uniform(0.9, 1.15), 2),
                tax_annual=round(assessed * 0.012, 2),
                owner_name=f"Owner {self._rng.randint(1000, 9999)}",
                owner_occupied=self._rng.random() < 0.4,
                ownership_length_yrs=round(self._rng.uniform(0.5, 25), 1),
                last_sale_date=last_sale_date,
                last_sale_price=round(last_sale, 2),
                estimated_equity_pct=round(equity_pct, 1),
                distress_types=distress_types,
                distress_score=distress_score,
                data_source="simulated",
                source_record_id=f"sim-{offset + i:06d}",
                address_fingerprint=_address_fingerprint(street_address, city, state),
            ))

        return candidates


# ──────────────────────────────────────────────────────────────────────────────
# Discovery orchestrator
# ──────────────────────────────────────────────────────────────────────────────

class PropertyDiscovery:
    def __init__(self, source: PropertySource):
        self._source = source

    def discover(self, batch_size: int, offset: int = 0) -> list[CandidateProperty]:
        """Fetch a batch of candidate properties from the configured source."""
        candidates = self._source.fetch_candidates(batch_size, offset)
        return candidates

    def stream(self, total: int, batch_size: int = 50) -> Iterator[list[CandidateProperty]]:
        """Yield batches of candidates up to `total`."""
        fetched = 0
        while fetched < total:
            size = min(batch_size, total - fetched)
            batch = self.discover(size, offset=fetched)
            if not batch:
                break
            yield batch
            fetched += len(batch)
