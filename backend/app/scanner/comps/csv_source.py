"""
CSV-based comparable sales source.

Accepts a CSV file exported from any tool — MLS, PropStream, ATTOM, Redfin,
or a manual spreadsheet — and filters comps for each subject property.

Expected columns (header row required; column order flexible; extras ignored):

    address, city, state, zip, latitude, longitude,
    sale_date, sale_price, sqft, bedrooms, bathrooms, year_built, property_type

Header names are case-insensitive and may contain spaces or underscores.
Column aliases accepted:  lat/latitude, lng/longitude, beds/bedrooms, baths/bathrooms.

sale_date format:  YYYY-MM-DD

The CSV is loaded and validated once at construction time; fetch_candidates()
is thereafter a pure in-memory operation (no I/O per call).

To use with the scanner pipeline, set SCANNER_COMP_CSV_PATH=/path/to/comps.csv
in your .env file, or pass comp_csv_path to ScannerConfig directly.
"""
from __future__ import annotations

import csv
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from app.scanner.comps.source import CompSource
from app.scanner.geo import haversine_miles
from app.scanner.models import CandidateProperty, ComparableSale

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Parsing helpers
# ──────────────────────────────────────────────────────────────────────────────

def _f(val: Optional[str]) -> Optional[float]:
    """Parse a cell value as float; return None on empty/invalid."""
    try:
        return float((val or "").strip()) if (val or "").strip() else None
    except (ValueError, TypeError):
        return None


def _i(val: Optional[str]) -> Optional[int]:
    """Parse a cell value as int (via float); return None on empty/invalid."""
    f = _f(val)
    return int(f) if f is not None else None


def _d(val: Optional[str]) -> Optional[date]:
    """Parse a cell value as an ISO date; return None on empty/invalid."""
    try:
        return date.fromisoformat((val or "").strip()) if (val or "").strip() else None
    except ValueError:
        return None


def _normalize_key(raw: str) -> str:
    return raw.strip().lower().replace(" ", "_")


# ──────────────────────────────────────────────────────────────────────────────
# CsvCompSource
# ──────────────────────────────────────────────────────────────────────────────

class CsvCompSource(CompSource):
    """
    Loads sold comparable sales from a CSV file and hard-filters them
    for a given subject property on each fetch_candidates() call.

    Parameters
    ----------
    csv_path:   Path to the CSV file. Header row required.
    encoding:   File encoding (default UTF-8).
    """

    def __init__(self, csv_path: str | Path, encoding: str = "utf-8"):
        self._path = Path(csv_path)
        self._rows: list[dict] = self._load(encoding)
        logger.info(
            "CsvCompSource: loaded %d valid rows from %s",
            len(self._rows), self._path,
        )

    # ── Loading ──────────────────────────────────────────────────────────────

    def _load(self, encoding: str) -> list[dict]:
        rows: list[dict] = []
        try:
            with self._path.open(encoding=encoding, newline="") as fh:
                reader = csv.DictReader(fh)
                for i, raw_row in enumerate(reader, start=2):
                    # Normalize header keys for this row
                    row = {
                        _normalize_key(k): (v or "").strip()
                        for k, v in raw_row.items()
                    }
                    parsed = self._parse_row(row)
                    if parsed is None:
                        logger.debug(
                            "CsvCompSource: skipped row %d (missing required field)", i
                        )
                    else:
                        rows.append(parsed)
        except (OSError, csv.Error) as exc:
            logger.error("CsvCompSource: failed to read %s — %s", self._path, exc)
        return rows

    def _parse_row(self, row: dict) -> Optional[dict]:
        """Validate and coerce one normalized CSV row. Returns None to skip."""
        # Latitude / longitude — required for radius filtering
        lat = _f(row.get("latitude") or row.get("lat"))
        lng = _f(row.get("longitude") or row.get("lng") or row.get("lon"))
        if lat is None or lng is None:
            return None

        # Sale date — required for recency filtering
        sale_date = _d(row.get("sale_date"))
        if sale_date is None:
            return None

        # Sale price — required for ARV calculation
        sale_price = _f(row.get("sale_price"))
        if not sale_price or sale_price <= 0:
            return None

        sqft = _i(row.get("sqft"))
        price_per_sqft = round(sale_price / sqft, 2) if sqft else None

        return {
            "address":       (row.get("address") or "").title(),
            "city":          (row.get("city") or "").title(),
            "state":         (row.get("state") or "").upper(),
            "zip":           (row.get("zip") or row.get("postal_code") or "")[:10],
            "latitude":      lat,
            "longitude":     lng,
            "sale_date":     sale_date,
            "sale_price":    sale_price,
            "sqft":          sqft,
            "bedrooms":      _i(row.get("bedrooms") or row.get("beds")),
            "bathrooms":     _f(row.get("bathrooms") or row.get("baths")),
            "year_built":    _i(row.get("year_built")),
            "property_type": (row.get("property_type") or "sfr").lower(),
            "price_per_sqft": price_per_sqft,
        }

    # ── Public interface ─────────────────────────────────────────────────────

    def fetch_candidates(
        self,
        subject: CandidateProperty,
        *,
        radius_miles: float,
        date_range_months: int,
        sqft_tolerance_pct: int,
        max_candidates: int,
    ) -> list[ComparableSale]:
        """
        Apply hard filters in priority order and return matching comps.

        Filter sequence (each applied to the survivors of the previous):
          1. Property type must match subject.
          2. Sale date within date_range_months.
          3. Haversine distance ≤ radius_miles.
          4. Sqft within ±sqft_tolerance_pct of subject sqft (skipped if
             either side is unknown).

        Returns comps sorted by distance (closest first), capped at max_candidates.
        """
        if subject.latitude is None or subject.longitude is None:
            logger.debug(
                "CsvCompSource: %s has no coordinates — skipping comp fetch",
                subject.street_address,
            )
            return []

        cutoff_date = date.today() - timedelta(days=date_range_months * 30)

        # (distance_miles, parsed_row) pairs for passing candidates
        passing: list[tuple[float, dict]] = []

        for row in self._rows:
            # 1. Property type
            if subject.property_type and row["property_type"] != subject.property_type:
                continue

            # 2. Sale date
            if row["sale_date"] < cutoff_date:
                continue

            # 3. Radius
            dist = haversine_miles(
                subject.latitude, subject.longitude,
                row["latitude"], row["longitude"],
            )
            if dist > radius_miles:
                continue

            # 4. Sqft tolerance (only when both values are present)
            if subject.sqft and row["sqft"]:
                allowed_delta = subject.sqft * sqft_tolerance_pct / 100
                if abs(subject.sqft - row["sqft"]) > allowed_delta:
                    continue

            passing.append((dist, row))

        # Sort by distance; cap before constructing objects
        passing.sort(key=lambda t: t[0])
        passing = passing[:max_candidates]

        comps: list[ComparableSale] = []
        for dist, row in passing:
            comp = ComparableSale(
                subject_property_id=subject.id,
                address=row["address"],
                city=row["city"],
                state=row["state"],
                zip=row["zip"],
                sale_date=row["sale_date"],
                sale_price=row["sale_price"],
                sqft=row["sqft"],
                bedrooms=row["bedrooms"],
                bathrooms=row["bathrooms"],
                year_built=row["year_built"],
                property_type=row["property_type"],
                latitude=row["latitude"],
                longitude=row["longitude"],
                price_per_sqft=row["price_per_sqft"],
                distance_miles=round(dist, 4),
                data_source="csv",
            )
            comps.append(comp)

        logger.debug(
            "CsvCompSource: %d / %d rows passed filters for %s (r=%.1f mi, %d mo)",
            len(comps), len(self._rows), subject.street_address,
            radius_miles, date_range_months,
        )
        return comps
