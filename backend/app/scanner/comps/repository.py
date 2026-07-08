"""
CompRepository interface — persistence-agnostic comparable sales storage.

PostgresCompRepository (postgres_source.py) is the production implementation,
using PostGIS ST_DWithin for spatial queries. InMemoryCompRepository below is
a dependency-free reference implementation used by tests and local dev
without a running Postgres instance — it implements the exact same contract.
"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from app.scanner.geo import haversine_miles


def compute_dedup_key(address: str, sale_date: date, sale_price: float) -> str:
    """
    Stable fingerprint for a comparable sale record. Prevents the same sale
    from being stored twice — e.g. re-uploading the same CSV export, or
    receiving the same sale from two overlapping providers.
    """
    normalized = f"{(address or '').strip().lower()}|{sale_date.isoformat()}|{round(sale_price, 2)}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


@dataclass
class CompRow:
    """Persistence-layer representation of one comparable sale."""

    address: str
    city: str
    state: str
    zip: str
    latitude: float
    longitude: float
    sale_date: date
    sale_price: float
    sqft: Optional[int] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[float] = None
    year_built: Optional[int] = None
    property_type: str = "sfr"
    pool: Optional[bool] = None
    garage_spaces: Optional[int] = None
    condition: Optional[str] = None
    price_per_sqft: Optional[float] = None
    source_id: Optional[str] = None
    source_name: str = "csv"
    dedup_key: str = ""

    def __post_init__(self):
        if not self.dedup_key:
            self.dedup_key = compute_dedup_key(self.address, self.sale_date, self.sale_price)


@dataclass
class IngestResult:
    """Outcome of a single CSV → repository ingestion run."""

    records_read: int = 0
    records_added: int = 0
    records_skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.errors and self.records_added == 0:
            return "failed"
        if self.errors or self.records_skipped:
            return "partial"
        return "success"


class CompRepository(ABC):
    """
    Persistence interface for comparable sales storage.

    Implementations MUST:
    - Enforce dedup_key uniqueness in upsert() — a duplicate key is a no-op,
      not an error.
    - Return query_within_radius() results sorted by distance ascending.
    - Only return comps with sale_date >= cutoff_date.
    """

    @abstractmethod
    def query_within_radius(
        self,
        latitude: float,
        longitude: float,
        radius_miles: float,
        cutoff_date: date,
        property_type: Optional[str] = None,
        limit: int = 500,
    ) -> list[tuple[CompRow, float]]:
        """
        Return (CompRow, distance_miles) pairs within radius_miles, sorted by
        distance ascending, sold on/after cutoff_date.
        """

    @abstractmethod
    def upsert(self, row: CompRow) -> bool:
        """
        Insert row if its dedup_key is new. Returns True if newly inserted,
        False if a row with the same dedup_key already existed (no-op).
        """

    @abstractmethod
    def count(self) -> int:
        """Total number of comp rows currently stored."""


class InMemoryCompRepository(CompRepository):
    """
    In-process CompRepository — no database required. Used by tests and by
    local/demo runs that want dedup + spatial-query semantics without
    standing up Postgres.
    """

    def __init__(self):
        self._rows: dict[str, CompRow] = {}

    def query_within_radius(
        self,
        latitude: float,
        longitude: float,
        radius_miles: float,
        cutoff_date: date,
        property_type: Optional[str] = None,
        limit: int = 500,
    ) -> list[tuple[CompRow, float]]:
        matches: list[tuple[float, CompRow]] = []
        for row in self._rows.values():
            if row.sale_date < cutoff_date:
                continue
            if property_type and row.property_type != property_type:
                continue
            dist = haversine_miles(latitude, longitude, row.latitude, row.longitude)
            if dist <= radius_miles:
                matches.append((dist, row))

        matches.sort(key=lambda t: t[0])
        return [(row, dist) for dist, row in matches[:limit]]

    def upsert(self, row: CompRow) -> bool:
        if row.dedup_key in self._rows:
            return False
        self._rows[row.dedup_key] = row
        return True

    def count(self) -> int:
        return len(self._rows)
