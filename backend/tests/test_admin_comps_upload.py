"""
Tests for POST /api/v1/admin/comps/upload — CSV comp ingestion endpoint.

Covers success, file-type validation, oversized-file rejection, malformed
rows (partial ingestion), and safe filename handling.
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.api import state as _state
from app.api.admin_comps import MAX_UPLOAD_BYTES
from app.scanner.comps.repository import InMemoryCompRepository
from app.scanner.config import config as scanner_config

ADMIN_TOKEN = "test-admin-token"
ADMIN_HEADERS = {"X-Admin-Token": ADMIN_TOKEN}


@pytest.fixture(autouse=True)
def clean_stores():
    _state.clear_stores()
    yield
    _state.clear_stores()


@pytest.fixture(autouse=True)
def admin_token_configured():
    """Every test in this file runs with a known admin token configured."""
    original = scanner_config.admin_api_token
    scanner_config.admin_api_token = ADMIN_TOKEN
    yield
    scanner_config.admin_api_token = original


@pytest.fixture
def client():
    from app.main import create_app
    app = create_app()
    return TestClient(app, headers=ADMIN_HEADERS)


def _csv_bytes(rows: list[str], header: str = "address,city,state,zip,latitude,longitude,sale_date,sale_price,sqft") -> bytes:
    lines = [header] + rows
    return ("\n".join(lines) + "\n").encode("utf-8")


VALID_ROW = "1 Oak St,Memphis,TN,38104,35.1495,-90.0490,2026-01-01,150000,1500"
VALID_ROW_2 = "2 Elm St,Memphis,TN,38104,35.1600,-90.0500,2026-02-01,160000,1600"


class TestUploadSuccess:
    def test_valid_csv_ingested(self, client):
        body = _csv_bytes([VALID_ROW, VALID_ROW_2])
        resp = client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps.csv", io.BytesIO(body), "text/csv")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["records_read"] == 2
        assert data["records_added"] == 2
        assert data["records_skipped"] == 0
        assert data["status"] == "success"
        assert data["error_count"] == 0

    def test_repository_actually_populated(self, client):
        body = _csv_bytes([VALID_ROW])
        client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps.csv", io.BytesIO(body), "text/csv")},
        )
        repo = _state.get_comp_repository()
        assert repo.count() == 1

    def test_response_includes_file_name(self, client):
        body = _csv_bytes([VALID_ROW])
        resp = client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("my_export.csv", io.BytesIO(body), "text/csv")},
        )
        assert resp.json()["file_name"] == "my_export.csv"

    def test_response_includes_bytes_received(self, client):
        body = _csv_bytes([VALID_ROW])
        resp = client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps.csv", io.BytesIO(body), "text/csv")},
        )
        assert resp.json()["bytes_received"] == len(body)

    def test_reuploading_same_file_dedupes(self, client):
        body = _csv_bytes([VALID_ROW])
        client.post("/api/v1/admin/comps/upload", files={"file": ("comps.csv", io.BytesIO(body), "text/csv")})
        resp2 = client.post("/api/v1/admin/comps/upload", files={"file": ("comps.csv", io.BytesIO(body), "text/csv")})
        data2 = resp2.json()
        assert data2["records_added"] == 0
        assert data2["records_skipped"] == 1
        repo = _state.get_comp_repository()
        assert repo.count() == 1


class TestFileTypeValidation:
    def test_non_csv_extension_rejected(self, client):
        resp = client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps.txt", io.BytesIO(b"address,city\n1 Oak St,Memphis\n"), "text/plain")},
        )
        assert resp.status_code == 400

    def test_no_extension_rejected(self, client):
        resp = client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps", io.BytesIO(b"data"), "application/octet-stream")},
        )
        assert resp.status_code == 400

    def test_uppercase_csv_extension_accepted(self, client):
        body = _csv_bytes([VALID_ROW])
        resp = client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("COMPS.CSV", io.BytesIO(body), "text/csv")},
        )
        assert resp.status_code == 200

    def test_double_extension_checks_final_suffix(self, client):
        resp = client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps.csv.exe", io.BytesIO(b"data"), "application/octet-stream")},
        )
        assert resp.status_code == 400


class TestOversizedFile:
    def test_file_over_limit_rejected(self, client):
        oversized = b"a" * (MAX_UPLOAD_BYTES + 1)
        resp = client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps.csv", io.BytesIO(oversized), "text/csv")},
        )
        assert resp.status_code == 413

    def test_file_at_limit_not_rejected_for_size(self, client):
        # Right at the boundary — content itself is garbage CSV, so ingestion
        # will report errors, but it must not be rejected for size.
        at_limit = b"address\n" + b"a" * (MAX_UPLOAD_BYTES - 8)
        resp = client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps.csv", io.BytesIO(at_limit), "text/csv")},
        )
        assert resp.status_code != 413

    def test_empty_file_rejected(self, client):
        resp = client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps.csv", io.BytesIO(b""), "text/csv")},
        )
        assert resp.status_code == 400


class TestMalformedRowsPartialIngestion:
    def test_missing_coordinates_skipped_others_ingested(self, client):
        rows = [
            VALID_ROW,
            "Bad Row,Memphis,TN,38104,,,2026-01-01,150000,1500",  # missing lat/lng
        ]
        resp = client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps.csv", io.BytesIO(_csv_bytes(rows)), "text/csv")},
        )
        data = resp.json()
        assert resp.status_code == 200
        assert data["records_read"] == 2
        assert data["records_added"] == 1
        assert data["records_skipped"] == 1
        assert data["status"] == "partial"
        assert data["error_count"] == 1

    def test_zero_price_row_skipped(self, client):
        rows = [
            VALID_ROW,
            "Free House,Memphis,TN,38104,35.20,-90.10,2026-01-01,0,1500",
        ]
        resp = client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps.csv", io.BytesIO(_csv_bytes(rows)), "text/csv")},
        )
        data = resp.json()
        assert data["records_added"] == 1
        assert data["records_skipped"] == 1

    def test_all_rows_malformed_reports_failed_status(self, client):
        rows = ["Bad Row 1,Memphis,TN,38104,,,2026-01-01,150000,1500"]
        resp = client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps.csv", io.BytesIO(_csv_bytes(rows)), "text/csv")},
        )
        data = resp.json()
        assert data["records_added"] == 0
        assert data["status"] == "failed"

    def test_errors_list_contains_row_details(self, client):
        rows = [
            "Bad Row,Memphis,TN,38104,,,2026-01-01,150000,1500",
        ]
        resp = client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps.csv", io.BytesIO(_csv_bytes(rows)), "text/csv")},
        )
        data = resp.json()
        assert len(data["errors"]) == 1
        assert "row" in data["errors"][0].lower()


class TestSafeFilenameHandling:
    def test_path_traversal_filename_sanitized(self, client):
        body = _csv_bytes([VALID_ROW])
        resp = client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("../../etc/passwd.csv", io.BytesIO(body), "text/csv")},
        )
        assert resp.status_code == 200
        # Only the base name is reported/used — no directory components survive.
        assert "/" not in resp.json()["file_name"]
        assert ".." not in resp.json()["file_name"]

    def test_no_filename_defaults_safely(self, client):
        body = _csv_bytes([VALID_ROW])
        resp = client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("", io.BytesIO(body), "text/csv")},
        )
        # Starlette/FastAPI requires *a* filename on the multipart part;
        # an empty one is treated as missing and fails validation upstream.
        assert resp.status_code in (400, 422)

    def test_upload_does_not_leave_temp_file_on_disk(self, client, tmp_path, monkeypatch):
        import tempfile as _tempfile

        created_paths = []
        real_mkstemp = _tempfile.mkstemp

        def _tracking_mkstemp(*args, **kwargs):
            fd, name = real_mkstemp(*args, **kwargs)
            created_paths.append(name)
            return fd, name

        monkeypatch.setattr(_tempfile, "mkstemp", _tracking_mkstemp)

        body = _csv_bytes([VALID_ROW])
        client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps.csv", io.BytesIO(body), "text/csv")},
        )
        assert len(created_paths) == 1
        import os
        assert not os.path.exists(created_paths[0])


class TestConfiguredRepositoryUsage:
    def test_upload_uses_currently_configured_repository(self, client):
        custom_repo = InMemoryCompRepository()
        _state.set_comp_repository(custom_repo)

        body = _csv_bytes([VALID_ROW])
        client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps.csv", io.BytesIO(body), "text/csv")},
        )
        assert custom_repo.count() == 1

    def test_ingestion_run_recorded_on_configured_repository(self, client):
        custom_repo = InMemoryCompRepository()
        _state.set_comp_repository(custom_repo)

        body = _csv_bytes([VALID_ROW])
        client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps.csv", io.BytesIO(body), "text/csv")},
        )
        assert len(custom_repo.ingestion_runs) == 1
        assert custom_repo.ingestion_runs[0]["source_name"] == "admin_upload"


class TestAdminAuth:
    """
    Uses a bare (no default headers) TestClient so each test controls the
    X-Admin-Token header explicitly.
    """

    @pytest.fixture
    def bare_client(self):
        from app.main import create_app
        app = create_app()
        return TestClient(app)

    def test_missing_token_returns_401(self, bare_client):
        body = _csv_bytes([VALID_ROW])
        resp = bare_client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps.csv", io.BytesIO(body), "text/csv")},
        )
        assert resp.status_code == 401

    def test_wrong_token_returns_403(self, bare_client):
        body = _csv_bytes([VALID_ROW])
        resp = bare_client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps.csv", io.BytesIO(body), "text/csv")},
            headers={"X-Admin-Token": "not-the-right-token"},
        )
        assert resp.status_code == 403

    def test_correct_token_succeeds(self, bare_client):
        body = _csv_bytes([VALID_ROW])
        resp = bare_client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps.csv", io.BytesIO(body), "text/csv")},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200

    def test_no_token_configured_rejects_everyone(self, bare_client):
        scanner_config.admin_api_token = ""
        body = _csv_bytes([VALID_ROW])
        resp = bare_client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps.csv", io.BytesIO(body), "text/csv")},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 403

    def test_upload_rejected_before_repository_is_touched(self, bare_client):
        """An unauthenticated request must never reach the ingester."""
        custom_repo = InMemoryCompRepository()
        _state.set_comp_repository(custom_repo)

        body = _csv_bytes([VALID_ROW])
        bare_client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps.csv", io.BytesIO(body), "text/csv")},
        )
        assert custom_repo.count() == 0


class TestErrorResponseCapAndScrubbing:
    def test_error_response_caps_stored_errors(self, client):
        header = "address,city,state,zip,latitude,longitude,sale_date,sale_price,sqft"
        bad_rows = [f"Bad Row {i},Memphis,TN,38104,,,2026-01-01,150000,1500" for i in range(150)]
        resp = client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps.csv", io.BytesIO(_csv_bytes(bad_rows, header=header)), "text/csv")},
        )
        data = resp.json()
        assert data["error_count"] == 150
        assert len(data["errors"]) == 101  # 100 stored + 1 summary line
        assert "50 more" in data["errors"][-1]

    def test_upsert_failure_does_not_leak_internals(self, client, monkeypatch):
        repo = _state.get_comp_repository()

        def _raise(*args, **kwargs):
            raise RuntimeError("password=hunter2 host=db-internal-01.prod")

        monkeypatch.setattr(repo, "upsert", _raise)

        body = _csv_bytes([VALID_ROW])
        resp = client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps.csv", io.BytesIO(body), "text/csv")},
        )
        data = resp.json()
        assert "hunter2" not in str(data)
        assert "db-internal-01" not in str(data)
        assert data["errors"][0] == "row 2: database error"


class TestFilenameControlCharacterStripping:
    def test_embedded_newline_stripped_from_response(self, client):
        body = _csv_bytes([VALID_ROW])
        resp = client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("evil\r\nX-Injected: true.csv", io.BytesIO(body), "text/csv")},
        )
        assert resp.status_code == 200
        assert "\n" not in resp.json()["file_name"]
        assert "\r" not in resp.json()["file_name"]

    def test_embedded_control_char_stripped(self, client):
        body = _csv_bytes([VALID_ROW])
        resp = client.post(
            "/api/v1/admin/comps/upload",
            files={"file": ("comps\x00.csv", io.BytesIO(body), "text/csv")},
        )
        assert resp.status_code == 200
        assert "\x00" not in resp.json()["file_name"]


class TestUploadPage:
    """
    The page itself is not token-gated (a plain GET has no way to attach a
    custom header) — only the POST /upload it calls via JS is.
    """

    @pytest.fixture
    def bare_client(self):
        from app.main import create_app
        app = create_app()
        return TestClient(app)

    def test_page_loads_without_token(self, bare_client):
        resp = bare_client.get("/api/v1/admin/comps/upload-page")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_page_contains_upload_form(self, bare_client):
        resp = bare_client.get("/api/v1/admin/comps/upload-page")
        html = resp.text
        assert '<form' in html
        assert 'type="file"' in html
        assert 'type="password"' in html
        assert "X-Admin-Token" in html

    def test_page_posts_to_upload_endpoint(self, bare_client):
        html = bare_client.get("/api/v1/admin/comps/upload-page").text
        assert "/api/v1/admin/comps/upload" in html

    def test_page_does_not_persist_token_client_side(self, bare_client):
        html = bare_client.get("/api/v1/admin/comps/upload-page").text
        assert "localStorage" not in html
        assert "sessionStorage" not in html
        assert "document.cookie" not in html

    def test_page_shows_result_fields(self, bare_client):
        html = bare_client.get("/api/v1/admin/comps/upload-page").text
        for field in ("records_read", "records_added", "records_skipped", "error_count", "errors"):
            assert field in html

    def test_page_escapes_error_text(self, bare_client):
        html = bare_client.get("/api/v1/admin/comps/upload-page").text
        assert "escapeHtml" in html
