"""
Admin endpoint for uploading comp CSV exports (MLS, PropStream, ATTOM, manual
spreadsheets) into the comp pool.

POST /api/v1/admin/comps/upload — multipart CSV upload, ingested via
CsvCompIngester against the process-wide CompRepository (app.api.state).
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.api import state as store
from app.scanner.comps.ingestion import CsvCompIngester

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/admin/comps", tags=["admin"])

# ── Upload limits ──────────────────────────────────────────────────────────────

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
_ALLOWED_EXTENSION = ".csv"
_CHUNK_SIZE = 1024 * 1024  # 1 MB, read incrementally so we never buffer an
                           # oversized file fully in memory before rejecting it


def _safe_original_name(filename: str | None) -> str:
    """
    Returns just the base name for display/audit purposes — never used to
    construct a filesystem path (the actual temp file uses a random name).
    Strips any directory components a malicious or malformed filename might
    carry (e.g. "../../etc/passwd").
    """
    return Path(filename or "upload.csv").name


@router.post("/upload")
async def upload_comps_csv(file: UploadFile = File(...)):
    """
    Accept a CSV of comparable sales, validate it, and ingest it into the
    configured comp repository. Returns an IngestResult summary.
    """
    original_name = _safe_original_name(file.filename)

    if Path(original_name).suffix.lower() != _ALLOWED_EXTENSION:
        raise HTTPException(
            status_code=400,
            detail=f"Only {_ALLOWED_EXTENSION} files are accepted (got '{original_name}').",
        )

    tmp_path: Path | None = None
    try:
        # Write to a randomly-named temp file — the uploaded filename is
        # never used to build a filesystem path.
        fd, tmp_name = tempfile.mkstemp(suffix=".csv", prefix="comp_upload_")
        tmp_path = Path(tmp_name)

        total_bytes = 0
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await file.read(_CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB "
                            f"upload limit."
                        ),
                    )
                out.write(chunk)

        if total_bytes == 0:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")

        repo = store.get_comp_repository()
        ingester = CsvCompIngester(repo, source_name="admin_upload")
        result = ingester.ingest_file(tmp_path)

        logger.info(
            "Admin comp upload: %s | read=%d added=%d skipped=%d errors=%d status=%s",
            original_name, result.records_read, result.records_added,
            result.records_skipped, len(result.errors), result.status,
        )

        return {
            "file_name": original_name,
            "bytes_received": total_bytes,
            "records_read": result.records_read,
            "records_added": result.records_added,
            "records_skipped": result.records_skipped,
            "error_count": len(result.errors),
            "errors": result.errors,
            "status": result.status,
        }
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()
        await file.close()
