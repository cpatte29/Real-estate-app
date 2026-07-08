"""
Tests for MemphisCodeEnforcementSource and ShelbyParcelEnricher.

All network calls are mocked — no real HTTP requests are made.

Run with:
    python3 -m pytest tests/test_memphis_source.py -v
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, Mock, patch, call
import pytest
import requests

from app.scanner.models import DistressType
from app.scanner.sources.memphis_code_enforcement import (
    MemphisCodeEnforcementSource,
    ParcelAttributes,
    ShelbyParcelEnricher,
    SourceUnavailableError,
    _address_fingerprint,
    _distress_score,
    _distress_types,
    _map_category,
    _mlgw_is_off,
    _parse_location,
)


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures — realistic Socrata row shapes
# ──────────────────────────────────────────────────────────────────────────────

def _socrata_row(
    incident_number: str = "CE-2024-001234",
    ce_category: str = "GROUNDS MAINTENANCE",
    full_address: str = "1234 BEALE ST",
    city: str = "Memphis",
    state: str = "TN",
    postal_code: str = "38103",
    parcel_id: str = "076-A-5-0012-00",
    owner_name: str = "DOE JOHN",
    request_status: str = "Open",
    mlgw_on: str = "false",
    mlgw_status: str = "Off",
    lat: str = "35.1387",
    lng: str = "-90.0525",
    number_of_tasks: str = "3",
) -> dict:
    """Build a dict that looks like one Socrata API row."""
    return {
        "incident_number": incident_number,
        "ce_category": ce_category,
        "request_type": ce_category,
        "request_status": request_status,
        "full_address": full_address,
        "city": city,
        "state": state,
        "postal_code": postal_code,
        "parcel_id": parcel_id,
        "owner_name": owner_name,
        "creation_date": "2024-03-15T00:00:00.000",
        "mlgw_on": mlgw_on,
        "mlgw_status": mlgw_status,
        "number_of_tasks": number_of_tasks,
        "next_open_task_date": "2024-06-01T00:00:00.000",
        "location_1": {
            "latitude": lat,
            "longitude": lng,
            "human_address": json.dumps({
                "address": full_address, "city": city,
                "state": state, "zip": postal_code,
            }),
        },
    }


def _arcgis_response(
    parcel_id: str = "076-A-5-0012-00",
    assessed: float = 25_000,
    appraised: float = 100_000,
    sqft: int = 1_400,
    beds: int = 3,
    baths: float = 1.5,
    year_built: int = 1962,
) -> dict:
    """Build a dict that looks like one ArcGIS FeatureServer response."""
    return {
        "features": [{
            "attributes": {
                "PARCELID": parcel_id,
                "OWNERNAME": "DOE JOHN",
                "ASSESSEDVALUE": assessed,
                "APPRAISEDVALUE": appraised,
                "LANDVALUE": 10_000,
                "IMPROVEMENTVALUE": 90_000,
                "ACREAGE": 0.12,
                "YEARBUILT": year_built,
                "SQFT": sqft,
                "BEDS": beds,
                "BATHS": baths,
                "ZONING": "R-A",
                "TAXDISTCODE": "TN",
            }
        }]
    }


def _make_mock_response(json_body, status_code=200):
    resp = Mock(spec=requests.Response)
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.text = json.dumps(json_body)
    resp.raise_for_status = Mock()
    return resp


# ──────────────────────────────────────────────────────────────────────────────
# Pure-function unit tests (no mocking needed)
# ──────────────────────────────────────────────────────────────────────────────

class TestCategoryMapping:
    def test_condemnation_maps_to_code_violation_high_score(self):
        dtype, score = _map_category("CONDEMNATION")
        assert dtype == DistressType.CODE_VIOLATION
        assert score >= 80

    def test_grounds_maintenance_maps_to_vacancy(self):
        dtype, score = _map_category("GROUNDS MAINTENANCE")
        assert dtype == DistressType.VACANCY
        assert score >= 60

    def test_blight_maps_to_code_violation(self):
        dtype, score = _map_category("BLIGHT")
        assert dtype == DistressType.CODE_VIOLATION
        assert score >= 70

    def test_board_up_maps_to_vacancy(self):
        dtype, score = _map_category("BOARD-UP")
        assert dtype == DistressType.VACANCY
        assert score >= 75

    def test_residential_maps_to_code_violation(self):
        dtype, score = _map_category("RESIDENTIAL CODE ENFORCEMENT")
        assert dtype == DistressType.CODE_VIOLATION

    def test_unknown_category_defaults_to_code_violation_low_score(self):
        dtype, score = _map_category("SOME NEW CATEGORY")
        assert dtype == DistressType.CODE_VIOLATION
        assert score <= 50

    def test_empty_string_does_not_crash(self):
        dtype, score = _map_category("")
        assert isinstance(dtype, DistressType)
        assert 0 <= score <= 100

    def test_case_insensitive(self):
        dtype1, score1 = _map_category("condemnation")
        dtype2, score2 = _map_category("CONDEMNATION")
        assert dtype1 == dtype2 and score1 == score2


class TestMLGWDetection:
    def test_mlgw_off_when_status_is_off(self):
        assert _mlgw_is_off({"mlgw_status": "Off"}) is True

    def test_mlgw_off_when_mlgw_on_is_false(self):
        assert _mlgw_is_off({"mlgw_on": "false"}) is True

    def test_mlgw_on_when_status_is_active(self):
        assert _mlgw_is_off({"mlgw_status": "Active", "mlgw_on": "true"}) is False

    def test_mlgw_on_when_fields_missing(self):
        assert _mlgw_is_off({}) is False

    def test_mlgw_case_insensitive(self):
        assert _mlgw_is_off({"mlgw_status": "INACTIVE"}) is True


class TestDistressScore:
    def test_base_score_returned_without_mlgw_penalty(self):
        score = _distress_score(60, mlgw_off=False, open_tasks=0)
        assert score == 60

    def test_mlgw_off_adds_bonus(self):
        score = _distress_score(60, mlgw_off=True, open_tasks=0)
        assert score > 60

    def test_open_tasks_add_points(self):
        score_0 = _distress_score(50, mlgw_off=False, open_tasks=0)
        score_5 = _distress_score(50, mlgw_off=False, open_tasks=5)
        assert score_5 > score_0

    def test_score_capped_at_100(self):
        score = _distress_score(95, mlgw_off=True, open_tasks=99)
        assert score == 100

    def test_score_minimum_is_0(self):
        score = _distress_score(0, mlgw_off=False, open_tasks=0)
        assert score == 0


class TestDistressTypes:
    def test_vacancy_added_when_mlgw_off(self):
        types = _distress_types(DistressType.CODE_VIOLATION, mlgw_off=True)
        assert DistressType.VACANCY in types
        assert DistressType.CODE_VIOLATION in types

    def test_vacancy_not_duplicated_when_primary_is_vacancy(self):
        types = _distress_types(DistressType.VACANCY, mlgw_off=True)
        assert types.count(DistressType.VACANCY) == 1

    def test_no_vacancy_when_mlgw_on(self):
        types = _distress_types(DistressType.CODE_VIOLATION, mlgw_off=False)
        assert DistressType.VACANCY not in types


class TestLocationParsing:
    def test_standard_socrata_location_object(self):
        loc = {"latitude": "35.1387", "longitude": "-90.0525"}
        lat, lng = _parse_location({"location_1": loc})
        assert abs(lat - 35.1387) < 0.0001
        assert abs(lng - (-90.0525)) < 0.0001

    def test_geojson_point_fallback(self):
        loc = {"type": "Point", "coordinates": [-90.0525, 35.1387]}
        lat, lng = _parse_location({"location_1": loc})
        assert abs(lat - 35.1387) < 0.0001

    def test_missing_location_returns_none_none(self):
        lat, lng = _parse_location({})
        assert lat is None and lng is None

    def test_json_string_location(self):
        loc_str = json.dumps({"latitude": "35.1", "longitude": "-90.0"})
        lat, lng = _parse_location({"location_1": loc_str})
        assert lat is not None

    def test_malformed_location_returns_none_none(self):
        lat, lng = _parse_location({"location_1": "not a location"})
        assert lat is None and lng is None


class TestAddressFingerprint:
    def test_same_address_same_fingerprint(self):
        fp1 = _address_fingerprint("1234 Beale St", "Memphis", "TN")
        fp2 = _address_fingerprint("1234 Beale St", "Memphis", "TN")
        assert fp1 == fp2

    def test_different_addresses_different_fingerprints(self):
        fp1 = _address_fingerprint("1234 Beale St", "Memphis", "TN")
        fp2 = _address_fingerprint("5678 Main Ave", "Memphis", "TN")
        assert fp1 != fp2

    def test_case_insensitive(self):
        fp1 = _address_fingerprint("1234 BEALE ST", "MEMPHIS", "tn")
        fp2 = _address_fingerprint("1234 beale st", "memphis", "TN")
        assert fp1 == fp2

    def test_fingerprint_is_16_chars(self):
        fp = _address_fingerprint("100 Main St", "Memphis", "TN")
        assert len(fp) == 16


# ──────────────────────────────────────────────────────────────────────────────
# ShelbyParcelEnricher tests (mocked HTTP)
# ──────────────────────────────────────────────────────────────────────────────

class TestShelbyParcelEnricher:
    def _make_enricher(self, session):
        return ShelbyParcelEnricher(session=session, timeout=5)

    def test_returns_parsed_attributes_on_success(self):
        session = MagicMock()
        session.get.return_value = _make_mock_response(_arcgis_response())
        enricher = self._make_enricher(session)

        attrs = enricher.enrich("076-A-5-0012-00")

        assert attrs.sqft == 1_400
        assert attrs.bedrooms == 3
        assert attrs.bathrooms == 1.5
        assert attrs.year_built == 1962
        assert attrs.appraised_value == 100_000
        assert attrs.assessed_value == 25_000

    def test_caches_result_on_second_call(self):
        session = MagicMock()
        session.get.return_value = _make_mock_response(_arcgis_response())
        enricher = self._make_enricher(session)

        enricher.enrich("076-A-5-0012-00")
        enricher.enrich("076-A-5-0012-00")

        # Network should only be called once
        assert session.get.call_count == 1

    def test_different_parcels_make_separate_calls(self):
        session = MagicMock()
        session.get.return_value = _make_mock_response(_arcgis_response())
        enricher = self._make_enricher(session)

        enricher.enrich("076-A-5-0012-00")
        enricher.enrich("076-B-7-0099-00")

        assert session.get.call_count == 2

    def test_returns_empty_attrs_when_feature_not_found(self):
        session = MagicMock()
        session.get.return_value = _make_mock_response({"features": []})
        enricher = self._make_enricher(session)

        attrs = enricher.enrich("UNKNOWN-PARCEL")
        assert attrs.sqft is None
        assert attrs.appraised_value is None

    def test_returns_empty_attrs_on_network_error(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("timeout")
        enricher = self._make_enricher(session)

        attrs = enricher.enrich("076-A-5-0012-00")
        assert isinstance(attrs, ParcelAttributes)
        assert attrs.sqft is None  # graceful degradation

    def test_returns_empty_attrs_on_http_error(self):
        session = MagicMock()
        session.get.return_value = _make_mock_response({}, status_code=503)
        session.get.return_value.raise_for_status.side_effect = requests.HTTPError("503")
        enricher = self._make_enricher(session)

        attrs = enricher.enrich("076-A-5-0012-00")
        assert attrs.sqft is None

    def test_empty_parcel_id_returns_empty_attrs_without_network_call(self):
        session = MagicMock()
        enricher = self._make_enricher(session)

        attrs = enricher.enrich("")
        session.get.assert_not_called()
        assert attrs.sqft is None

    def test_sql_injection_chars_stripped_from_parcel_id(self):
        session = MagicMock()
        session.get.return_value = _make_mock_response({"features": []})
        enricher = self._make_enricher(session)

        enricher.enrich("'; DROP TABLE parcels; --")

        call_params = session.get.call_args.kwargs["params"]
        where = call_params["where"]
        # Single-quote and semicolon are stripped so the injected string cannot
        # break out of the SQL string literal context.
        injected_value = where.split("'")[1]  # value between the PARCELID quotes
        assert "'" not in injected_value
        assert ";" not in injected_value


# ──────────────────────────────────────────────────────────────────────────────
# MemphisCodeEnforcementSource integration tests (mocked HTTP)
# ──────────────────────────────────────────────────────────────────────────────

class TestMemphisCodeEnforcementSource:
    """All tests use mocked sessions; no real network traffic."""

    def _make_source(self, socrata_rows, parcel_attrs=None, enrich=True):
        """
        Build a source with a mocked session.
        socrata_rows: list of row dicts the Socrata API will return
        parcel_attrs: dict the CERT_Parcel API will return (or None to disable enrichment)
        """
        source = MemphisCodeEnforcementSource(
            app_token="test-token",
            enrich_parcels=enrich,
            request_timeout=5,
            page_delay=0,
        )
        # Replace the session with a mock
        mock_session = MagicMock()
        source._session = mock_session
        source._enricher = ShelbyParcelEnricher(session=mock_session, timeout=5)

        arcgis_body = parcel_attrs or _arcgis_response()

        def side_effect(url, **kwargs):
            if "data.memphistn.gov" in url:
                return _make_mock_response(socrata_rows)
            elif "shelbycountytn.gov" in url:
                return _make_mock_response(arcgis_body)
            raise ValueError(f"Unexpected URL: {url}")

        mock_session.get.side_effect = side_effect
        return source

    # ── fetch_candidates ──────────────────────────────────────────────────

    def test_returns_candidate_properties(self):
        rows = [_socrata_row()]
        source = self._make_source(rows)
        candidates = source.fetch_candidates(batch_size=10, offset=0)
        assert len(candidates) == 1

    def test_street_address_title_cased(self):
        rows = [_socrata_row(full_address="1234 BEALE ST")]
        source = self._make_source(rows)
        candidates = source.fetch_candidates(batch_size=10, offset=0)
        assert candidates[0].street_address == "1234 Beale St"

    def test_state_uppercased(self):
        rows = [_socrata_row(state="tn")]
        source = self._make_source(rows)
        candidates = source.fetch_candidates(batch_size=10, offset=0)
        assert candidates[0].state == "TN"

    def test_data_source_is_memphis(self):
        rows = [_socrata_row()]
        source = self._make_source(rows)
        candidates = source.fetch_candidates(batch_size=10, offset=0)
        assert candidates[0].data_source == "memphis_code_enforcement"

    def test_county_is_shelby(self):
        rows = [_socrata_row()]
        source = self._make_source(rows)
        candidates = source.fetch_candidates(batch_size=10, offset=0)
        assert candidates[0].county == "Shelby County"

    def test_source_record_id_is_incident_number(self):
        rows = [_socrata_row(incident_number="CE-2024-001234")]
        source = self._make_source(rows)
        candidates = source.fetch_candidates(batch_size=10, offset=0)
        assert candidates[0].source_record_id == "CE-2024-001234"

    # ── Distress classification ────────────────────────────────────────────

    def test_grounds_maintenance_produces_vacancy_type(self):
        rows = [_socrata_row(ce_category="GROUNDS MAINTENANCE")]
        source = self._make_source(rows)
        candidates = source.fetch_candidates(batch_size=10, offset=0)
        assert DistressType.VACANCY in candidates[0].distress_types

    def test_condemnation_produces_code_violation_type(self):
        rows = [_socrata_row(ce_category="CONDEMNATION")]
        source = self._make_source(rows)
        candidates = source.fetch_candidates(batch_size=10, offset=0)
        assert DistressType.CODE_VIOLATION in candidates[0].distress_types

    def test_mlgw_off_adds_vacancy_type(self):
        rows = [_socrata_row(ce_category="RESIDENTIAL CODE ENFORCEMENT",
                             mlgw_status="Off", mlgw_on="false")]
        source = self._make_source(rows)
        candidates = source.fetch_candidates(batch_size=10, offset=0)
        assert DistressType.VACANCY in candidates[0].distress_types

    def test_distress_score_is_positive_integer(self):
        rows = [_socrata_row()]
        source = self._make_source(rows)
        candidates = source.fetch_candidates(batch_size=10, offset=0)
        assert 0 < candidates[0].distress_score <= 100

    def test_condemnation_has_higher_score_than_residential(self):
        cond_row = [_socrata_row(ce_category="CONDEMNATION", mlgw_on="true", mlgw_status="Active")]
        res_row  = [_socrata_row(ce_category="RESIDENTIAL CODE ENFORCEMENT", mlgw_on="true", mlgw_status="Active")]

        source_cond = self._make_source(cond_row)
        source_res  = self._make_source(res_row)

        cond_score = source_cond.fetch_candidates(1)[0].distress_score
        res_score  = source_res.fetch_candidates(1)[0].distress_score
        assert cond_score > res_score

    # ── Location ──────────────────────────────────────────────────────────

    def test_lat_lng_parsed_from_location_1(self):
        rows = [_socrata_row(lat="35.1387", lng="-90.0525")]
        source = self._make_source(rows)
        candidates = source.fetch_candidates(batch_size=10, offset=0)
        assert abs(candidates[0].latitude - 35.1387) < 0.001
        assert abs(candidates[0].longitude - (-90.0525)) < 0.001

    def test_missing_location_returns_none_coords(self):
        row = _socrata_row()
        row.pop("location_1")
        source = self._make_source([row])
        candidates = source.fetch_candidates(batch_size=10, offset=0)
        assert candidates[0].latitude is None
        assert candidates[0].longitude is None

    # ── Parcel enrichment ─────────────────────────────────────────────────

    def test_enrichment_populates_sqft_from_parcel(self):
        rows = [_socrata_row(parcel_id="076-A-5-0012-00")]
        source = self._make_source(rows, parcel_attrs=_arcgis_response(sqft=1_400))
        candidates = source.fetch_candidates(batch_size=10, offset=0)
        assert candidates[0].sqft == 1_400

    def test_enrichment_populates_year_built(self):
        rows = [_socrata_row(parcel_id="076-A-5-0012-00")]
        source = self._make_source(rows, parcel_attrs=_arcgis_response(year_built=1962))
        candidates = source.fetch_candidates(batch_size=10, offset=0)
        assert candidates[0].year_built == 1962

    def test_enrichment_populates_assessed_value(self):
        rows = [_socrata_row(parcel_id="076-A-5-0012-00")]
        source = self._make_source(rows, parcel_attrs=_arcgis_response(assessed=25_000))
        candidates = source.fetch_candidates(batch_size=10, offset=0)
        assert candidates[0].assessed_value == 25_000

    def test_enrichment_disabled_when_flag_false(self):
        rows = [_socrata_row(parcel_id="076-A-5-0012-00")]
        source = self._make_source(rows, enrich=False)
        candidates = source.fetch_candidates(batch_size=10, offset=0)
        # Without enrichment sqft/year_built come back None (no parcel call)
        assert candidates[0].sqft is None

    def test_enrichment_failure_does_not_drop_property(self):
        """A bad ArcGIS response must not prevent the property from being returned."""
        row = _socrata_row(parcel_id="076-A-5-0012-00")
        source = self._make_source([row], enrich=True)

        # Make the ArcGIS call fail
        def side_effect(url, **kwargs):
            if "data.memphistn.gov" in url:
                return _make_mock_response([row])
            raise requests.ConnectionError("ArcGIS down")

        source._session.get.side_effect = side_effect

        candidates = source.fetch_candidates(batch_size=10, offset=0)
        # Property still returned; attributes just None
        assert len(candidates) == 1
        assert candidates[0].sqft is None

    def test_no_parcel_id_skips_enrichment(self):
        rows = [_socrata_row(parcel_id="")]
        source = self._make_source(rows, enrich=True)
        # Track ArcGIS calls
        arcgis_calls = []

        def side_effect(url, **kwargs):
            if "data.memphistn.gov" in url:
                return _make_mock_response(rows)
            arcgis_calls.append(url)
            return _make_mock_response(_arcgis_response())

        source._session.get.side_effect = side_effect
        source.fetch_candidates(batch_size=10, offset=0)
        # ArcGIS must not be called if there's no parcel_id
        assert len(arcgis_calls) == 0

    # ── Error handling ────────────────────────────────────────────────────

    def test_raises_source_unavailable_on_socrata_http_error(self):
        source = MemphisCodeEnforcementSource(enrich_parcels=False, page_delay=0)
        mock_session = MagicMock()
        resp = _make_mock_response({}, status_code=503)
        mock_session.get.return_value = resp
        source._session = mock_session

        with pytest.raises(SourceUnavailableError):
            source.fetch_candidates(batch_size=10, offset=0)

    def test_raises_source_unavailable_on_network_error(self):
        source = MemphisCodeEnforcementSource(enrich_parcels=False, page_delay=0)
        mock_session = MagicMock()
        mock_session.get.side_effect = requests.ConnectionError("timeout")
        source._session = mock_session

        with pytest.raises(SourceUnavailableError):
            source.fetch_candidates(batch_size=10, offset=0)

    def test_malformed_row_skipped_not_crashed(self):
        """A row that cannot be parsed should be skipped; valid rows still returned."""
        bad_row  = {"incident_number": "BAD", "ce_category": None,
                    "full_address": None, "city": None}  # will fail title() on None
        good_row = _socrata_row(incident_number="CE-2024-GOOD")

        source = self._make_source([bad_row, good_row])
        candidates = source.fetch_candidates(batch_size=10, offset=0)
        # Only the good row survives
        assert len(candidates) == 1
        assert candidates[0].source_record_id == "CE-2024-GOOD"

    def test_empty_socrata_response_returns_empty_list(self):
        source = self._make_source([])
        candidates = source.fetch_candidates(batch_size=10, offset=0)
        assert candidates == []

    # ── Pagination ────────────────────────────────────────────────────────

    def test_offset_and_limit_passed_to_socrata(self):
        source = MemphisCodeEnforcementSource(enrich_parcels=False, page_delay=0)
        mock_session = MagicMock()
        mock_session.get.return_value = _make_mock_response([])
        source._session = mock_session

        source.fetch_candidates(batch_size=25, offset=50)

        params = mock_session.get.call_args.kwargs["params"]
        assert params["$limit"] == 25
        assert params["$offset"] == 50

    def test_app_token_sent_in_header(self):
        source = MemphisCodeEnforcementSource(
            app_token="my-secret-token", enrich_parcels=False, page_delay=0
        )
        # App token is set on the session headers at init time — check before any mocking
        assert source._session.headers.get("X-App-Token") == "my-secret-token"

    # ── PropertySource interface compliance ───────────────────────────────

    def test_implements_property_source_interface(self):
        from app.scanner.discovery import PropertySource
        source = MemphisCodeEnforcementSource(enrich_parcels=False)
        assert isinstance(source, PropertySource)

    def test_fingerprint_is_consistent_for_same_address(self):
        row = _socrata_row(full_address="1234 BEALE ST", city="Memphis", state="TN")
        source = self._make_source([row, row])  # same row twice
        candidates = source.fetch_candidates(batch_size=10, offset=0)
        fps = [c.address_fingerprint for c in candidates]
        assert fps[0] == fps[1]

    def test_fingerprint_differs_across_addresses(self):
        row1 = _socrata_row(full_address="1234 BEALE ST", incident_number="A")
        row2 = _socrata_row(full_address="9999 MAIN AVE", incident_number="B")
        source = self._make_source([row1, row2])
        candidates = source.fetch_candidates(batch_size=10, offset=0)
        assert candidates[0].address_fingerprint != candidates[1].address_fingerprint
