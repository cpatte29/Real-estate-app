"""
Memphis / Shelby County Distressed Property Source
===================================================

Primary data:   Memphis Open Data — Socrata SODA API
Dataset:        "Active Code Enforcement by Type"
ID:             h4nu-tbge
Endpoint:       https://data.memphistn.gov/resource/h4nu-tbge.json
Docs:           https://data.memphistn.gov/dataset/Active-Code-Enforcement-by-Type/h4nu-tbge
Auth:           None required. App token raises rate limit from ~1 req/s to ~10 req/s.
                Register free at https://data.memphistn.gov/login

Enrichment:     Shelby County ReGIS — CERT_Parcel (ArcGIS MapServer)
Endpoint:       https://gis.shelbycountytn.gov/arcgis/rest/services/Parcel/CERT_Parcel/MapServer/0/query
Auth:           None required (public).

Design
------
1.  Fetch a page of code enforcement records from Socrata (offset-based pagination).
2.  Filter to distress-relevant CE_CATEGORY values.
3.  Map CE_CATEGORY + MLGW_STATUS → DistressType list and distress_score.
4.  For each record that has a PARCEL_ID, optionally enrich from CERT_Parcel to
    pick up sqft, bedrooms, assessed value, and year built.
5.  Return a list[CandidateProperty] — same interface as SimulatedPropertySource.

Graceful degradation
--------------------
- If Socrata returns a non-2xx response, raises SourceUnavailableError (caller
  decides whether to fall back to simulation or abort the run).
- If CERT_Parcel enrichment fails for an individual parcel, that property is
  still returned with partial attributes (no crash).
- All Socrata field accesses use .get() with safe defaults — the API schema can
  gain or lose fields between dataset updates.

To plug in
----------
Replace SimulatedPropertySource in pipeline.py or config.py:

    from app.scanner.sources.memphis_code_enforcement import MemphisCodeEnforcementSource
    source = MemphisCodeEnforcementSource(app_token=os.getenv("SOCRATA_APP_TOKEN"))
    discovery = PropertyDiscovery(source)
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Optional
from urllib.parse import urlencode

import requests

from app.scanner.discovery import PropertySource
from app.scanner.models import CandidateProperty, DistressType

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Endpoint constants
# ──────────────────────────────────────────────────────────────────────────────

SOCRATA_ENDPOINT = "https://data.memphistn.gov/resource/h4nu-tbge.json"
CERT_PARCEL_ENDPOINT = (
    "https://gis.shelbycountytn.gov/arcgis/rest/services"
    "/Parcel/CERT_Parcel/MapServer/0/query"
)

# Socrata returns field names lowercased with spaces replaced by underscores.
# Map as confirmed by the dataset metadata.
_F_INCIDENT_NUMBER = "incident_number"
_F_CE_CATEGORY     = "ce_category"
_F_REQUEST_TYPE    = "request_type"
_F_REQUEST_STATUS  = "request_status"
_F_FULL_ADDRESS    = "full_address"
_F_ADDRESS1        = "address1"
_F_CITY            = "city"
_F_STATE           = "state"
_F_POSTAL_CODE     = "postal_code"
_F_OWNER_NAME      = "owner_name"
_F_PARCEL_ID       = "parcel_id"
_F_CREATION_DATE   = "creation_date"
_F_MLGW_ON         = "mlgw_on"
_F_MLGW_STATUS     = "mlgw_status"
_F_LOCATION        = "location_1"          # Socrata Point type

# ──────────────────────────────────────────────────────────────────────────────
# CE_CATEGORY → DistressType mapping
#
# Values observed in the Memphis dataset (case-insensitive match):
#   GROUNDS MAINTENANCE   — uncut weeds/grass, overgrown lots → strong vacancy signal
#   CONDEMNATION          — structure condemned by the city → highest severity
#   RESIDENTIAL CODE ENF  — residential code violation
#   COMMERCIAL CODE ENF   — commercial code violation
#   BLIGHT                — designated blight area property
#   BOARD-UP              — property boarded by city → confirmed vacant
#   DEBRIS/JUNK           — debris accumulation → neglect signal
#
# Unknown / unmapped categories fall through to CODE_VIOLATION as a safe default.
# ──────────────────────────────────────────────────────────────────────────────

# Maps a fragment of CE_CATEGORY (uppercased) to (DistressType, base_score)
_CATEGORY_MAP: list[tuple[str, DistressType, int]] = [
    ("CONDEMN",      DistressType.CODE_VIOLATION, 85),  # CONDEMNATION
    ("BOARD",        DistressType.VACANCY,        80),  # BOARD-UP
    ("BLIGHT",       DistressType.CODE_VIOLATION, 75),
    ("GROUNDS",      DistressType.VACANCY,        65),  # GROUNDS MAINTENANCE
    ("RESIDENTIAL",  DistressType.CODE_VIOLATION, 55),
    ("COMMERCIAL",   DistressType.CODE_VIOLATION, 45),
    ("DEBRIS",       DistressType.CODE_VIOLATION, 50),
    ("JUNK",         DistressType.CODE_VIOLATION, 45),
]

# MLGW utility cutoff adds a vacancy signal on top of any category
_MLGW_VACANCY_BONUS = 15    # extra distress score points when utility is off
_MLGW_OFF_VALUES = {"off", "inactive", "cut", "no", "false", "0"}


def _map_category(ce_category: str) -> tuple[DistressType, int]:
    """Return (DistressType, base_score) for a CE_CATEGORY string."""
    cat_upper = (ce_category or "").upper().strip()
    for fragment, dtype, score in _CATEGORY_MAP:
        if fragment in cat_upper:
            return dtype, score
    # Unknown category — treat as generic code violation, low score
    return DistressType.CODE_VIOLATION, 40


def _mlgw_is_off(row: dict) -> bool:
    """Return True if Memphis Light Gas & Water service is off for this property."""
    status = str(row.get(_F_MLGW_STATUS) or "").lower().strip()
    on_flag = str(row.get(_F_MLGW_ON) or "").lower().strip()
    return status in _MLGW_OFF_VALUES or on_flag in {"false", "no", "0"}


def _distress_score(base: int, mlgw_off: bool, open_tasks: int) -> int:
    score = base
    if mlgw_off:
        score += _MLGW_VACANCY_BONUS
    # Stacking open tasks indicates persistent neglect
    score += min(10, open_tasks * 2)
    return min(100, max(0, score))


def _distress_types(primary: DistressType, mlgw_off: bool) -> list[DistressType]:
    types = [primary]
    if mlgw_off and DistressType.VACANCY not in types:
        types.append(DistressType.VACANCY)
    return types


# ──────────────────────────────────────────────────────────────────────────────
# Location parsing — Socrata Point column
# ──────────────────────────────────────────────────────────────────────────────

def _parse_location(row: dict) -> tuple[Optional[float], Optional[float]]:
    """
    Socrata SODA 2.1 returns location columns as:
      {"latitude": "35.12", "longitude": "-90.04", "human_address": "..."}
    or sometimes as a GeoJSON Point:
      {"type": "Point", "coordinates": [-90.04, 35.12]}
    """
    loc = row.get(_F_LOCATION)
    if not loc:
        return None, None

    if isinstance(loc, str):
        try:
            loc = json.loads(loc)
        except (ValueError, TypeError):
            return None, None

    if not isinstance(loc, dict):
        return None, None

    # Standard Socrata location object
    try:
        lat = float(loc.get("latitude") or loc.get("lat") or 0)
        lng = float(loc.get("longitude") or loc.get("lon") or 0)
        if lat != 0 and lng != 0:
            return lat, lng
    except (TypeError, ValueError):
        pass

    # GeoJSON Point fallback
    coords = loc.get("coordinates")
    if coords and len(coords) >= 2:
        try:
            return float(coords[1]), float(coords[0])
        except (TypeError, ValueError):
            pass

    return None, None


def _address_fingerprint(street: str, city: str, state: str) -> str:
    normalized = f"{street.lower().strip()},{city.lower().strip()},{state.lower()}"
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


# ──────────────────────────────────────────────────────────────────────────────
# Parcel enricher — Shelby County CERT_Parcel (ArcGIS)
# ──────────────────────────────────────────────────────────────────────────────

# Expected field names on the CERT_Parcel MapServer layer.
# These are the standard Shelby County CAMA export field names.
# If the server renames them, attributes will just be None — no crash.
_PARCEL_FIELDS = ",".join([
    "PARCELID",
    "OWNERNAME",
    "PROPERTYADDRESS",
    "LANDVALUE",
    "IMPROVEMENTVALUE",
    "APPRAISEDVALUE",
    "ASSESSEDVALUE",
    "ACREAGE",
    "ZONING",
    "TAXDISTCODE",
    # Structural attributes (present in many county CAMA exports)
    "YEARBUILT",
    "SQFT",
    "BEDS",
    "BATHS",
    "STORIES",
])


@dataclass
class ParcelAttributes:
    """Typed parcel attributes returned from CERT_Parcel."""
    assessed_value: Optional[float] = None
    appraised_value: Optional[float] = None
    land_value: Optional[float] = None
    improvement_value: Optional[float] = None
    acreage: Optional[float] = None
    year_built: Optional[int] = None
    sqft: Optional[int] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[float] = None
    zoning: str = ""
    owner_name: str = ""


class ShelbyParcelEnricher:
    """
    Looks up property attributes from Shelby County CERT_Parcel (ArcGIS REST).

    Results are cached for the lifetime of the object (one scanner run) so
    repeated lookups of the same parcel only hit the network once.

    If the service is unreachable or the parcel is not found, returns an
    empty ParcelAttributes (all None). Never raises — failure is silent.
    """

    def __init__(self, session: requests.Session, timeout: int = 10):
        self._session = session
        self._timeout = timeout
        self._cache: dict[str, ParcelAttributes] = {}

    def enrich(self, parcel_id: str) -> ParcelAttributes:
        """Return parcel attributes for parcel_id, or empty ParcelAttributes on any error."""
        if not parcel_id:
            return ParcelAttributes()

        pid = parcel_id.strip()
        if pid in self._cache:
            return self._cache[pid]

        attrs = self._fetch(pid)
        self._cache[pid] = attrs
        return attrs

    def _fetch(self, parcel_id: str) -> ParcelAttributes:
        # Sanitize: parcel IDs should be alphanumeric + dash/space only
        safe_id = parcel_id.replace("'", "").replace(";", "")
        try:
            resp = self._session.get(
                CERT_PARCEL_ENDPOINT,
                params={
                    "where": f"PARCELID='{safe_id}'",
                    "outFields": _PARCEL_FIELDS,
                    "returnGeometry": "false",
                    "f": "json",
                    "resultRecordCount": 1,
                },
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            logger.debug("CERT_Parcel lookup failed for %s: %s", parcel_id, exc)
            return ParcelAttributes()

        features = data.get("features") or []
        if not features:
            logger.debug("CERT_Parcel: no feature found for parcel %s", parcel_id)
            return ParcelAttributes()

        attrs_raw: dict = features[0].get("attributes") or {}

        def _float(key: str) -> Optional[float]:
            v = attrs_raw.get(key)
            try:
                return float(v) if v not in (None, "", "null") else None
            except (TypeError, ValueError):
                return None

        def _int(key: str) -> Optional[int]:
            v = attrs_raw.get(key)
            try:
                return int(v) if v not in (None, "", "null") else None
            except (TypeError, ValueError):
                return None

        return ParcelAttributes(
            assessed_value=_float("ASSESSEDVALUE"),
            appraised_value=_float("APPRAISEDVALUE"),
            land_value=_float("LANDVALUE"),
            improvement_value=_float("IMPROVEMENTVALUE"),
            acreage=_float("ACREAGE"),
            year_built=_int("YEARBUILT"),
            sqft=_int("SQFT"),
            bedrooms=_int("BEDS"),
            bathrooms=_float("BATHS"),
            zoning=str(attrs_raw.get("ZONING") or ""),
            owner_name=str(attrs_raw.get("OWNERNAME") or ""),
        )


# ──────────────────────────────────────────────────────────────────────────────
# Source-level error
# ──────────────────────────────────────────────────────────────────────────────

class SourceUnavailableError(Exception):
    """Raised when the upstream API returns a non-recoverable error."""


# ──────────────────────────────────────────────────────────────────────────────
# Main adapter
# ──────────────────────────────────────────────────────────────────────────────

# Categories to request from Socrata — narrow the dataset to distress signals only.
# Passed as a SODA $where clause fragment.
_DEFAULT_CATEGORY_FILTER = " OR ".join([
    "upper(ce_category) like '%CONDEMN%'",
    "upper(ce_category) like '%BOARD%'",
    "upper(ce_category) like '%BLIGHT%'",
    "upper(ce_category) like '%GROUNDS%'",
    "upper(ce_category) like '%RESIDENTIAL%'",
    "upper(ce_category) like '%DEBRIS%'",
])

# Only fetch open cases (closed cases are historical, not actionable leads)
_STATUS_FILTER = "upper(request_status) = 'OPEN'"


class MemphisCodeEnforcementSource(PropertySource):
    """
    Fetches distressed property candidates from the Memphis Open Data
    Active Code Enforcement dataset (Socrata h4nu-tbge), optionally enriched
    with parcel attributes from Shelby County CERT_Parcel (ArcGIS REST).

    Parameters
    ----------
    app_token:
        Optional Socrata application token. Free to register at
        https://data.memphistn.gov/login. Without one, Socrata applies a
        shared throttle of ~1 req/s; with one, ~10 req/s.
    enrich_parcels:
        If True (default), attempt to pull property attributes (sqft, bedrooms,
        assessed value, year built) from Shelby County CERT_Parcel for each
        record that has a PARCEL_ID. Set False to skip enrichment entirely
        (faster, fewer network calls).
    request_timeout:
        Per-request timeout in seconds for both Socrata and ArcGIS calls.
    page_delay:
        Seconds to sleep between Socrata pages. Only relevant when
        fetch_candidates is called with a large batch_size. Default 0.2s
        is well within the throttle even without an app token.
    """

    DATA_SOURCE = "memphis_code_enforcement"

    def __init__(
        self,
        app_token: Optional[str] = None,
        enrich_parcels: bool = True,
        request_timeout: int = 15,
        page_delay: float = 0.2,
    ):
        self._app_token = app_token
        self._enrich = enrich_parcels
        self._timeout = request_timeout
        self._page_delay = page_delay

        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        if app_token:
            self._session.headers["X-App-Token"] = app_token

        self._enricher = ShelbyParcelEnricher(self._session, timeout=request_timeout)

    # ── Public interface ────────────────────────────────────────────────────

    def fetch_candidates(self, batch_size: int, offset: int = 0) -> list[CandidateProperty]:
        """
        Return up to batch_size CandidateProperty records starting at offset.

        Raises SourceUnavailableError if Socrata returns a non-2xx response.
        Individual record parse failures are logged and skipped — they never
        raise so a single bad row cannot abort the batch.
        """
        raw_rows = self._fetch_page(limit=batch_size, offset=offset)
        if self._page_delay > 0:
            time.sleep(self._page_delay)

        candidates: list[CandidateProperty] = []
        for row in raw_rows:
            prop = self._row_to_property(row)
            if prop is not None:
                candidates.append(prop)

        logger.info(
            "MemphisCodeEnforcementSource: fetched %d rows, parsed %d properties "
            "(offset=%d)",
            len(raw_rows), len(candidates), offset,
        )
        return candidates

    # ── Socrata fetch ────────────────────────────────────────────────────────

    def _fetch_page(self, limit: int, offset: int) -> list[dict]:
        """
        Fetch one page from the Socrata SODA API.

        Uses a $where clause to pre-filter to distress-relevant categories
        and open cases, reducing data transfer and Socrata compute.
        """
        where = f"({_DEFAULT_CATEGORY_FILTER}) AND {_STATUS_FILTER}"

        params: dict[str, str | int] = {
            "$where":  where,
            "$limit":  limit,
            "$offset": offset,
            "$order":  "creation_date DESC",
            # Request only the fields we actually use — smaller payloads
            "$select": ", ".join([
                _F_INCIDENT_NUMBER,
                _F_CE_CATEGORY,
                _F_REQUEST_TYPE,
                _F_REQUEST_STATUS,
                _F_FULL_ADDRESS,
                _F_ADDRESS1,
                _F_CITY,
                _F_STATE,
                _F_POSTAL_CODE,
                _F_OWNER_NAME,
                _F_PARCEL_ID,
                _F_CREATION_DATE,
                _F_MLGW_ON,
                _F_MLGW_STATUS,
                _F_LOCATION,
                "number_of_tasks",
                "next_open_task_date",
            ]),
        }

        try:
            resp = self._session.get(
                SOCRATA_ENDPOINT,
                params=params,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise SourceUnavailableError(
                f"Socrata request failed: {exc}"
            ) from exc

        if resp.status_code != 200:
            raise SourceUnavailableError(
                f"Socrata returned HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            return resp.json()
        except ValueError as exc:
            raise SourceUnavailableError(
                f"Socrata response is not valid JSON: {exc}"
            ) from exc

    # ── Row → CandidateProperty ──────────────────────────────────────────────

    def _row_to_property(self, row: dict) -> Optional[CandidateProperty]:
        """
        Map one Socrata row to a CandidateProperty.
        Returns None if the row is too malformed to be useful.
        """
        try:
            return self._parse_row(row)
        except Exception as exc:
            incident = row.get(_F_INCIDENT_NUMBER, "<unknown>")
            logger.debug("Skipping row %s: %s", incident, exc)
            return None

    def _parse_row(self, row: dict) -> Optional[CandidateProperty]:
        # ── Address ─────────────────────────────────────────────────
        street = (
            row.get(_F_FULL_ADDRESS)
            or row.get(_F_ADDRESS1)
            or ""
        ).strip().title()

        if not street:
            return None

        city  = (row.get(_F_CITY)  or "Memphis").strip().title()
        state = (row.get(_F_STATE) or "TN").strip().upper()
        zip_  = (row.get(_F_POSTAL_CODE) or "").strip()[:10]

        # Zip sometimes comes as "38109-1234" — keep full or just base 5
        if not zip_:
            zip_ = "00000"   # placeholder; VERIFY phase accepts it but ARV won't use it

        # ── Distress classification ──────────────────────────────────
        ce_category = row.get(_F_CE_CATEGORY) or row.get(_F_REQUEST_TYPE) or ""
        primary_type, base_score = _map_category(ce_category)
        mlgw_off = _mlgw_is_off(row)
        open_tasks = int(row.get("number_of_tasks") or 0)

        distress_types = _distress_types(primary_type, mlgw_off)
        distress_score = _distress_score(base_score, mlgw_off, open_tasks)

        # ── Location ─────────────────────────────────────────────────
        lat, lng = _parse_location(row)

        # ── Identifiers ──────────────────────────────────────────────
        parcel_id = (row.get(_F_PARCEL_ID) or "").strip()
        incident_number = (row.get(_F_INCIDENT_NUMBER) or "").strip()

        # ── Parcel enrichment ─────────────────────────────────────────
        parcel = (
            self._enricher.enrich(parcel_id)
            if (self._enrich and parcel_id)
            else ParcelAttributes()
        )

        # ── Owner ─────────────────────────────────────────────────────
        # Prefer parcel enricher's OWNERNAME (cleaner formatting) over CE record
        owner_name = parcel.owner_name or (row.get(_F_OWNER_NAME) or "").strip().title()

        # ── Assessed / estimated value ────────────────────────────────
        # Parcel enricher may return appraised value; fall back to None
        assessed  = parcel.assessed_value
        appraised = parcel.appraised_value  # use as estimated market value

        # ── Tax ───────────────────────────────────────────────────────
        # Shelby County effective property tax rate ≈ 2.7% of assessed value
        # (assessed = 25% of appraised in TN → effective rate on appraised ≈ 0.675%)
        tax_annual = (assessed * 0.027) if assessed else None

        # ── Lot size ─────────────────────────────────────────────────
        lot_sqft = int(parcel.acreage * 43_560) if parcel.acreage else None

        fp = _address_fingerprint(street, city, state)

        return CandidateProperty(
            street_address=street,
            city=city,
            state=state,
            zip=zip_,
            county="Shelby County",
            apn=parcel_id,
            latitude=lat,
            longitude=lng,
            property_type="sfr",          # code enforcement is overwhelmingly SFR
            bedrooms=parcel.bedrooms,
            bathrooms=parcel.bathrooms,
            sqft=parcel.sqft,
            lot_sqft=lot_sqft,
            year_built=parcel.year_built,
            assessed_value=assessed,
            estimated_value=appraised,
            tax_annual=round(tax_annual, 2) if tax_annual else None,
            owner_name=owner_name,
            distress_types=distress_types,
            distress_score=distress_score,
            data_source=self.DATA_SOURCE,
            source_record_id=incident_number or parcel_id,
            address_fingerprint=fp,
        )
