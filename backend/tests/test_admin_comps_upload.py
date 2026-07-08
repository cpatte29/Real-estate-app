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


@pytest.fixture(autouse=True)
def clean_stores():
    _state.clear_stores()
    yield
    _state.clear_stores()


@pytest.fixture
def client():
    from app.main import create_app
    app = create_app()
    return TestClient(app)


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
