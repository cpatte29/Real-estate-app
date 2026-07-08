"""
Admin endpoints for uploading comp CSV exports (MLS, PropStream, ATTOM,
manual spreadsheets) into the comp pool.

GET  /api/v1/admin/comps/upload-page — minimal HTML upload form. Not itself
     token-gated (a bare page load has no way to attach a custom header) —
     the admin types their token into a field on the page, and the page's
     JS sends it as X-Admin-Token on the actual upload request below.
POST /api/v1/admin/comps/upload — multipart CSV upload, ingested via
     CsvCompIngester against the process-wide CompRepository
     (app.api.state). Requires the X-Admin-Token header (see
     verify_admin_token) — this is the only endpoint auth actually gates.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

from app.api import state as store
from app.scanner.comps.ingestion import CsvCompIngester
from app.scanner.config import config

logger = logging.getLogger(__name__)

# ── Upload limits ──────────────────────────────────────────────────────────────

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
_ALLOWED_EXTENSION = ".csv"
_CHUNK_SIZE = 1024 * 1024  # 1 MB, read incrementally so we never buffer an
                           # oversized file fully in memory before rejecting it

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


# ── Auth ─────────────────────────────────────────────────────────────────────

def verify_admin_token(x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token")):
    """
    Require a valid X-Admin-Token header on every admin route.

    Fails closed: if no admin_api_token is configured (the default), every
    request is rejected with 403 rather than silently allowing access.
    """
    expected = config.admin_api_token
    if not expected:
        raise HTTPException(status_code=403, detail="Admin API is not configured")
    if x_admin_token is None:
        raise HTTPException(status_code=401, detail="X-Admin-Token header is required")
    if x_admin_token != expected:
        raise HTTPException(status_code=403, detail="Invalid admin API token")


router = APIRouter(prefix="/api/v1/admin/comps", tags=["admin"])


def _safe_original_name(filename: str | None) -> str:
    """
    Returns a base name safe for display, logging, and audit purposes —
    never used to construct a filesystem path (the actual temp file uses a
    random name). Strips directory components a malicious or malformed
    filename might carry (e.g. "../../etc/passwd") and strips control
    characters (e.g. embedded newlines) that could otherwise forge log
    lines when this value is written to the log.
    """
    base = Path(filename or "upload.csv").name
    return _CONTROL_CHARS_RE.sub("", base)


@router.post("/upload", dependencies=[Depends(verify_admin_token)])
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
            result.records_skipped, result.error_count, result.status,
        )

        return {
            "file_name": original_name,
            "bytes_received": total_bytes,
            "records_read": result.records_read,
            "records_added": result.records_added,
            "records_skipped": result.records_skipped,
            "error_count": result.error_count,
            "errors": result.display_errors,
            "status": result.status,
        }
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()
        await file.close()


@router.get("/upload-page", response_class=HTMLResponse)
async def upload_page():
    """
    Minimal admin form for uploading a sold-comps CSV.

    The token field is a plain in-page input — nothing is written to
    localStorage, sessionStorage, or a cookie. Reloading this page always
    starts with an empty token field.
    """
    return HTMLResponse(content=_UPLOAD_PAGE_HTML)


_UPLOAD_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Comp Upload — REI Scanner Admin</title>
<style>
  :root {
    --bg: #0f172a; --surface: #1e293b; --border: #334155;
    --text: #e2e8f0; --muted: #94a3b8; --accent: #3b82f6;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f8fafc; --surface:#fff; --border:#e2e8f0;
            --text:#0f172a; --muted:#64748b; }
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; background: var(--bg);
         color: var(--text); min-height: 100vh; }
  .wrap { max-width: 560px; margin: 0 auto; padding: 2rem 1.5rem; }
  h1 { font-size: 1.25rem; font-weight: 700; margin-bottom: .25rem; }
  .subtitle { color: var(--muted); font-size: .85rem; margin-bottom: 1.5rem; }
  .card { background: var(--surface); border: 1px solid var(--border);
          border-radius: .75rem; padding: 1.5rem; }
  label { display: block; font-size: .8rem; font-weight: 600;
          margin-bottom: .35rem; margin-top: 1rem; }
  label:first-of-type { margin-top: 0; }
  input[type="password"], input[type="file"] {
    width: 100%; background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: .4rem;
    padding: .55rem .7rem; font-size: .85rem; font-family: inherit;
  }
  .hint { font-size: .72rem; color: var(--muted); margin-top: .3rem; }
  button { margin-top: 1.25rem; background: var(--accent); color: #fff;
           border: none; border-radius: .4rem; padding: .6rem 1.2rem;
           font-size: .85rem; font-weight: 600; cursor: pointer; width: 100%; }
  button:hover { opacity: .9; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  #result { margin-top: 1.5rem; }
  .summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(100px, 1fr));
             gap: .75rem; padding: 1rem; background: rgba(0,0,0,.2);
             border-radius: .5rem; margin-bottom: 1rem; }
  .stat-val { font-size: 1.1rem; font-weight: 700; }
  .stat-lbl { font-size: .68rem; color: var(--muted); text-transform: uppercase; }
  .stat-val.error { color: #ef4444; }
  .status-line { font-size: .85rem; font-weight: 600; margin-bottom: .75rem; }
  .status-line.success { color: #22c55e; }
  .status-line.partial { color: #eab308; }
  .status-line.failed  { color: #ef4444; }
  .error-list { list-style: none; font-size: .78rem; color: var(--muted);
                background: var(--bg); border: 1px solid var(--border);
                border-radius: .4rem; padding: .6rem .8rem; max-height: 220px;
                overflow-y: auto; }
  .error-list li { padding: .15rem 0; }
  .banner { padding: .7rem 1rem; border-radius: .4rem; font-size: .85rem;
            margin-bottom: 1rem; }
  .banner.error { background: rgba(239,68,68,.15); color: #ef4444; }
</style>
</head>
<body>
<div class="wrap">
  <h1>📤 Upload Sold Comps</h1>
  <p class="subtitle">Ingest a CSV of comparable sales into the comp pool.</p>

  <div class="card">
    <form id="upload-form">
      <label for="token">Admin token</label>
      <input type="password" id="token" name="token" autocomplete="off"
             placeholder="X-Admin-Token" required>
      <p class="hint">Entered per-session only — never stored in the browser.</p>

      <label for="file">CSV file</label>
      <input type="file" id="file" name="file" accept=".csv" required>
      <p class="hint">Max 10 MB. Header row required.</p>

      <button type="submit" id="submit-btn">Upload</button>
    </form>

    <div id="result"></div>
  </div>
</div>

<script>
const form = document.getElementById('upload-form');
const resultEl = document.getElementById('result');
const submitBtn = document.getElementById('submit-btn');

form.addEventListener('submit', async (evt) => {
  evt.preventDefault();

  const token = document.getElementById('token').value;
  const fileInput = document.getElementById('file');
  const file = fileInput.files[0];
  if (!file) return;

  const formData = new FormData();
  formData.append('file', file);

  submitBtn.disabled = true;
  submitBtn.textContent = 'Uploading…';
  resultEl.innerHTML = '';

  try {
    const resp = await fetch('/api/v1/admin/comps/upload', {
      method: 'POST',
      headers: { 'X-Admin-Token': token },
      body: formData,
    });

    if (!resp.ok) {
      let detail = resp.statusText;
      try {
        const body = await resp.json();
        detail = body.detail || detail;
      } catch (e) { /* non-JSON error body */ }
      resultEl.innerHTML = `<div class="banner error">Upload failed (${resp.status}): ${escapeHtml(detail)}</div>`;
      return;
    }

    const data = await resp.json();
    renderResult(data);
  } catch (e) {
    resultEl.innerHTML = `<div class="banner error">Request failed: ${escapeHtml(e.message)}</div>`;
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Upload';
  }
});

function renderResult(data) {
  const statusClass = data.status === 'success' ? 'success'
                     : data.status === 'partial' ? 'partial' : 'failed';

  let html = `<div class="status-line ${statusClass}">Status: ${escapeHtml(data.status)}</div>`;
  html += '<div class="summary">';
  html += statTile(data.records_read, 'Read');
  html += statTile(data.records_added, 'Added');
  html += statTile(data.records_skipped, 'Skipped');
  html += statTile(data.error_count, 'Errors', data.error_count > 0);
  html += '</div>';

  if (data.errors && data.errors.length > 0) {
    html += '<ul class="error-list">';
    for (const err of data.errors) {
      html += `<li>${escapeHtml(err)}</li>`;
    }
    html += '</ul>';
  }

  resultEl.innerHTML = html;
}

function statTile(value, label, isError) {
  const cls = isError ? 'stat-val error' : 'stat-val';
  return `<div><div class="${cls}">${value}</div><div class="stat-lbl">${label}</div></div>`;
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = String(str);
  return div.innerHTML;
}
</script>
</body>
</html>"""
