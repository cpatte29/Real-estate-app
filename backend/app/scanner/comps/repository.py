"""
CompRepository interface — persistence-agnostic comparable sales storage.

PostgresCompRepository (postgres_source.py) is the production implementation,
using PostGIS ST_DWithin for spatial queries. InMemoryCompRepository below is
a dependency-free reference implementation used by tests and local dev
without a running Postgres instance — it implements the exact same contract.
"""
from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import ClassVar, Optional

from app.scanner.geo import haversine_miles

# Common street-suffix spellings collapsed to one form so "123 Oak Street"
# and "123 Oak St" produce the same dedup key.
_SUFFIX_MAP = {
    "street": "st", "avenue": "ave", "drive": "dr", "road": "rd",
    "lane": "ln", "court": "ct", "boulevard": "blvd", "place": "pl",
    "circle": "cir", "terrace": "ter", "highway": "hwy", "parkway": "pkwy",
}


def _normalize_address(address: str) -> str:
    s = (address or "").strip().lower()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    words = [_SUFFIX_MAP.get(w, w) for w in s.split(" ")]
    return " ".join(words)


def compute_dedup_key(
    address: str,
    sale_date: date,
    zip_code: Optional[str] = None,
    city: Optional[str] = None,
    state: Optional[str] = None,
) -> str:
    """
    Stable fingerprint for a comparable sale record — identity is address +
    sale date + location, NOT sale price. Providers commonly report the same
    closed sale with slightly different prices (rounding, later corrections,
    commission-inclusive vs. exclusive figures); keying on price would let
    the same sale slip in twice under two different price reports.

    A geographic component is required: comp_pool is multi-market, and a
    common street address ("100 Main St") recurs across many cities. Without
    zip (or city+state when zip is unavailable), two unrelated sales in
    different markets on the same date would collide and one would be
    silently dropped as a "duplicate".
    """
    geo = (zip_code or "").strip()
    if not geo:
        geo = f"{(city or '').strip().lower()},{(state or '').strip().lower()}"
    normalized = f"{_normalize_address(address)}|{sale_date.isoformat()}|{geo.lower()}"
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
            self.dedup_key = compute_dedup_key(
                self.address, self.sale_date,
                zip_code=self.zip, city=self.city, state=self.state,
            )


@dataclass
class IngestResult:
    """
    Outcome of a single CSV → repository ingestion run.

    ``errors`` stores at most MAX_STORED_ERRORS messages — a malformed
    upload with hundreds of thousands of bad rows must not balloon this
    object (and the JSON response / audit row built from it) without bound.
    ``error_count`` tracks the true total regardless of how many were kept.
    Use add_error() rather than appending to ``errors`` directly so the cap
    is enforced consistently.
    """

    MAX_STORED_ERRORS: ClassVar[int] = 100

    records_read: int = 0
    records_added: int = 0
    records_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    error_count: int = 0

    def add_error(self, message: str) -> None:
        self.error_count += 1
        if len(self.errors) < self.MAX_STORED_ERRORS:
            self.errors.append(message)

    @property
    def display_errors(self) -> list[str]:
        """Stored errors, plus a truncation summary if any were dropped."""
        if self.error_count > len(self.errors):
            omitted = self.error_count - len(self.errors)
            return self.errors + [f"...and {omitted} more error(s)"]
        return list(self.errors)

    @property
    def status(self) -> str:
        if self.error_count and self.records_added == 0:
            return "failed"
        if self.error_count or self.records_skipped:
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

    def record_ingestion_run(
        self,
        source_name: str,
        file_name: str,
        result: IngestResult,
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
    ) -> None:
        """
        Optional audit hook, called once per CsvCompIngester.ingest_file()
        run. No-op by default — override to persist a run record (see
        PostgresCompRepository, which writes to comp_ingestion_runs).
        """
        return None


class InMemoryCompRepository(CompRepository):
    """
    In-process CompRepository — no database required. Used by tests and by
    local/demo runs that want dedup + spatial-query semantics without
    standing up Postgres.
    """

    def __init__(self):
        self._rows: dict[str, CompRow] = {}
        self.ingestion_runs: list[dict] = []

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

    def record_ingestion_run(
        self,
        source_name: str,
        file_name: str,
        result: IngestResult,
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
    ) -> None:
        """Captures the call for test assertions instead of discarding it."""
        self.ingestion_runs.append({
            "source_name": source_name,
            "file_name": file_name,
            "result": result,
            "started_at": started_at,
            "completed_at": completed_at,
        })
