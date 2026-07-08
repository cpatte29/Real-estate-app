"""
Gated integration test — runs against a real PostGIS-enabled Postgres
instance when DATABASE_URL is set and psycopg2 is installed. Skipped
otherwise (e.g. in CI without a database, or in this sandbox).

This is the test class the unit-test suite cannot provide: it is the only
place that would have caught a comp_pool/comparable_sales schema mismatch,
an ON CONFLICT target that doesn't match a real constraint, or a
transaction left aborted after a bad statement.

To run locally:
    createdb rei_scanner_test
    psql rei_scanner_test -c 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"; CREATE EXTENSION IF NOT EXISTS postgis;'
    psql rei_scanner_test -f app/db/schema.sql
    psql rei_scanner_test -f app/db/migrations/001_lead_review.sql
    psql rei_scanner_test -f app/db/migrations/002_comp_ingestion.sql
    psql rei_scanner_test -f app/db/migrations/003_comp_pool.sql
    DATABASE_URL=postgresql://localhost/rei_scanner_test pytest tests/test_postgres_comps_integration.py -v
"""
from __future__ import annotations

import os
from datetime import date, timedelta

import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")

try:
    import psycopg2
    import psycopg2.extras
    _PSYCOPG2_AVAILABLE = True
except ImportError:
    _PSYCOPG2_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not DATABASE_URL or not _PSYCOPG2_AVAILABLE,
    reason="DATABASE_URL not set or psycopg2 not installed — skipping real-Postgres integration test",
)


@pytest.fixture
def pg_conn():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    yield conn
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM comp_pool")
        cur.execute("DELETE FROM comp_ingestion_runs")
    conn.commit()
    conn.close()


@pytest.fixture
def pg_repo(pg_conn):
    from app.scanner.comps.postgres_source import PostgresCompRepository
    return PostgresCompRepository(pg_conn)


class TestRealPostgresSpatialQueries:
    def test_upsert_and_dedup(self, pg_repo):
        from app.scanner.comps.repository import CompRow

        row = CompRow(
            address="1 Integration Test St", city="Memphis", state="TN", zip="38104",
            latitude=35.1495, longitude=-90.0490,
            sale_date=date.today() - timedelta(days=30),
            sale_price=150_000.0, sqft=1500, property_type="sfr",
        )
        assert pg_repo.upsert(row) is True
        assert pg_repo.upsert(row) is False  # same dedup_key — no-op, not an error
        assert pg_repo.count() == 1

    def test_conflict_uses_full_unique_constraint_not_partial_index(self, pg_repo):
        """
        Regression guard: comp_pool.dedup_key is NOT NULL + full UNIQUE
        (migration 003), unlike the earlier partial-index approach on
        comparable_sales, so ON CONFLICT (dedup_key) always has a matching
        constraint to target.
        """
        from app.scanner.comps.repository import CompRow

        row = CompRow(
            address="2 Conflict Ave", city="Memphis", state="TN", zip="38104",
            latitude=35.15, longitude=-90.05,
            sale_date=date.today() - timedelta(days=10),
            sale_price=175_000.0, sqft=1600, property_type="sfr",
        )
        pg_repo.upsert(row)
        pg_repo.upsert(row)  # would raise if the conflict target didn't match

    def test_query_within_radius_uses_st_dwithin(self, pg_repo):
        from app.scanner.comps.repository import CompRow

        near = CompRow(
            address="3 Near St", city="Memphis", state="TN", zip="38104",
            latitude=35.1500, longitude=-90.0495,
            sale_date=date.today() - timedelta(days=5),
            sale_price=155_000.0, sqft=1550, property_type="sfr",
        )
        far = CompRow(
            address="4 Far Rd", city="Nashville", state="TN", zip="37201",
            latitude=36.1627, longitude=-86.7816,
            sale_date=date.today() - timedelta(days=5),
            sale_price=300_000.0, sqft=2000, property_type="sfr",
        )
        pg_repo.upsert(near)
        pg_repo.upsert(far)

        results = pg_repo.query_within_radius(
            35.1495, -90.0490, radius_miles=1.0,
            cutoff_date=date.today() - timedelta(days=180),
        )
        addresses = [row.address for row, _ in results]
        assert "3 Near St" in addresses
        assert "4 Far Rd" not in addresses

    def test_query_excludes_sales_before_cutoff(self, pg_repo):
        from app.scanner.comps.repository import CompRow

        old = CompRow(
            address="5 Old Sale Ln", city="Memphis", state="TN", zip="38104",
            latitude=35.1495, longitude=-90.0490,
            sale_date=date.today() - timedelta(days=400),
            sale_price=140_000.0, sqft=1400, property_type="sfr",
        )
        pg_repo.upsert(old)
        results = pg_repo.query_within_radius(
            35.1495, -90.0490, radius_miles=5.0,
            cutoff_date=date.today() - timedelta(days=180),
        )
        assert results == []

    def test_query_result_values_are_floats_not_decimal(self, pg_repo):
        from decimal import Decimal
        from app.scanner.comps.repository import CompRow

        row = CompRow(
            address="6 Type Check Dr", city="Memphis", state="TN", zip="38104",
            latitude=35.1495, longitude=-90.0490,
            sale_date=date.today() - timedelta(days=5),
            sale_price=150_000.0, sqft=1500, bathrooms=2.0, property_type="sfr",
        )
        pg_repo.upsert(row)
        results = pg_repo.query_within_radius(
            35.1495, -90.0490, radius_miles=1.0,
            cutoff_date=date.today() - timedelta(days=180),
        )
        found, dist = results[0]
        assert not isinstance(found.sale_price, Decimal)
        assert not isinstance(found.bathrooms, Decimal)
        assert isinstance(dist, float)

    def test_ingestion_run_recorded(self, pg_repo):
        from app.scanner.comps.repository import IngestResult

        result = IngestResult(records_read=3, records_added=2, records_skipped=1)
        pg_repo.record_ingestion_run("csv", "test.csv", result)

        cur = pg_repo._conn.cursor()
        cur.execute("SELECT * FROM comp_ingestion_runs WHERE file_name = %(f)s", {"f": "test.csv"})
        rows = cur.fetchall()
        cur.close()
        assert len(rows) == 1
        assert rows[0]["records_added"] == 2
        assert rows[0]["status"] == "partial"

    def test_full_csv_ingestion_round_trip(self, pg_repo, tmp_path):
        from app.scanner.comps.ingestion import CsvCompIngester

        csv_path = tmp_path / "comps.csv"
        csv_path.write_text(
            "address,city,state,zip,latitude,longitude,sale_date,sale_price,sqft\n"
            "7 Round Trip Ct,Memphis,TN,38104,35.1495,-90.0490,"
            + (date.today() - timedelta(days=20)).isoformat()
            + ",160000,1550\n"
        )
        result = CsvCompIngester(pg_repo).ingest_file(csv_path)
        assert result.records_added == 1
        assert pg_repo.count() == 1

        # Re-ingesting the same file is idempotent thanks to dedup_key.
        result2 = CsvCompIngester(pg_repo).ingest_file(csv_path)
        assert result2.records_added == 0
        assert pg_repo.count() == 1
