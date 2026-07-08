"""
CSV → CompRepository ingestion.

Reuses the same column-parsing rules as CsvCompSource (case-insensitive
headers, lat/lng and beds/baths aliases) so any file that loads with
CsvCompSource can also be ingested into a CompRepository (Postgres or
in-memory) via CompIngester.
"""
from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.scanner.comps.csv_source import _d, _f, _i, _normalize_key
from app.scanner.comps.repository import CompRepository, CompRow, IngestResult

logger = logging.getLogger(__name__)

# Maps common free-text property type spellings (as seen in MLS/PropStream/
# ATTOM exports) to the property_type Postgres enum values
# (sfr, multi_family, condo, townhouse, land). Unrecognized or blank values
# fall back to "sfr" rather than raising an enum-violation error at insert.
_PROPERTY_TYPE_MAP = {
    "single family": "sfr",
    "single-family": "sfr",
    "single family residence": "sfr",
    "sfr/residential": "sfr",
    "sfr": "sfr",
    "residential": "sfr",

    "multi family": "multi_family",
    "multi-family": "multi_family",
    "multifamily": "multi_family",
    "multi_family": "multi_family",
    "duplex": "multi_family",
    "triplex": "multi_family",
    "fourplex": "multi_family",

    "townhome": "townhouse",
    "town house": "townhouse",
    "town-house": "townhouse",
    "townhouse": "townhouse",

    "condo": "condo",
    "condominium": "condo",

    "land": "land",
    "lot": "land",
    "vacant land": "land",
}


def _normalize_property_type(raw: Optional[str]) -> str:
    key = (raw or "").strip().lower()
    return _PROPERTY_TYPE_MAP.get(key, "sfr")


class CsvCompIngester:
    """Loads a CSV of comparable sales into a CompRepository, skipping duplicates."""

    def __init__(self, repository: CompRepository, source_name: str = "csv"):
        self._repo = repository
        self._source_name = source_name

    def ingest_file(self, csv_path: str | Path, encoding: str = "utf-8") -> IngestResult:
        path = Path(csv_path)
        result = IngestResult()
        started_at = datetime.utcnow()

        try:
            try:
                with path.open(encoding=encoding, newline="") as fh:
                    reader = csv.DictReader(fh)
                    for i, raw_row in enumerate(reader, start=2):
                        result.records_read += 1
                        row = {_normalize_key(k): (v or "").strip() for k, v in raw_row.items()}
                        comp_row = self._parse_row(row)
                        if comp_row is None:
                            result.records_skipped += 1
                            result.errors.append(f"row {i}: missing required field")
                            continue

                        # A single bad row (e.g. a repository/DB error) must
                        # not abort the rest of the file — count it as an
                        # error and keep going.
                        try:
                            added = self._repo.upsert(comp_row)
                        except Exception as exc:
                            result.records_skipped += 1
                            result.errors.append(f"row {i}: upsert failed — {exc}")
                            logger.warning(
                                "CsvCompIngester: row %d upsert failed for %s — %s",
                                i, path, exc,
                            )
                            continue

                        if added:
                            result.records_added += 1
                        else:
                            result.records_skipped += 1
            except (OSError, csv.Error) as exc:
                result.errors.append(str(exc))
                logger.error("CsvCompIngester: failed to read %s — %s", path, exc)
        finally:
            # Always record the run — even a file-read failure or a
            # partially-completed ingest should leave an audit trail.
            completed_at = datetime.utcnow()
            self._repo.record_ingestion_run(
                source_name=self._source_name,
                file_name=str(path),
                result=result,
                started_at=started_at,
                completed_at=completed_at,
            )

        logger.info(
            "CsvCompIngester: %s | read=%d added=%d skipped=%d errors=%d status=%s",
            path, result.records_read, result.records_added,
            result.records_skipped, len(result.errors), result.status,
        )
        return result

    def _parse_row(self, row: dict) -> Optional[CompRow]:
        lat = _f(row.get("latitude") or row.get("lat"))
        lng = _f(row.get("longitude") or row.get("lng") or row.get("lon"))
        if lat is None or lng is None:
            return None

        sale_date = _d(row.get("sale_date"))
        if sale_date is None:
            return None

        sale_price = _f(row.get("sale_price"))
        if not sale_price or sale_price <= 0:
            return None

        sqft = _i(row.get("sqft"))
        price_per_sqft = round(sale_price / sqft, 2) if sqft else None

        pool_raw = (row.get("pool") or "").lower()
        pool = pool_raw in ("1", "true", "yes", "y") if pool_raw else None

        return CompRow(
            address=(row.get("address") or "").title(),
            city=(row.get("city") or "").title(),
            state=(row.get("state") or "").upper(),
            zip=(row.get("zip") or row.get("postal_code") or "")[:10],
            latitude=lat,
            longitude=lng,
            sale_date=sale_date,
            sale_price=sale_price,
            sqft=sqft,
            bedrooms=_i(row.get("bedrooms") or row.get("beds")),
            bathrooms=_f(row.get("bathrooms") or row.get("baths")),
            year_built=_i(row.get("year_built")),
            property_type=_normalize_property_type(row.get("property_type")),
            pool=pool,
            garage_spaces=_i(row.get("garage_spaces") or row.get("garage")),
            condition=(row.get("condition") or "").lower() or None,
            price_per_sqft=price_per_sqft,
            source_id=row.get("source_id") or row.get("mls_number") or None,
            source_name=self._source_name,
        )
