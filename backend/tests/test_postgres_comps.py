"""
Tests for the Phase 2 comparable sales subsystem:
  - CompRow / dedup key computation
  - InMemoryCompRepository (spatial filtering, date cutoff, dedup)
  - PostgresCompSource (repository-agnostic — exercised against InMemoryCompRepository)
  - PostgresCompRepository SQL generation (exercised against a DB-API test double)
  - CsvCompIngester (parsing, dedup, error handling)
"""
from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import pytest

from app.scanner.comps.repository import (
    CompRepository,
    CompRow,
    InMemoryCompRepository,
    IngestResult,
    compute_dedup_key,
)
from app.scanner.comps.postgres_source import PostgresCompRepository, PostgresCompSource
from app.scanner.comps.ingestion import CsvCompIngester
from app.scanner.models import CandidateProperty


SUBJECT_LAT = 35.1495
SUBJECT_LNG = -90.0490
TODAY = date(2026, 7, 8)


def _subject(**overrides) -> CandidateProperty:
    defaults = dict(
        id=uuid4(),
        street_address="100 Main St",
        city="Memphis",
        state="TN",
        zip="38104",
        property_type="sfr",
        sqft=1500,
        bedrooms=3,
        bathrooms=2.0,
        year_built=1980,
        latitude=SUBJECT_LAT,
        longitude=SUBJECT_LNG,
    )
    defaults.update(overrides)
    return CandidateProperty(**defaults)


def _row(
    address="123 Oak St",
    lat=None,
    lng=None,
    sale_date=None,
    sale_price=150_000.0,
    sqft=1500,
    property_type="sfr",
    **kwargs,
) -> CompRow:
    return CompRow(
        address=address,
        city="Memphis",
        state="TN",
        zip="38104",
        latitude=lat if lat is not None else SUBJECT_LAT + 0.001,
        longitude=lng if lng is not None else SUBJECT_LNG + 0.001,
        sale_date=sale_date or (TODAY - timedelta(days=30)),
        sale_price=sale_price,
        sqft=sqft,
        property_type=property_type,
        **kwargs,
    )


# ──────────────────────────────────────────────────────────────────────────────
# compute_dedup_key / CompRow
# ──────────────────────────────────────────────────────────────────────────────

class TestDedupKey:
    def test_deterministic(self):
        k1 = compute_dedup_key("123 Oak St", date(2026, 1, 1))
        k2 = compute_dedup_key("123 Oak St", date(2026, 1, 1))
        assert k1 == k2

    def test_case_and_whitespace_insensitive(self):
        k1 = compute_dedup_key("123 Oak St", date(2026, 1, 1))
        k2 = compute_dedup_key("  123 OAK ST  ", date(2026, 1, 1))
        assert k1 == k2

    def test_street_suffix_variants_produce_same_key(self):
        k1 = compute_dedup_key("123 Oak Street", date(2026, 1, 1))
        k2 = compute_dedup_key("123 Oak St", date(2026, 1, 1))
        assert k1 == k2

    def test_punctuation_ignored(self):
        k1 = compute_dedup_key("123 Oak St.", date(2026, 1, 1))
        k2 = compute_dedup_key("123 Oak St", date(2026, 1, 1))
        assert k1 == k2

    def test_different_address_differs(self):
        k1 = compute_dedup_key("123 Oak St", date(2026, 1, 1))
        k2 = compute_dedup_key("456 Elm St", date(2026, 1, 1))
        assert k1 != k2

    def test_different_price_same_key(self):
        """
        Sale price is intentionally excluded from the dedup key: the same
        closed sale is often reported with slightly different prices across
        providers or after a correction, and should still be treated as one
        sale rather than duplicated.
        """
        k1 = compute_dedup_key("123 Oak St", date(2026, 1, 1))
        k2 = compute_dedup_key("123 Oak St", date(2026, 1, 1))
        assert k1 == k2

    def test_different_date_differs(self):
        k1 = compute_dedup_key("123 Oak St", date(2026, 1, 1))
        k2 = compute_dedup_key("123 Oak St", date(2026, 1, 2))
        assert k1 != k2

    def test_comp_row_auto_computes_dedup_key(self):
        row = _row()
        assert row.dedup_key != ""
        assert len(row.dedup_key) == 32

    def test_comp_row_explicit_dedup_key_preserved(self):
        row = _row(dedup_key="custom-key")
        assert row.dedup_key == "custom-key"

    def test_comp_row_same_address_date_different_price_dedupes(self):
        """Codifies the price-exclusion behavior at the CompRow level."""
        row1 = _row(address="1 Oak St", sale_date=date(2026, 1, 1), sale_price=150_000.0)
        row2 = _row(address="1 Oak St", sale_date=date(2026, 1, 1), sale_price=149_999.60)
        assert row1.dedup_key == row2.dedup_key


# ──────────────────────────────────────────────────────────────────────────────
# InMemoryCompRepository — dedup
# ──────────────────────────────────────────────────────────────────────────────

class TestInMemoryRepositoryDedup:
    def test_upsert_new_row_returns_true(self):
        repo = InMemoryCompRepository()
        assert repo.upsert(_row()) is True

    def test_upsert_duplicate_returns_false(self):
        repo = InMemoryCompRepository()
        row = _row()
        repo.upsert(row)
        assert repo.upsert(_row(address=row.address, sale_date=row.sale_date, sale_price=row.sale_price)) is False

    def test_count_reflects_unique_rows_only(self):
        repo = InMemoryCompRepository()
        row = _row()
        repo.upsert(row)
        repo.upsert(_row(address=row.address, sale_date=row.sale_date, sale_price=row.sale_price))
        assert repo.count() == 1

    def test_distinct_rows_both_stored(self):
        repo = InMemoryCompRepository()
        repo.upsert(_row(address="1 First St"))
        repo.upsert(_row(address="2 Second St"))
        assert repo.count() == 2


# ──────────────────────────────────────────────────────────────────────────────
# InMemoryCompRepository — spatial + date filtering
# ──────────────────────────────────────────────────────────────────────────────

class TestInMemoryRepositoryQuery:
    def test_within_radius_included(self):
        repo = InMemoryCompRepository()
        repo.upsert(_row(lat=SUBJECT_LAT + 0.003, lng=SUBJECT_LNG))  # ~0.2 mi
        results = repo.query_within_radius(SUBJECT_LAT, SUBJECT_LNG, radius_miles=0.5, cutoff_date=TODAY - timedelta(days=180))
        assert len(results) == 1

    def test_outside_radius_excluded(self):
        repo = InMemoryCompRepository()
        repo.upsert(_row(lat=SUBJECT_LAT + 0.05, lng=SUBJECT_LNG))  # ~3.4 mi
        results = repo.query_within_radius(SUBJECT_LAT, SUBJECT_LNG, radius_miles=0.5, cutoff_date=TODAY - timedelta(days=180))
        assert len(results) == 0

    def test_sale_before_cutoff_excluded(self):
        repo = InMemoryCompRepository()
        repo.upsert(_row(sale_date=TODAY - timedelta(days=400)))
        results = repo.query_within_radius(SUBJECT_LAT, SUBJECT_LNG, radius_miles=5, cutoff_date=TODAY - timedelta(days=180))
        assert len(results) == 0

    def test_sale_on_cutoff_included(self):
        repo = InMemoryCompRepository()
        cutoff = TODAY - timedelta(days=180)
        repo.upsert(_row(sale_date=cutoff))
        results = repo.query_within_radius(SUBJECT_LAT, SUBJECT_LNG, radius_miles=5, cutoff_date=cutoff)
        assert len(results) == 1

    def test_property_type_filter(self):
        repo = InMemoryCompRepository()
        repo.upsert(_row(address="1 A St", property_type="sfr"))
        repo.upsert(_row(address="2 B St", property_type="condo"))
        results = repo.query_within_radius(
            SUBJECT_LAT, SUBJECT_LNG, radius_miles=5,
            cutoff_date=TODAY - timedelta(days=180), property_type="condo",
        )
        assert len(results) == 1
        assert results[0][0].property_type == "condo"

    def test_no_property_type_filter_returns_all(self):
        repo = InMemoryCompRepository()
        repo.upsert(_row(address="1 A St", property_type="sfr"))
        repo.upsert(_row(address="2 B St", property_type="condo"))
        results = repo.query_within_radius(SUBJECT_LAT, SUBJECT_LNG, radius_miles=5, cutoff_date=TODAY - timedelta(days=180))
        assert len(results) == 2

    def test_results_sorted_by_distance(self):
        repo = InMemoryCompRepository()
        repo.upsert(_row(address="Far", lat=SUBJECT_LAT + 0.01))
        repo.upsert(_row(address="Near", lat=SUBJECT_LAT + 0.001))
        results = repo.query_within_radius(SUBJECT_LAT, SUBJECT_LNG, radius_miles=5, cutoff_date=TODAY - timedelta(days=180))
        assert results[0][0].address == "Near"
        assert results[1][0].address == "Far"

    def test_limit_caps_results(self):
        repo = InMemoryCompRepository()
        for i in range(5):
            repo.upsert(_row(address=f"{i} St", lat=SUBJECT_LAT + i * 0.0001))
        results = repo.query_within_radius(SUBJECT_LAT, SUBJECT_LNG, radius_miles=5, cutoff_date=TODAY - timedelta(days=180), limit=2)
        assert len(results) == 2

    def test_distance_miles_returned(self):
        repo = InMemoryCompRepository()
        repo.upsert(_row(lat=SUBJECT_LAT + 0.01, lng=SUBJECT_LNG))
        results = repo.query_within_radius(SUBJECT_LAT, SUBJECT_LNG, radius_miles=5, cutoff_date=TODAY - timedelta(days=180))
        _, dist = results[0]
        assert dist > 0


# ──────────────────────────────────────────────────────────────────────────────
# PostgresCompSource — repository-agnostic (exercised via in-memory repo)
# ──────────────────────────────────────────────────────────────────────────────

class TestPostgresCompSource:
    def test_no_coordinates_returns_empty(self):
        repo = InMemoryCompRepository()
        repo.upsert(_row())
        source = PostgresCompSource(repo)
        subject = _subject(latitude=None, longitude=None)
        comps = source.fetch_candidates(subject, radius_miles=0.5, date_range_months=6, sqft_tolerance_pct=20, max_candidates=8)
        assert comps == []

    def test_returns_comparable_sale_objects(self):
        repo = InMemoryCompRepository()
        repo.upsert(_row())
        source = PostgresCompSource(repo)
        subject = _subject()
        comps = source.fetch_candidates(subject, radius_miles=0.5, date_range_months=6, sqft_tolerance_pct=20, max_candidates=8)
        assert len(comps) == 1
        assert comps[0].subject_property_id == subject.id
        assert comps[0].distance_miles is not None

    def test_sqft_tolerance_filters_out(self):
        repo = InMemoryCompRepository()
        repo.upsert(_row(sqft=3000))  # subject sqft=1500, way outside ±20%
        source = PostgresCompSource(repo)
        subject = _subject(sqft=1500)
        comps = source.fetch_candidates(subject, radius_miles=0.5, date_range_months=6, sqft_tolerance_pct=20, max_candidates=8)
        assert comps == []

    def test_sqft_within_tolerance_included(self):
        repo = InMemoryCompRepository()
        repo.upsert(_row(sqft=1600))  # within ±20% of 1500
        source = PostgresCompSource(repo)
        subject = _subject(sqft=1500)
        comps = source.fetch_candidates(subject, radius_miles=0.5, date_range_months=6, sqft_tolerance_pct=20, max_candidates=8)
        assert len(comps) == 1

    def test_missing_sqft_skips_tolerance_filter(self):
        repo = InMemoryCompRepository()
        repo.upsert(_row(sqft=None))
        source = PostgresCompSource(repo)
        subject = _subject(sqft=1500)
        comps = source.fetch_candidates(subject, radius_miles=0.5, date_range_months=6, sqft_tolerance_pct=20, max_candidates=8)
        assert len(comps) == 1

    def test_max_candidates_respected(self):
        repo = InMemoryCompRepository()
        for i in range(10):
            repo.upsert(_row(address=f"{i} St", lat=SUBJECT_LAT + i * 0.0005))
        source = PostgresCompSource(repo)
        subject = _subject()
        comps = source.fetch_candidates(subject, radius_miles=5, date_range_months=6, sqft_tolerance_pct=50, max_candidates=3)
        assert len(comps) == 3

    def test_similarity_and_adjustment_fields_left_unset(self):
        repo = InMemoryCompRepository()
        repo.upsert(_row())
        source = PostgresCompSource(repo)
        subject = _subject()
        comps = source.fetch_candidates(subject, radius_miles=0.5, date_range_months=6, sqft_tolerance_pct=20, max_candidates=8)
        assert comps[0].similarity_score == 0.0
        assert comps[0].price_adjustment == 0.0

    def test_data_source_reflects_row_source_name(self):
        repo = InMemoryCompRepository()
        repo.upsert(_row(source_name="mls_export"))
        source = PostgresCompSource(repo)
        subject = _subject()
        comps = source.fetch_candidates(subject, radius_miles=0.5, date_range_months=6, sqft_tolerance_pct=20, max_candidates=8)
        assert comps[0].data_source == "mls_export"


# ──────────────────────────────────────────────────────────────────────────────
# PostgresCompRepository — SQL generation against a DB-API test double
# ──────────────────────────────────────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, fetchall_result=None, fetchone_result=None):
        self.executed_sql = None
        self.executed_params = None
        self.rowcount = 1
        self._fetchall_result = fetchall_result or []
        self._fetchone_result = fetchone_result

    def execute(self, sql, params=None):
        self.executed_sql = sql
        self.executed_params = params

    def fetchall(self):
        return self._fetchall_result

    def fetchone(self):
        return self._fetchone_result

    def close(self):
        pass


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class _FailingCursor:
    """Raises on execute() to simulate a DB error mid-statement."""

    def execute(self, sql, params=None):
        raise RuntimeError("simulated DB failure")

    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def close(self):
        pass


class TestPostgresCompRepositorySQL:
    def test_query_targets_comp_pool_not_comparable_sales(self):
        cur = _FakeCursor(fetchall_result=[])
        conn = _FakeConnection(cur)
        repo = PostgresCompRepository(conn)
        repo.query_within_radius(SUBJECT_LAT, SUBJECT_LNG, radius_miles=0.5, cutoff_date=TODAY)
        assert "FROM comp_pool" in cur.executed_sql
        assert "comparable_sales" not in cur.executed_sql

    def test_upsert_targets_comp_pool_not_comparable_sales(self):
        cur = _FakeCursor()
        conn = _FakeConnection(cur)
        repo = PostgresCompRepository(conn)
        repo.upsert(_row())
        assert "INTO comp_pool" in cur.executed_sql
        assert "comparable_sales" not in cur.executed_sql

    def test_query_uses_st_dwithin(self):
        cur = _FakeCursor(fetchall_result=[])
        conn = _FakeConnection(cur)
        repo = PostgresCompRepository(conn)
        repo.query_within_radius(SUBJECT_LAT, SUBJECT_LNG, radius_miles=0.5, cutoff_date=TODAY)
        assert "ST_DWithin" in cur.executed_sql

    def test_query_converts_radius_to_meters(self):
        cur = _FakeCursor(fetchall_result=[])
        conn = _FakeConnection(cur)
        repo = PostgresCompRepository(conn)
        repo.query_within_radius(SUBJECT_LAT, SUBJECT_LNG, radius_miles=1.0, cutoff_date=TODAY)
        assert cur.executed_params["radius_meters"] == pytest.approx(1609.34)

    def test_query_omits_property_type_clause_when_none(self):
        cur = _FakeCursor(fetchall_result=[])
        conn = _FakeConnection(cur)
        repo = PostgresCompRepository(conn)
        repo.query_within_radius(SUBJECT_LAT, SUBJECT_LNG, radius_miles=0.5, cutoff_date=TODAY)
        assert "property_type" not in cur.executed_params

    def test_query_adds_property_type_clause_when_given(self):
        cur = _FakeCursor(fetchall_result=[])
        conn = _FakeConnection(cur)
        repo = PostgresCompRepository(conn)
        repo.query_within_radius(SUBJECT_LAT, SUBJECT_LNG, radius_miles=0.5, cutoff_date=TODAY, property_type="condo")
        assert cur.executed_params["property_type"] == "condo"
        assert "property_type = %(property_type)s" in cur.executed_sql

    def test_query_maps_rows_to_comprow_and_distance(self):
        db_row = {
            "address": "1 Oak St", "city": "Memphis", "state": "TN", "zip": "38104",
            "latitude": SUBJECT_LAT, "longitude": SUBJECT_LNG,
            "sale_date": TODAY, "sale_price": 150000.0, "sqft": 1500,
            "bedrooms": 3, "bathrooms": 2.0, "year_built": 1980, "property_type": "sfr",
            "pool": False, "garage_spaces": 1, "condition": "good", "price_per_sqft": 100.0,
            "source_id": "abc", "source_name": "mls", "dedup_key": "deadbeef",
            "distance_meters": 804.67,  # 0.5 mi
        }
        cur = _FakeCursor(fetchall_result=[db_row])
        conn = _FakeConnection(cur)
        repo = PostgresCompRepository(conn)
        results = repo.query_within_radius(SUBJECT_LAT, SUBJECT_LNG, radius_miles=1.0, cutoff_date=TODAY)
        assert len(results) == 1
        row, dist = results[0]
        assert row.address == "1 Oak St"
        assert row.dedup_key == "deadbeef"
        assert dist == pytest.approx(0.5, rel=0.01)

    def test_upsert_uses_on_conflict_do_nothing(self):
        cur = _FakeCursor()
        conn = _FakeConnection(cur)
        repo = PostgresCompRepository(conn)
        repo.upsert(_row())
        assert "ON CONFLICT (dedup_key) DO NOTHING" in cur.executed_sql

    def test_upsert_commits(self):
        cur = _FakeCursor()
        conn = _FakeConnection(cur)
        repo = PostgresCompRepository(conn)
        repo.upsert(_row())
        assert conn.committed is True

    def test_upsert_returns_true_when_rowcount_one(self):
        cur = _FakeCursor()
        cur.rowcount = 1
        conn = _FakeConnection(cur)
        repo = PostgresCompRepository(conn)
        assert repo.upsert(_row()) is True

    def test_upsert_returns_false_when_conflict_skipped(self):
        cur = _FakeCursor()
        cur.rowcount = 0  # ON CONFLICT DO NOTHING -> 0 rows affected
        conn = _FakeConnection(cur)
        repo = PostgresCompRepository(conn)
        assert repo.upsert(_row()) is False

    def test_count_returns_dict_row_value(self):
        cur = _FakeCursor(fetchone_result={"n": 42})
        conn = _FakeConnection(cur)
        repo = PostgresCompRepository(conn)
        assert repo.count() == 42

    def test_count_returns_tuple_row_value(self):
        cur = _FakeCursor(fetchone_result=(7,))
        conn = _FakeConnection(cur)
        repo = PostgresCompRepository(conn)
        assert repo.count() == 7

    def test_query_maps_decimal_like_values_to_float(self):
        """
        Simulates a driver returning Decimal for NUMERIC columns — the
        repository must cast to float so ARV arithmetic never mixes types.
        """
        from decimal import Decimal

        db_row = {
            "address": "1 Oak St", "city": "Memphis", "state": "TN", "zip": "38104",
            "latitude": Decimal("35.1495"), "longitude": Decimal("-90.0490"),
            "sale_date": TODAY, "sale_price": Decimal("150000.00"), "sqft": 1500,
            "bedrooms": 3, "bathrooms": Decimal("2.0"), "year_built": 1980, "property_type": "sfr",
            "pool": False, "garage_spaces": 1, "condition": "good", "price_per_sqft": Decimal("100.00"),
            "source_id": "abc", "source_name": "mls", "dedup_key": "deadbeef",
            "distance_meters": Decimal("804.67"),
        }
        cur = _FakeCursor(fetchall_result=[db_row])
        conn = _FakeConnection(cur)
        repo = PostgresCompRepository(conn)
        results = repo.query_within_radius(SUBJECT_LAT, SUBJECT_LNG, radius_miles=1.0, cutoff_date=TODAY)
        row, dist = results[0]
        assert isinstance(row.sale_price, float)
        assert isinstance(row.latitude, float)
        assert isinstance(row.bathrooms, float)
        assert isinstance(row.price_per_sqft, float)
        assert isinstance(dist, float)


class TestPostgresCompRepositoryRollback:
    def test_query_rolls_back_on_failure(self):
        conn = _FakeConnection(_FailingCursor())
        repo = PostgresCompRepository(conn)
        with pytest.raises(RuntimeError):
            repo.query_within_radius(SUBJECT_LAT, SUBJECT_LNG, radius_miles=0.5, cutoff_date=TODAY)
        assert conn.rolled_back is True

    def test_upsert_rolls_back_on_failure(self):
        conn = _FakeConnection(_FailingCursor())
        repo = PostgresCompRepository(conn)
        with pytest.raises(RuntimeError):
            repo.upsert(_row())
        assert conn.rolled_back is True
        assert conn.committed is False

    def test_count_rolls_back_on_failure(self):
        conn = _FakeConnection(_FailingCursor())
        repo = PostgresCompRepository(conn)
        with pytest.raises(RuntimeError):
            repo.count()
        assert conn.rolled_back is True

    def test_record_ingestion_run_rolls_back_on_failure(self):
        conn = _FakeConnection(_FailingCursor())
        repo = PostgresCompRepository(conn)
        with pytest.raises(RuntimeError):
            repo.record_ingestion_run("csv", "comps.csv", IngestResult(records_read=1, records_added=1))
        assert conn.rolled_back is True


class TestPostgresCompRepositoryIngestionRunAudit:
    def test_record_ingestion_run_inserts_into_comp_ingestion_runs(self):
        cur = _FakeCursor()
        conn = _FakeConnection(cur)
        repo = PostgresCompRepository(conn)
        result = IngestResult(records_read=5, records_added=4, records_skipped=1, errors=["row 3: bad"])
        repo.record_ingestion_run("csv", "comps.csv", result)
        assert "comp_ingestion_runs" in cur.executed_sql
        assert cur.executed_params["records_read"] == 5
        assert cur.executed_params["records_added"] == 4
        assert cur.executed_params["error_count"] == 1
        assert cur.executed_params["status"] == "partial"

    def test_record_ingestion_run_commits(self):
        cur = _FakeCursor()
        conn = _FakeConnection(cur)
        repo = PostgresCompRepository(conn)
        repo.record_ingestion_run("csv", "comps.csv", IngestResult(records_read=1, records_added=1))
        assert conn.committed is True


# ──────────────────────────────────────────────────────────────────────────────
# CsvCompIngester
# ──────────────────────────────────────────────────────────────────────────────

def _write_csv(path, rows: list[dict]):
    import csv as _csv
    fieldnames = sorted({k for row in rows for k in row.keys()})
    with open(path, "w", newline="") as fh:
        writer = _csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


class TestCsvCompIngester:
    def test_ingest_valid_rows(self, tmp_path):
        csv_path = tmp_path / "comps.csv"
        _write_csv(csv_path, [
            {"address": "1 Oak St", "city": "Memphis", "state": "TN", "zip": "38104",
             "latitude": "35.15", "longitude": "-90.05", "sale_date": "2026-01-01",
             "sale_price": "150000", "sqft": "1500"},
            {"address": "2 Elm St", "city": "Memphis", "state": "TN", "zip": "38104",
             "latitude": "35.16", "longitude": "-90.06", "sale_date": "2026-02-01",
             "sale_price": "160000", "sqft": "1600"},
        ])
        repo = InMemoryCompRepository()
        ingester = CsvCompIngester(repo)
        result = ingester.ingest_file(csv_path)
        assert result.records_read == 2
        assert result.records_added == 2
        assert result.records_skipped == 0
        assert result.status == "success"
        assert repo.count() == 2

    def test_ingest_skips_missing_required_fields(self, tmp_path):
        csv_path = tmp_path / "comps.csv"
        _write_csv(csv_path, [
            {"address": "1 Oak St", "city": "Memphis", "state": "TN", "zip": "38104",
             "latitude": "35.15", "longitude": "-90.05", "sale_date": "2026-01-01",
             "sale_price": "150000", "sqft": "1500"},
            {"address": "Missing Coords", "city": "Memphis", "state": "TN", "zip": "38104",
             "latitude": "", "longitude": "", "sale_date": "2026-02-01",
             "sale_price": "160000", "sqft": "1600"},
        ])
        repo = InMemoryCompRepository()
        ingester = CsvCompIngester(repo)
        result = ingester.ingest_file(csv_path)
        assert result.records_read == 2
        assert result.records_added == 1
        assert result.records_skipped == 1
        assert result.status == "partial"

    def test_ingest_zero_price_skipped(self, tmp_path):
        csv_path = tmp_path / "comps.csv"
        _write_csv(csv_path, [
            {"address": "1 Oak St", "city": "Memphis", "state": "TN", "zip": "38104",
             "latitude": "35.15", "longitude": "-90.05", "sale_date": "2026-01-01",
             "sale_price": "0", "sqft": "1500"},
        ])
        repo = InMemoryCompRepository()
        result = CsvCompIngester(repo).ingest_file(csv_path)
        assert result.records_added == 0
        assert result.records_skipped == 1

    def test_ingest_dedups_within_same_file(self, tmp_path):
        csv_path = tmp_path / "comps.csv"
        _write_csv(csv_path, [
            {"address": "1 Oak St", "city": "Memphis", "state": "TN", "zip": "38104",
             "latitude": "35.15", "longitude": "-90.05", "sale_date": "2026-01-01",
             "sale_price": "150000", "sqft": "1500"},
            {"address": "1 Oak St", "city": "Memphis", "state": "TN", "zip": "38104",
             "latitude": "35.15", "longitude": "-90.05", "sale_date": "2026-01-01",
             "sale_price": "150000", "sqft": "1500"},
        ])
        repo = InMemoryCompRepository()
        result = CsvCompIngester(repo).ingest_file(csv_path)
        assert result.records_read == 2
        assert result.records_added == 1
        assert result.records_skipped == 1
        assert repo.count() == 1

    def test_reingesting_same_file_is_idempotent(self, tmp_path):
        csv_path = tmp_path / "comps.csv"
        _write_csv(csv_path, [
            {"address": "1 Oak St", "city": "Memphis", "state": "TN", "zip": "38104",
             "latitude": "35.15", "longitude": "-90.05", "sale_date": "2026-01-01",
             "sale_price": "150000", "sqft": "1500"},
        ])
        repo = InMemoryCompRepository()
        ingester = CsvCompIngester(repo)
        first = ingester.ingest_file(csv_path)
        second = ingester.ingest_file(csv_path)
        assert first.records_added == 1
        assert second.records_added == 0
        assert second.records_skipped == 1
        assert repo.count() == 1

    def test_ingest_nonexistent_file_reports_error(self, tmp_path):
        repo = InMemoryCompRepository()
        result = CsvCompIngester(repo).ingest_file(tmp_path / "does_not_exist.csv")
        assert result.status == "failed"
        assert len(result.errors) == 1

    def test_ingest_sets_source_name(self, tmp_path):
        csv_path = tmp_path / "comps.csv"
        _write_csv(csv_path, [
            {"address": "1 Oak St", "city": "Memphis", "state": "TN", "zip": "38104",
             "latitude": "35.15", "longitude": "-90.05", "sale_date": "2026-01-01",
             "sale_price": "150000", "sqft": "1500"},
        ])
        repo = InMemoryCompRepository()
        CsvCompIngester(repo, source_name="propstream").ingest_file(csv_path)
        row = next(iter(repo._rows.values()))
        assert row.source_name == "propstream"

    def test_ingest_parses_optional_fields(self, tmp_path):
        csv_path = tmp_path / "comps.csv"
        _write_csv(csv_path, [
            {"address": "1 Oak St", "city": "Memphis", "state": "TN", "zip": "38104",
             "latitude": "35.15", "longitude": "-90.05", "sale_date": "2026-01-01",
             "sale_price": "150000", "sqft": "1500", "pool": "true",
             "garage_spaces": "2", "condition": "Good"},
        ])
        repo = InMemoryCompRepository()
        CsvCompIngester(repo).ingest_file(csv_path)
        row = next(iter(repo._rows.values()))
        assert row.pool is True
        assert row.garage_spaces == 2
        assert row.condition == "good"

    def test_ingest_result_status_success_when_no_issues(self, tmp_path):
        csv_path = tmp_path / "comps.csv"
        _write_csv(csv_path, [
            {"address": "1 Oak St", "city": "Memphis", "state": "TN", "zip": "38104",
             "latitude": "35.15", "longitude": "-90.05", "sale_date": "2026-01-01",
             "sale_price": "150000", "sqft": "1500"},
        ])
        repo = InMemoryCompRepository()
        result = CsvCompIngester(repo).ingest_file(csv_path)
        assert result.status == "success"


class TestCsvCompIngesterAuditWiring:
    def test_ingest_records_ingestion_run(self, tmp_path):
        csv_path = tmp_path / "comps.csv"
        _write_csv(csv_path, [
            {"address": "1 Oak St", "city": "Memphis", "state": "TN", "zip": "38104",
             "latitude": "35.15", "longitude": "-90.05", "sale_date": "2026-01-01",
             "sale_price": "150000", "sqft": "1500"},
        ])
        repo = InMemoryCompRepository()
        CsvCompIngester(repo, source_name="propstream").ingest_file(csv_path)
        assert len(repo.ingestion_runs) == 1
        run = repo.ingestion_runs[0]
        assert run["source_name"] == "propstream"
        assert run["file_name"] == str(csv_path)
        assert run["result"].records_added == 1

    def test_ingest_records_run_even_when_file_missing(self, tmp_path):
        repo = InMemoryCompRepository()
        CsvCompIngester(repo).ingest_file(tmp_path / "missing.csv")
        assert len(repo.ingestion_runs) == 1
        assert repo.ingestion_runs[0]["result"].status == "failed"

    def test_ingest_run_started_before_completed(self, tmp_path):
        csv_path = tmp_path / "comps.csv"
        _write_csv(csv_path, [
            {"address": "1 Oak St", "city": "Memphis", "state": "TN", "zip": "38104",
             "latitude": "35.15", "longitude": "-90.05", "sale_date": "2026-01-01",
             "sale_price": "150000", "sqft": "1500"},
        ])
        repo = InMemoryCompRepository()
        CsvCompIngester(repo).ingest_file(csv_path)
        run = repo.ingestion_runs[0]
        assert run["started_at"] <= run["completed_at"]
