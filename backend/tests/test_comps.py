"""
Comps layer tests — CSV source, simulated source, and ARVEngine integration.
Run with: pytest backend/tests/test_comps.py -v

All tests are pure in-memory; no real network calls or external files required.
CSV tests write temporary files using pytest's tmp_path fixture.
"""
from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.scanner.arv import ARVEngine, _apply_adjustments, _similarity_score
from app.scanner.comps.csv_source import CsvCompSource
from app.scanner.comps.source import CompSource, SimulatedCompSource
from app.scanner.models import ARVConfidence, CandidateProperty, ComparableSale, DistressType


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ──────────────────────────────────────────────────────────────────────────────

# Subject property coordinates (downtown Memphis)
SUBJECT_LAT = 35.1495
SUBJECT_LNG = -90.0490

# Nearby coords — ~0.24 mi from subject (within default 0.5 mi radius)
NEAR_LAT = 35.1530
NEAR_LNG = -90.0490

# Far coords — ~0.73 mi from subject (outside default 0.5 mi radius)
FAR_LAT = 35.1600
FAR_LNG = -90.0490

# Even farther — ~1.4 mi
VERY_FAR_LAT = 35.1700
VERY_FAR_LNG = -90.0490


def _subject(**kwargs) -> CandidateProperty:
    defaults = dict(
        street_address="100 Test St",
        city="Memphis",
        state="TN",
        zip="38103",
        county="Shelby County",
        latitude=SUBJECT_LAT,
        longitude=SUBJECT_LNG,
        property_type="sfr",
        bedrooms=3,
        bathrooms=2.0,
        sqft=1400,
        year_built=1985,
        distress_types=[DistressType.CODE_VIOLATION],
        distress_score=60,
    )
    defaults.update(kwargs)
    return CandidateProperty(**defaults)


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _comp_row(**kwargs) -> dict:
    defaults = dict(
        address="200 Oak St",
        city="Memphis",
        state="TN",
        zip="38103",
        latitude=NEAR_LAT,
        longitude=NEAR_LNG,
        sale_date=_days_ago(30),
        sale_price=140_000,
        sqft=1_450,
        bedrooms=3,
        bathrooms=2.0,
        year_built=1983,
        property_type="sfr",
    )
    defaults.update(kwargs)
    return defaults


def _write_csv(tmp_path: Path, rows: list[dict]) -> Path:
    """Write rows to a temp CSV file; returns the path."""
    path = tmp_path / "comps.csv"
    fieldnames = [
        "address", "city", "state", "zip", "latitude", "longitude",
        "sale_date", "sale_price", "sqft", "bedrooms", "bathrooms",
        "year_built", "property_type",
    ]
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def _make_comp(
    distance_miles: float = 0.2,
    sqft: int = 1_450,
    bedrooms: int = 3,
    bathrooms: float = 2.0,
    year_built: int = 1983,
    sale_price: float = 140_000,
    days_ago: int = 30,
    subject: CandidateProperty = None,
) -> ComparableSale:
    subj = subject or _subject()
    ppf = round(sale_price / sqft, 2) if sqft else None
    return ComparableSale(
        subject_property_id=subj.id,
        address="200 Oak St",
        city="Memphis",
        state="TN",
        zip="38103",
        sale_date=date.today() - timedelta(days=days_ago),
        sale_price=sale_price,
        sqft=sqft,
        bedrooms=bedrooms,
        bathrooms=bathrooms,
        year_built=year_built,
        property_type="sfr",
        latitude=NEAR_LAT,
        longitude=NEAR_LNG,
        price_per_sqft=ppf,
        distance_miles=distance_miles,
        data_source="test",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Interface compliance
# ──────────────────────────────────────────────────────────────────────────────

class TestCompSourceInterface:
    def test_csv_source_is_comp_source(self, tmp_path):
        path = _write_csv(tmp_path, [_comp_row()])
        assert isinstance(CsvCompSource(path), CompSource)

    def test_simulated_source_is_comp_source(self):
        assert isinstance(SimulatedCompSource(), CompSource)


# ──────────────────────────────────────────────────────────────────────────────
# CSV loading
# ──────────────────────────────────────────────────────────────────────────────

class TestCsvLoad:
    def test_loads_valid_rows(self, tmp_path):
        rows = [_comp_row(), _comp_row(address="201 Oak St")]
        source = CsvCompSource(_write_csv(tmp_path, rows))
        assert len(source._rows) == 2

    def test_skips_row_without_latitude(self, tmp_path):
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row(latitude="")]))
        assert len(source._rows) == 0

    def test_skips_row_without_longitude(self, tmp_path):
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row(longitude="")]))
        assert len(source._rows) == 0

    def test_skips_row_without_sale_date(self, tmp_path):
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row(sale_date="")]))
        assert len(source._rows) == 0

    def test_skips_row_without_sale_price(self, tmp_path):
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row(sale_price="")]))
        assert len(source._rows) == 0

    def test_skips_row_with_zero_price(self, tmp_path):
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row(sale_price=0)]))
        assert len(source._rows) == 0

    def test_case_insensitive_headers(self, tmp_path):
        path = tmp_path / "comps.csv"
        path.write_text(
            "ADDRESS,CITY,STATE,ZIP,LATITUDE,LONGITUDE,SALE_DATE,SALE_PRICE,"
            "SQFT,BEDROOMS,BATHROOMS,YEAR_BUILT,PROPERTY_TYPE\n"
            f"200 Oak St,Memphis,TN,38103,{NEAR_LAT},{NEAR_LNG},"
            f"{_days_ago(30)},140000,1450,3,2.0,1983,sfr\n"
        )
        assert len(CsvCompSource(path)._rows) == 1

    def test_city_title_cased(self, tmp_path):
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row(city="MEMPHIS")]))
        assert source._rows[0]["city"] == "Memphis"

    def test_state_uppercased(self, tmp_path):
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row(state="tn")]))
        assert source._rows[0]["state"] == "TN"

    def test_price_per_sqft_calculated(self, tmp_path):
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row(sale_price=145_000, sqft=1_000)]))
        assert source._rows[0]["price_per_sqft"] == 145.0

    def test_missing_optional_fields_default_to_none(self, tmp_path):
        # bedrooms / bathrooms / year_built are optional
        row = _comp_row()
        row.pop("bedrooms")
        row.pop("bathrooms")
        row.pop("year_built")
        path = tmp_path / "comps.csv"
        fieldnames = [k for k in _comp_row() if k not in ("bedrooms", "bathrooms", "year_built")]
        with path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerow(row)
        source = CsvCompSource(path)
        assert len(source._rows) == 1
        assert source._rows[0]["bedrooms"] is None
        assert source._rows[0]["bathrooms"] is None

    def test_empty_csv_loads_zero_rows(self, tmp_path):
        source = CsvCompSource(_write_csv(tmp_path, []))
        assert source._rows == []

    def test_nonexistent_file_loads_zero_rows(self, tmp_path):
        source = CsvCompSource(tmp_path / "missing.csv")
        assert source._rows == []


# ──────────────────────────────────────────────────────────────────────────────
# Hard-filter pipeline
# ──────────────────────────────────────────────────────────────────────────────

class TestCsvFetchCandidates:

    def _fetch(self, source, subject=None, **kw):
        defaults = dict(
            radius_miles=0.5,
            date_range_months=6,
            sqft_tolerance_pct=20,
            max_candidates=20,
        )
        defaults.update(kw)
        return source.fetch_candidates(subject or _subject(), **defaults)

    # ── Radius filter ────────────────────────────────────────────────────────

    def test_includes_comp_within_radius(self, tmp_path):
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row(latitude=NEAR_LAT, longitude=NEAR_LNG)]))
        assert len(self._fetch(source)) == 1

    def test_excludes_comp_outside_radius(self, tmp_path):
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row(latitude=FAR_LAT, longitude=FAR_LNG)]))
        assert len(self._fetch(source)) == 0

    def test_exactly_on_radius_boundary_included(self, tmp_path):
        # NEAR_LAT is well within 0.5 mi — just verify boundary direction is right
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row(latitude=NEAR_LAT)]))
        results = self._fetch(source, radius_miles=0.5)
        assert len(results) == 1

    # ── Property type filter ─────────────────────────────────────────────────

    def test_excludes_different_property_type(self, tmp_path):
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row(property_type="mfr")]))
        assert len(self._fetch(source)) == 0

    def test_includes_matching_property_type(self, tmp_path):
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row(property_type="sfr")]))
        assert len(self._fetch(source)) == 1

    # ── Date filter ──────────────────────────────────────────────────────────

    def test_excludes_comp_older_than_date_range(self, tmp_path):
        old_date = _days_ago(200)   # outside 6-month window
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row(sale_date=old_date)]))
        assert len(self._fetch(source)) == 0

    def test_includes_comp_within_date_range(self, tmp_path):
        recent = _days_ago(60)
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row(sale_date=recent)]))
        assert len(self._fetch(source)) == 1

    # ── Sqft filter ──────────────────────────────────────────────────────────

    def test_excludes_comp_outside_sqft_tolerance(self, tmp_path):
        # Subject sqft=1400, ±20% = [1120, 1680].  2100 is outside.
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row(sqft=2_100)]))
        assert len(self._fetch(source)) == 0

    def test_includes_comp_within_sqft_tolerance(self, tmp_path):
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row(sqft=1_550)]))
        assert len(self._fetch(source)) == 1

    def test_no_sqft_filter_when_subject_sqft_missing(self, tmp_path):
        # A comp with sqft=3000 would fail ±20% filter, but subject has no sqft
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row(sqft=3_000)]))
        results = self._fetch(source, subject=_subject(sqft=None))
        assert len(results) == 1

    def test_no_sqft_filter_when_comp_sqft_missing(self, tmp_path):
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row(sqft="")]))
        results = self._fetch(source)
        assert len(results) == 1

    # ── Sorting and capping ──────────────────────────────────────────────────

    def test_results_sorted_by_distance_asc(self, tmp_path):
        rows = [
            _comp_row(address="Far",    latitude=35.1520, longitude=NEAR_LNG),  # ~0.17 mi
            _comp_row(address="Close",  latitude=35.1500, longitude=NEAR_LNG),  # ~0.03 mi
        ]
        source = CsvCompSource(_write_csv(tmp_path, rows))
        results = self._fetch(source)
        assert results[0].address == "Close"

    def test_max_candidates_respected(self, tmp_path):
        rows = [_comp_row(address=f"{i} Oak St") for i in range(10)]
        source = CsvCompSource(_write_csv(tmp_path, rows))
        results = self._fetch(source, max_candidates=3)
        assert len(results) == 3

    # ── Output fields ────────────────────────────────────────────────────────

    def test_distance_miles_is_set(self, tmp_path):
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row()]))
        result = self._fetch(source)[0]
        assert result.distance_miles is not None
        assert result.distance_miles < 0.5

    def test_subject_property_id_is_set(self, tmp_path):
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row()]))
        subj = _subject()
        results = source.fetch_candidates(subj, radius_miles=1.0, date_range_months=6,
                                          sqft_tolerance_pct=20, max_candidates=10)
        assert results[0].subject_property_id == subj.id

    def test_data_source_is_csv(self, tmp_path):
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row()]))
        assert self._fetch(source)[0].data_source == "csv"

    def test_returns_empty_when_no_comps_pass_filters(self, tmp_path):
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row(latitude=FAR_LAT)]))
        assert self._fetch(source) == []

    def test_returns_empty_when_subject_has_no_coordinates(self, tmp_path):
        source = CsvCompSource(_write_csv(tmp_path, [_comp_row()]))
        results = self._fetch(source, subject=_subject(latitude=None, longitude=None))
        assert results == []

    def test_multiple_filters_applied_together(self, tmp_path):
        """All four hard filters must pass simultaneously."""
        rows = [
            _comp_row(address="Good"),
            _comp_row(address="Bad-radius",   latitude=FAR_LAT),
            _comp_row(address="Bad-date",     sale_date=_days_ago(200)),
            _comp_row(address="Bad-type",     property_type="mfr"),
            _comp_row(address="Bad-sqft",     sqft=3_000),
        ]
        source = CsvCompSource(_write_csv(tmp_path, rows))
        results = self._fetch(source)
        assert len(results) == 1
        assert results[0].address == "Good"


# ──────────────────────────────────────────────────────────────────────────────
# ARVEngine integration with CsvCompSource
# ──────────────────────────────────────────────────────────────────────────────

class TestARVEngineWithCsvSource:

    def _engine_with_rows(self, tmp_path, rows):
        return ARVEngine(comp_source=CsvCompSource(_write_csv(tmp_path, rows)))

    def test_returns_arv_result_with_comps(self, tmp_path):
        rows = [_comp_row(address=f"{i} Oak St") for i in range(5)]
        engine = self._engine_with_rows(tmp_path, rows)
        result = engine.calculate(_subject(), uuid4())
        assert result.arv > 0
        assert result.comp_count == 5

    def test_arv_reflects_sale_prices(self, tmp_path):
        rows = [_comp_row(sale_price=200_000, address=f"{i} Rd") for i in range(4)]
        engine = self._engine_with_rows(tmp_path, rows)
        result = engine.calculate(_subject(), uuid4())
        # ARV should be in the ballpark of sale_price ± adjustments
        assert 150_000 < result.arv < 250_000

    def test_zero_comps_returns_insufficient(self, tmp_path):
        # No comps within radius
        rows = [_comp_row(latitude=FAR_LAT, longitude=FAR_LNG)]
        engine = self._engine_with_rows(tmp_path, rows)
        result = engine.calculate(_subject(), uuid4())
        assert result.confidence == ARVConfidence.INSUFFICIENT
        assert result.comp_count == 0
        assert result.arv == 0.0

    def test_three_comps_gives_at_least_low_confidence(self, tmp_path):
        rows = [_comp_row(address=f"{i} Oak St") for i in range(3)]
        engine = self._engine_with_rows(tmp_path, rows)
        result = engine.calculate(_subject(), uuid4())
        assert result.comp_count == 3
        assert result.confidence in (ARVConfidence.LOW, ARVConfidence.MEDIUM, ARVConfidence.HIGH)

    def test_six_comps_can_give_high_confidence(self, tmp_path):
        # Identical sale prices → zero variance → maximum confidence for count
        rows = [_comp_row(address=f"{i} Oak St", sale_price=145_000) for i in range(6)]
        engine = self._engine_with_rows(tmp_path, rows)
        result = engine.calculate(_subject(), uuid4())
        assert result.comp_count == 6
        assert result.confidence == ARVConfidence.HIGH

    def test_similarity_scores_applied_to_comps(self, tmp_path):
        rows = [_comp_row()]
        engine = self._engine_with_rows(tmp_path, rows)
        result = engine.calculate(_subject(), uuid4())
        for comp in result.comps:
            assert comp.similarity_score > 0

    def test_price_adjustments_applied(self, tmp_path):
        rows = [_comp_row()]
        engine = self._engine_with_rows(tmp_path, rows)
        result = engine.calculate(_subject(), uuid4())
        for comp in result.comps:
            # adjusted_price = sale_price + price_adjustment
            assert abs(comp.adjusted_price - (comp.sale_price + comp.price_adjustment)) < 0.01

    def test_subject_without_coords_gives_insufficient(self, tmp_path):
        rows = [_comp_row()]
        engine = self._engine_with_rows(tmp_path, rows)
        result = engine.calculate(_subject(latitude=None, longitude=None), uuid4())
        assert result.confidence == ARVConfidence.INSUFFICIENT

    def test_max_comps_config_respected(self, tmp_path):
        # 20 comps in CSV, engine should cap at arv_max_comps (default 10)
        rows = [_comp_row(address=f"{i} Oak St") for i in range(20)]
        engine = self._engine_with_rows(tmp_path, rows)
        result = engine.calculate(_subject(), uuid4(), max_comps=5)
        assert result.comp_count <= 5

    def test_best_similarity_comps_selected_when_over_max(self, tmp_path):
        # One close comp (high similarity) and many far ones (low similarity)
        close = _comp_row(address="Close", latitude=35.1497, longitude=SUBJECT_LNG)
        far_rows = [_comp_row(address=f"Far{i}", latitude=35.1525, longitude=NEAR_LNG) for i in range(20)]
        engine = self._engine_with_rows(tmp_path, [close] + far_rows)
        result = engine.calculate(_subject(), uuid4(), max_comps=5)
        addresses = [c.address for c in result.comps]
        assert "Close" in addresses  # closest comp should survive the cap


# ──────────────────────────────────────────────────────────────────────────────
# Simulated comp source
# ──────────────────────────────────────────────────────────────────────────────

class TestSimulatedCompSource:

    def _fetch(self, source, subject=None, **kw):
        defaults = dict(radius_miles=0.5, date_range_months=6,
                        sqft_tolerance_pct=20, max_candidates=8)
        defaults.update(kw)
        return source.fetch_candidates(subject or _subject(), **defaults)

    def test_returns_list(self):
        comps = self._fetch(SimulatedCompSource(seed=1))
        assert isinstance(comps, list)

    def test_empty_when_subject_has_no_coordinates(self):
        comps = self._fetch(SimulatedCompSource(), subject=_subject(latitude=None, longitude=None))
        assert comps == []

    def test_empty_when_subject_has_no_sqft(self):
        comps = self._fetch(SimulatedCompSource(), subject=_subject(sqft=None))
        assert comps == []

    def test_distance_miles_set(self):
        source = SimulatedCompSource(seed=3)
        for _ in range(10):
            comps = self._fetch(source)
            for comp in comps:
                assert comp.distance_miles is not None
                assert comp.distance_miles <= 0.5

    def test_subject_id_set(self):
        subject = _subject()
        comps = self._fetch(SimulatedCompSource(seed=4), subject=subject)
        for comp in comps:
            assert comp.subject_property_id == subject.id

    def test_max_candidates_respected(self):
        comps = self._fetch(SimulatedCompSource(seed=5), max_candidates=3)
        assert len(comps) <= 3

    def test_different_seeds_produce_different_counts(self):
        counts = set()
        for seed in range(30):
            comps = self._fetch(SimulatedCompSource(seed=seed))
            counts.add(len(comps))
        assert len(counts) > 1  # not all the same


# ──────────────────────────────────────────────────────────────────────────────
# Similarity scoring unit tests
# ──────────────────────────────────────────────────────────────────────────────

class TestSimilarityScoring:

    def test_closer_comp_scores_higher(self):
        subject = _subject()
        close = _make_comp(distance_miles=0.1, subject=subject)
        far = _make_comp(distance_miles=0.45, subject=subject)
        assert _similarity_score(subject, close) > _similarity_score(subject, far)

    def test_matching_sqft_scores_higher(self):
        subject = _subject(sqft=1_400)
        match = _make_comp(sqft=1_400, subject=subject)
        mismatch = _make_comp(sqft=2_000, subject=subject)
        assert _similarity_score(subject, match) > _similarity_score(subject, mismatch)

    def test_matching_beds_scores_higher(self):
        subject = _subject(bedrooms=3)
        match = _make_comp(bedrooms=3, subject=subject)
        mismatch = _make_comp(bedrooms=6, subject=subject)
        assert _similarity_score(subject, match) > _similarity_score(subject, mismatch)

    def test_recent_sale_scores_higher(self):
        subject = _subject()
        recent = _make_comp(days_ago=20, subject=subject)
        old = _make_comp(days_ago=170, subject=subject)
        assert _similarity_score(subject, recent) > _similarity_score(subject, old)

    def test_score_bounded_between_0_and_100(self):
        subject = _subject()
        worst = _make_comp(distance_miles=0.49, sqft=2_000, bedrooms=8, days_ago=180, subject=subject)
        best = _make_comp(distance_miles=0.01, sqft=1_400, bedrooms=3, days_ago=5, subject=subject)
        assert 0 <= _similarity_score(subject, worst) <= 100
        assert 0 <= _similarity_score(subject, best) <= 100


# ──────────────────────────────────────────────────────────────────────────────
# Adjustment grid unit tests
# ──────────────────────────────────────────────────────────────────────────────

class TestAdjustments:

    def test_subject_more_beds_positive_adjustment(self):
        subject = _subject(bedrooms=4)
        comp = _make_comp(bedrooms=3, subject=subject)
        assert _apply_adjustments(subject, comp) > 0

    def test_subject_fewer_beds_negative_adjustment(self):
        subject = _subject(bedrooms=2)
        comp = _make_comp(bedrooms=4, subject=subject)
        assert _apply_adjustments(subject, comp) < 0

    def test_matching_beds_baths_zero_bed_bath_adjustment(self):
        subject = _subject(bedrooms=3, bathrooms=2.0, sqft=1_450)
        comp = _make_comp(bedrooms=3, bathrooms=2.0, sqft=1_450, subject=subject)
        # With matching beds/baths/sqft, only garage/pool can contribute; default is 0
        adj = _apply_adjustments(subject, comp)
        assert adj == 0.0

    def test_larger_sqft_subject_positive_adjustment(self):
        subject = _subject(sqft=1_800, bedrooms=3, bathrooms=2.0)
        comp = _make_comp(sqft=1_400, bedrooms=3, bathrooms=2.0, sale_price=140_000, subject=subject)
        adj = _apply_adjustments(subject, comp)
        assert adj > 0  # subject is larger → upward adjustment

    def test_pool_adds_positive_adjustment(self):
        subject_no_pool = _subject(pool=False)
        subject_pool = _subject(pool=True)
        comp = _make_comp(subject=subject_no_pool)
        adj_no_pool = _apply_adjustments(subject_no_pool, comp)
        adj_pool = _apply_adjustments(subject_pool, comp)
        assert adj_pool > adj_no_pool

    def test_garage_adds_positive_adjustment(self):
        subject_no_garage = _subject(garage_spaces=0)
        subject_garage = _subject(garage_spaces=2)
        comp = _make_comp(subject=subject_no_garage)
        adj_no_garage = _apply_adjustments(subject_no_garage, comp)
        adj_garage = _apply_adjustments(subject_garage, comp)
        assert adj_garage > adj_no_garage


# ──────────────────────────────────────────────────────────────────────────────
# Backwards compatibility — existing ARVEngine tests must still pass
# ──────────────────────────────────────────────────────────────────────────────

class TestARVEngineBackwardsCompat:
    """Verify that ARVEngine() with no comp_source behaves exactly as before."""

    def test_simulation_path_returns_result(self):
        engine = ARVEngine()
        result = engine.calculate(_subject(), uuid4())
        assert result.arv >= 0
        assert result.property_id is not None

    def test_rng_swap_changes_output(self):
        import random
        engine = ARVEngine()
        confidences = set()
        for seed in range(20):
            engine._rng = random.Random(seed)
            r = engine.calculate(_subject(), uuid4())
            confidences.add(r.confidence)
        assert len(confidences) >= 2  # multiple confidence levels across seeds

    def test_no_sqft_returns_insufficient(self):
        engine = ARVEngine()
        result = engine.calculate(_subject(sqft=None), uuid4())
        assert result.comp_count == 0
        assert result.confidence == ARVConfidence.INSUFFICIENT
