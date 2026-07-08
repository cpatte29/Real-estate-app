"""
PostgreSQL + PostGIS-backed CompRepository and CompSource.

Backed by comp_pool (migration 003) — a subject-independent table of raw
ingested comps, distinct from comparable_sales (which stores per-subject,
already-scored ARV snapshots and is untouched by this module).

This module does not import a DB driver directly — PostgresCompRepository
accepts any DB-API 2.0 connection whose cursor yields dict-like rows (e.g.
psycopg2 with psycopg2.extras.RealDictCursor, or a test double). That keeps
this module driver-agnostic and importable without psycopg2 installed.

Usage:
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    repo = PostgresCompRepository(conn)
    source = PostgresCompSource(repo)
    engine = ARVEngine(comp_source=source)
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from app.scanner.comps.repository import CompRepository, CompRow, IngestResult
from app.scanner.comps.source import CompSource
from app.scanner.models import CandidateProperty, ComparableSale

logger = logging.getLogger(__name__)

_METERS_PER_MILE = 1609.34


def _to_float(val) -> Optional[float]:
    """
    Cast a DB-driver numeric value (often Decimal for NUMERIC/DECIMAL
    columns) to float at the persistence boundary, so downstream ARV
    arithmetic never mixes Decimal and float and raises TypeError.
    """
    return float(val) if val is not None else None


# ST_DWithin / ST_Distance operate on `geography` so both take meters and
# account for earth curvature — no manual haversine math needed here.
_UPSERT_SQL = """
INSERT INTO comp_pool (
    address, city, state, zip, location,
    sale_date, sale_price, sqft, bedrooms, bathrooms, year_built,
    property_type, pool, garage_spaces, condition, price_per_sqft,
    source_id, source_name, dedup_key
) VALUES (
    %(address)s, %(city)s, %(state)s, %(zip)s,
    ST_SetSRID(ST_MakePoint(%(longitude)s, %(latitude)s), 4326)::geography,
    %(sale_date)s, %(sale_price)s, %(sqft)s, %(bedrooms)s, %(bathrooms)s, %(year_built)s,
    %(property_type)s, %(pool)s, %(garage_spaces)s, %(condition)s, %(price_per_sqft)s,
    %(source_id)s, %(source_name)s, %(dedup_key)s
)
ON CONFLICT (dedup_key) DO NOTHING
"""

_RADIUS_QUERY_SQL = """
SELECT
    address, city, state, zip,
    ST_Y(location::geometry) AS latitude,
    ST_X(location::geometry) AS longitude,
    sale_date, sale_price, sqft, bedrooms, bathrooms, year_built,
    property_type, pool, garage_spaces, condition, price_per_sqft,
    source_id, source_name, dedup_key,
    ST_Distance(
        location,
        ST_SetSRID(ST_MakePoint(%(longitude)s, %(latitude)s), 4326)::geography
    ) AS distance_meters
FROM comp_pool
WHERE ST_DWithin(
        location,
        ST_SetSRID(ST_MakePoint(%(longitude)s, %(latitude)s), 4326)::geography,
        %(radius_meters)s
      )
  AND sale_date >= %(cutoff_date)s
  {property_type_clause}
ORDER BY distance_meters ASC
LIMIT %(limit)s
"""

_COUNT_SQL = "SELECT COUNT(*) AS n FROM comp_pool"

_INSERT_INGESTION_RUN_SQL = """
INSERT INTO comp_ingestion_runs (
    source_name, file_name, records_read, records_added, records_skipped,
    error_count, errors, started_at, completed_at, status
) VALUES (
    %(source_name)s, %(file_name)s, %(records_read)s, %(records_added)s, %(records_skipped)s,
    %(error_count)s, %(errors)s::jsonb, %(started_at)s, %(completed_at)s, %(status)s
)
"""


class PostgresCompRepository(CompRepository):
    """
    PostGIS-backed comp storage (comp_pool table).

    Accepts an injected DB-API 2.0 connection (dependency injection keeps
    this class testable without a live database and decoupled from a
    specific driver). Each write commits immediately; any failed statement
    triggers a rollback so the connection isn't left in an aborted-
    transaction state for the next call.
    """

    def __init__(self, connection):
        self._conn = connection

    def query_within_radius(
        self,
        latitude: float,
        longitude: float,
        radius_miles: float,
        cutoff_date: date,
        property_type: Optional[str] = None,
        limit: int = 500,
    ) -> list[tuple[CompRow, float]]:
        clause = "AND property_type = %(property_type)s" if property_type else ""
        sql = _RADIUS_QUERY_SQL.format(property_type_clause=clause)
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "radius_meters": radius_miles * _METERS_PER_MILE,
            "cutoff_date": cutoff_date,
            "limit": limit,
        }
        if property_type:
            params["property_type"] = property_type

        cur = self._conn.cursor()
        try:
            cur.execute(sql, params)
            db_rows = cur.fetchall()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

        results: list[tuple[CompRow, float]] = []
        for r in db_rows:
            row = CompRow(
                address=r["address"], city=r["city"], state=r["state"], zip=r["zip"],
                latitude=_to_float(r["latitude"]), longitude=_to_float(r["longitude"]),
                sale_date=r["sale_date"], sale_price=_to_float(r["sale_price"]),
                sqft=r["sqft"], bedrooms=r["bedrooms"],
                bathrooms=_to_float(r["bathrooms"]),
                year_built=r["year_built"], property_type=r["property_type"],
                pool=r["pool"], garage_spaces=r["garage_spaces"], condition=r["condition"],
                price_per_sqft=_to_float(r["price_per_sqft"]),
                source_id=r["source_id"], source_name=r["source_name"],
                dedup_key=r["dedup_key"],
            )
            dist_miles = _to_float(r["distance_meters"]) / _METERS_PER_MILE
            results.append((row, dist_miles))
        return results

    def upsert(self, row: CompRow) -> bool:
        cur = self._conn.cursor()
        try:
            cur.execute(_UPSERT_SQL, {
                "address": row.address, "city": row.city, "state": row.state, "zip": row.zip,
                "latitude": row.latitude, "longitude": row.longitude,
                "sale_date": row.sale_date, "sale_price": row.sale_price,
                "sqft": row.sqft, "bedrooms": row.bedrooms, "bathrooms": row.bathrooms,
                "year_built": row.year_built, "property_type": row.property_type,
                "pool": row.pool, "garage_spaces": row.garage_spaces, "condition": row.condition,
                "price_per_sqft": row.price_per_sqft,
                "source_id": row.source_id, "source_name": row.source_name,
                "dedup_key": row.dedup_key,
            })
            inserted = cur.rowcount == 1
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()
        return inserted

    def count(self) -> int:
        cur = self._conn.cursor()
        try:
            cur.execute(_COUNT_SQL)
            row = cur.fetchone()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()
        return row["n"] if isinstance(row, dict) else row[0]

    def record_ingestion_run(
        self,
        source_name: str,
        file_name: str,
        result: IngestResult,
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
    ) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(_INSERT_INGESTION_RUN_SQL, {
                "source_name": source_name,
                "file_name": file_name,
                "records_read": result.records_read,
                "records_added": result.records_added,
                "records_skipped": result.records_skipped,
                "error_count": result.error_count,
                "errors": json.dumps(result.display_errors),
                "started_at": started_at,
                "completed_at": completed_at,
                "status": result.status,
            })
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()


class PostgresCompSource(CompSource):
    """
    CompSource backed by a CompRepository. Spatial radius, date-range, and
    property-type filtering happen in the repository query; the sqft
    tolerance filter is applied here since it is not expressible as a single
    spatial/date predicate shared across repository implementations.
    """

    def __init__(self, repository: CompRepository):
        self._repo = repository

    def fetch_candidates(
        self,
        subject: CandidateProperty,
        *,
        radius_miles: float,
        date_range_months: int,
        sqft_tolerance_pct: int,
        max_candidates: int,
    ) -> list[ComparableSale]:
        if subject.latitude is None or subject.longitude is None:
            logger.debug(
                "PostgresCompSource: %s has no coordinates — skipping comp fetch",
                subject.street_address,
            )
            return []

        cutoff_date = date.today() - timedelta(days=date_range_months * 30)
        rows = self._repo.query_within_radius(
            latitude=subject.latitude,
            longitude=subject.longitude,
            radius_miles=radius_miles,
            cutoff_date=cutoff_date,
            property_type=subject.property_type or None,
            limit=max_candidates * 4,  # headroom before the sqft filter below
        )

        comps: list[ComparableSale] = []
        for row, dist in rows:
            if subject.sqft and row.sqft:
                allowed_delta = subject.sqft * sqft_tolerance_pct / 100
                if abs(subject.sqft - row.sqft) > allowed_delta:
                    continue

            comps.append(ComparableSale(
                subject_property_id=subject.id,
                address=row.address,
                city=row.city,
                state=row.state,
                zip=row.zip,
                sale_date=row.sale_date,
                sale_price=row.sale_price,
                sqft=row.sqft,
                bedrooms=row.bedrooms,
                bathrooms=row.bathrooms,
                year_built=row.year_built,
                property_type=row.property_type,
                latitude=row.latitude,
                longitude=row.longitude,
                price_per_sqft=row.price_per_sqft,
                distance_miles=round(dist, 4),
                data_source=row.source_name,
            ))
            if len(comps) >= max_candidates:
                break

        logger.debug(
            "PostgresCompSource: %d comps returned for %s (r=%.1f mi, %d mo)",
            len(comps), subject.street_address, radius_miles, date_range_months,
        )
        return comps
