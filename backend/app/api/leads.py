"""
Lead review / investor CRM endpoints.

GET  /api/v1/leads                — paginated list with status/score/run filters
GET  /api/v1/leads/stats          — counts by review status
GET  /api/v1/leads/dashboard      — HTML investor CRM dashboard
GET  /api/v1/leads/{lead_id}      — single lead detail
PATCH /api/v1/leads/{lead_id}/review — update status, notes, reviewer
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.api import state as store
from app.models.lead_review import (
    REVIEW_STATUS_COLORS,
    REVIEW_STATUS_LABELS,
    LeadRecord,
    LeadReviewStatus,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/leads", tags=["leads"])


# ──────────────────────────────────────────────────────────────────────────────
# Pydantic schemas
# ──────────────────────────────────────────────────────────────────────────────

class ReviewPatch(BaseModel):
    review_status: Optional[LeadReviewStatus] = None
    human_notes:   Optional[str] = None
    reviewed_by:   Optional[str] = None

    model_config = {"use_enum_values": False}


# ──────────────────────────────────────────────────────────────────────────────
# Serialisation helpers
# ──────────────────────────────────────────────────────────────────────────────

def _review_to_dict(record: LeadRecord) -> dict:
    rv = record.review
    return {
        "review_status": rv.review_status.value,
        "human_notes":   rv.human_notes,
        "reviewed_at":   rv.reviewed_at.isoformat() if rv.reviewed_at else None,
        "reviewed_by":   rv.reviewed_by,
    }


def _record_to_dict(record: LeadRecord, include_comps: bool = False) -> dict:
    result = record.result
    prop   = result.property
    deal   = result.deal_analysis
    arv    = result.arv_result
    rehab  = result.rehab_estimate

    d = {
        "lead_id":  record.lead_id,
        "run_id":   record.run_id,
        "run_date": record.run_date,
        "created_at": record.created_at.isoformat(),
        "review":   _review_to_dict(record),

        "property": {
            "address": {
                "street": prop.street_address,
                "city":   prop.city,
                "state":  prop.state,
                "zip":    prop.zip,
                "county": getattr(prop, "county", None),
            },
            "type":       getattr(prop, "property_type", None),
            "bedrooms":   prop.bedrooms,
            "bathrooms":  prop.bathrooms,
            "sqft":       prop.sqft,
            "year_built": prop.year_built,
            "latitude":   getattr(prop, "latitude", None),
            "longitude":  getattr(prop, "longitude", None),
            "distress_types":  [
                (dt.value if hasattr(dt, "value") else dt)
                for dt in getattr(prop, "distress_types", [])
            ],
            "distress_score":  getattr(prop, "distress_score", None),
            "data_source":     getattr(prop, "data_source", None),
            "owner_name":      getattr(prop, "owner_name", None),
        },

        "deal": {
            "opportunity_score":   getattr(deal, "opportunity_score", None),
            "arv":                 getattr(deal, "arv", None),
            "rehab_cost":          getattr(deal, "rehab_cost", None),
            "max_allowable_offer": getattr(deal, "max_allowable_offer", None),
            "estimated_profit":    getattr(deal, "estimated_profit", None),
            "roi_pct":             getattr(deal, "roi_pct", None),
            "list_price":          getattr(deal, "list_price", None),
        } if deal else None,

        "arv": {
            "confidence":       (arv.confidence.value if hasattr(arv.confidence, "value") else arv.confidence) if arv else None,
            "confidence_score": arv.confidence_score if arv else None,
            "comp_count":       arv.comp_count if arv else None,
            "price_per_sqft_avg": getattr(arv, "price_per_sqft_avg", None) if arv else None,
        } if arv else None,

        "rehab": {
            "level":      (rehab.rehab_level.value if hasattr(rehab.rehab_level, "value") else rehab.rehab_level) if rehab else None,
            "total_cost": rehab.total_cost if rehab else None,
            "cost_per_sqft": rehab.cost_per_sqft if rehab else None,
        } if rehab else None,
    }

    if include_comps and arv:
        d["comps"] = [
            {
                "address":         c.address,
                "city":            c.city,
                "state":           c.state,
                "sale_date":       c.sale_date.isoformat(),
                "sale_price":      c.sale_price,
                "sqft":            c.sqft,
                "adjusted_price":  c.adjusted_price,
                "similarity_score": c.similarity_score,
                "distance_miles":  c.distance_miles,
            }
            for c in arv.comps
        ]

    return d


# ──────────────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def lead_stats():
    """Count of leads grouped by review status, plus total."""
    all_leads = store.get_all_leads()
    counts: dict[str, int] = {s.value: 0 for s in LeadReviewStatus}
    for record in all_leads:
        counts[record.review.review_status.value] += 1
    return {"total": len(all_leads), "by_status": counts}


@router.get("/dashboard", response_class=HTMLResponse)
async def lead_dashboard(
    status: Optional[str] = Query(None, description="Filter by review status"),
):
    """Interactive HTML investor CRM dashboard."""
    all_leads = store.get_all_leads()

    # Sort by opportunity score desc
    all_leads.sort(
        key=lambda r: (r.result.deal_analysis.opportunity_score
                       if r.result.deal_analysis else 0),
        reverse=True,
    )

    # Status counts for the filter tabs
    counts: dict[str, int] = {s.value: 0 for s in LeadReviewStatus}
    for record in all_leads:
        counts[record.review.review_status.value] += 1

    # Apply status filter
    if status:
        try:
            flt = LeadReviewStatus(status)
            visible = [r for r in all_leads if r.review.review_status == flt]
        except ValueError:
            visible = all_leads
    else:
        visible = all_leads

    html = _build_dashboard_html(all_leads, visible, counts, status)
    return HTMLResponse(content=html)


@router.get("")
async def list_leads(
    status:    Optional[str]  = Query(None),
    run_id:    Optional[str]  = Query(None),
    min_score: float          = Query(0.0, ge=0.0, le=100.0),
    limit:     int            = Query(25, ge=1, le=200),
    offset:    int            = Query(0, ge=0),
):
    """
    Paginated list of accepted leads.

    Filters (all optional):
      status    — one of: new, reviewed, rejected_by_human, contacted,
                  under_contract, closed
      run_id    — restrict to a specific scanner run
      min_score — minimum opportunity score (0-100)
    """
    all_leads = store.get_all_leads()

    # Validate status filter
    status_filter: Optional[LeadReviewStatus] = None
    if status:
        try:
            status_filter = LeadReviewStatus(status)
        except ValueError:
            valid = [s.value for s in LeadReviewStatus]
            raise HTTPException(
                status_code=422,
                detail=f"Invalid status '{status}'. Valid values: {valid}",
            )

    filtered = []
    for record in all_leads:
        if status_filter and record.review.review_status != status_filter:
            continue
        if run_id and record.run_id != run_id:
            continue
        deal = record.result.deal_analysis
        score = deal.opportunity_score if deal else 0.0
        if score < min_score:
            continue
        filtered.append(record)

    # Sort by opportunity score desc
    filtered.sort(
        key=lambda r: (r.result.deal_analysis.opportunity_score
                       if r.result.deal_analysis else 0),
        reverse=True,
    )

    page = filtered[offset : offset + limit]

    return {
        "total":  len(filtered),
        "offset": offset,
        "limit":  limit,
        "leads":  [_record_to_dict(r) for r in page],
    }


@router.get("/{lead_id}")
async def get_lead(lead_id: str):
    """Full detail for a single lead, including comps."""
    record = store.get_lead(lead_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Lead '{lead_id}' not found")
    return _record_to_dict(record, include_comps=True)


@router.patch("/{lead_id}/review")
async def update_review(lead_id: str, patch: ReviewPatch):
    """
    Update the review state of a lead.

    All fields are optional — send only what you want to change.
    ``reviewed_at`` is set automatically to the current UTC time whenever
    any field in the body changes.
    """
    if patch.review_status is None and patch.human_notes is None and patch.reviewed_by is None:
        raise HTTPException(status_code=422, detail="Request body must contain at least one field.")

    record = store.update_lead_review(
        lead_id=lead_id,
        review_status=patch.review_status,
        human_notes=patch.human_notes,
        reviewed_by=patch.reviewed_by,
    )
    if record is None:
        raise HTTPException(status_code=404, detail=f"Lead '{lead_id}' not found")

    return {
        "lead_id": lead_id,
        "review":  _review_to_dict(record),
    }


# ──────────────────────────────────────────────────────────────────────────────
# HTML dashboard builder
# ──────────────────────────────────────────────────────────────────────────────

def _score_color(score: float) -> str:
    if score >= 75:
        return "#22c55e"
    if score >= 55:
        return "#eab308"
    return "#ef4444"


def _build_dashboard_html(
    all_leads: list[LeadRecord],
    visible: list[LeadRecord],
    counts: dict[str, int],
    active_status: Optional[str],
) -> str:
    status_options = "".join(
        f'<option value="{s.value}">{REVIEW_STATUS_LABELS[s]}</option>'
        for s in LeadReviewStatus
    )

    tab_items = '<li><a href="/api/v1/leads/dashboard" '
    tab_items += 'class="tab' + ('' if active_status else ' active') + '">'
    tab_items += f'All <span class="badge">{len(all_leads)}</span></a></li>\n'
    for s in LeadReviewStatus:
        active_cls = " active" if active_status == s.value else ""
        color = REVIEW_STATUS_COLORS[s]
        tab_items += (
            f'<li><a href="/api/v1/leads/dashboard?status={s.value}" '
            f'class="tab{active_cls}" style="--badge-bg:{color}">'
            f'{REVIEW_STATUS_LABELS[s]} '
            f'<span class="badge">{counts[s.value]}</span></a></li>\n'
        )

    cards = ""
    for record in visible:
        prop  = record.result.property
        deal  = record.result.deal_analysis
        arv_r = record.result.arv_result
        rehab = record.result.rehab_estimate
        rev   = record.review
        score = deal.opportunity_score if deal else 0
        sc    = _score_color(score)
        rv_color = REVIEW_STATUS_COLORS[rev.review_status]
        rv_label = REVIEW_STATUS_LABELS[rev.review_status]

        profit_str  = f"${deal.estimated_profit:,.0f}"  if deal else "—"
        arv_str     = f"${deal.arv:,.0f}"               if deal else "—"
        mao_str     = f"${deal.max_allowable_offer:,.0f}" if deal else "—"
        if rehab:
            rl = rehab.rehab_level.value if hasattr(rehab.rehab_level, "value") else rehab.rehab_level
            rehab_str = f"${rehab.total_cost:,.0f} ({rl})"
        else:
            rehab_str = "—"
        if arv_r:
            conf = arv_r.confidence.value if hasattr(arv_r.confidence, "value") else arv_r.confidence
            comps_str = f"{arv_r.comp_count} comps ({conf})"
        else:
            comps_str = "—"
        distress    = ", ".join(
            (d.value if hasattr(d, "value") else d).replace("_", " ").title()
            for d in getattr(prop, "distress_types", [])
        )
        notes_esc   = rev.human_notes.replace('"', '&quot;').replace('<', '&lt;')
        reviewer_esc= rev.reviewed_by.replace('"', '&quot;').replace('<', '&lt;')
        lid         = record.lead_id

        cards += f"""
<div class="card" id="card-{lid}">
  <div class="card-header">
    <div>
      <div class="address">{prop.street_address}</div>
      <div class="sub-address">{prop.city}, {prop.state} {prop.zip}</div>
      <div class="tags">
        <span class="tag">{distress or 'No distress tags'}</span>
        <span class="tag">{(getattr(prop,'property_type',None) or 'sfr').upper()} · {prop.bedrooms or '?'}bd/{prop.bathrooms or '?'}ba · {prop.sqft or '?'} sqft</span>
      </div>
    </div>
    <div class="score-badge" style="background:{sc}">
      <div class="score-val">{score:.0f}</div>
      <div class="score-lbl">score</div>
    </div>
  </div>

  <div class="metrics">
    <div class="metric"><div class="metric-val">{arv_str}</div><div class="metric-lbl">ARV</div></div>
    <div class="metric"><div class="metric-val">{mao_str}</div><div class="metric-lbl">MAO</div></div>
    <div class="metric"><div class="metric-val">{profit_str}</div><div class="metric-lbl">Est. Profit</div></div>
    <div class="metric"><div class="metric-val">{rehab_str}</div><div class="metric-lbl">Rehab</div></div>
    <div class="metric"><div class="metric-val">{comps_str}</div><div class="metric-lbl">Comps</div></div>
  </div>

  <div class="review-section">
    <div class="review-row">
      <span class="status-badge" id="status-badge-{lid}"
            style="background:{rv_color}">{rv_label}</span>
      <select class="status-select" data-lead="{lid}" onchange="updateStatus(this)">
        <option value="">Change status…</option>
        {status_options}
      </select>
    </div>
    <textarea class="notes" id="notes-{lid}" placeholder="Add notes…"
              rows="2">{notes_esc}</textarea>
    <div class="review-row">
      <input class="reviewer-input" id="reviewer-{lid}" type="text"
             placeholder="Your name / email" value="{reviewer_esc}">
      <button class="save-btn" onclick="saveNotes('{lid}')">Save notes</button>
    </div>
    <div class="save-status" id="save-status-{lid}"></div>
  </div>
</div>
"""

    empty_msg = "" if visible else '<p class="empty">No leads match the current filter.</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lead Review Dashboard — REI Scanner</title>
<style>
  :root {{
    --bg: #0f172a; --surface: #1e293b; --border: #334155;
    --text: #e2e8f0; --muted: #94a3b8; --accent: #3b82f6;
  }}
  @media (prefers-color-scheme: light) {{
    :root {{ --bg:#f8fafc; --surface:#fff; --border:#e2e8f0;
             --text:#0f172a; --muted:#64748b; }}
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: system-ui, sans-serif; background: var(--bg);
          color: var(--text); min-height: 100vh; }}
  a {{ color: inherit; text-decoration: none; }}

  /* ── Header ── */
  .header {{ background: var(--surface); border-bottom: 1px solid var(--border);
             padding: 1rem 1.5rem; display: flex; align-items: center;
             justify-content: space-between; }}
  .header h1 {{ font-size: 1.25rem; font-weight: 700; }}
  .header .total {{ color: var(--muted); font-size: .85rem; }}

  /* ── Tabs ── */
  .tabs {{ background: var(--surface); border-bottom: 1px solid var(--border);
           padding: 0 1.5rem; }}
  .tabs ul {{ display: flex; gap: .25rem; list-style: none; overflow-x: auto; }}
  .tab {{ display: inline-flex; align-items: center; gap: .35rem;
          padding: .65rem .9rem; font-size: .82rem; border-radius: .5rem .5rem 0 0;
          white-space: nowrap; color: var(--muted); transition: background .15s; }}
  .tab:hover {{ background: rgba(255,255,255,.05); color: var(--text); }}
  .tab.active {{ background: var(--bg); color: var(--text); font-weight: 600; }}
  .badge {{ background: var(--badge-bg, #4b5563); color: #fff; border-radius: 999px;
            padding: .1rem .45rem; font-size: .72rem; font-weight: 700; }}

  /* ── Body ── */
  .body {{ max-width: 900px; margin: 0 auto; padding: 1.5rem; }}

  /* ── Cards ── */
  .card {{ background: var(--surface); border: 1px solid var(--border);
           border-radius: .75rem; padding: 1.25rem; margin-bottom: 1rem; }}
  .card-header {{ display: flex; justify-content: space-between;
                  align-items: flex-start; gap: 1rem; }}
  .address {{ font-size: 1rem; font-weight: 700; }}
  .sub-address {{ color: var(--muted); font-size: .82rem; margin-top: .15rem; }}
  .tags {{ display: flex; flex-wrap: wrap; gap: .4rem; margin-top: .5rem; }}
  .tag {{ background: rgba(99,102,241,.15); color: #818cf8; border-radius: 999px;
          padding: .15rem .65rem; font-size: .72rem; }}
  .score-badge {{ border-radius: .5rem; width: 64px; text-align: center;
                  padding: .5rem; flex-shrink: 0; }}
  .score-val {{ font-size: 1.6rem; font-weight: 800; color: #fff; line-height: 1; }}
  .score-lbl {{ font-size: .65rem; color: rgba(255,255,255,.8); text-transform: uppercase; }}

  /* ── Metrics ── */
  .metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(100px, 1fr));
              gap: .75rem; margin: 1rem 0; padding: 1rem;
              background: rgba(0,0,0,.2); border-radius: .5rem; }}
  .metric-val {{ font-size: .95rem; font-weight: 700; }}
  .metric-lbl {{ font-size: .7rem; color: var(--muted); text-transform: uppercase;
                 margin-top: .1rem; }}

  /* ── Review section ── */
  .review-section {{ display: flex; flex-direction: column; gap: .6rem;
                     border-top: 1px solid var(--border); padding-top: .9rem; margin-top: .5rem; }}
  .review-row {{ display: flex; align-items: center; gap: .6rem; flex-wrap: wrap; }}
  .status-badge {{ border-radius: 999px; padding: .25rem .75rem;
                   font-size: .75rem; font-weight: 700; color: #fff; white-space: nowrap; }}
  .status-select {{ background: var(--bg); color: var(--text); border: 1px solid var(--border);
                    border-radius: .4rem; padding: .3rem .6rem; font-size: .8rem; cursor: pointer; }}
  .notes {{ width: 100%; background: var(--bg); color: var(--text);
            border: 1px solid var(--border); border-radius: .4rem;
            padding: .5rem; font-size: .82rem; resize: vertical; font-family: inherit; }}
  .reviewer-input {{ flex: 1; min-width: 160px; background: var(--bg); color: var(--text);
                     border: 1px solid var(--border); border-radius: .4rem;
                     padding: .3rem .6rem; font-size: .8rem; }}
  .save-btn {{ background: var(--accent); color: #fff; border: none; border-radius: .4rem;
               padding: .35rem .9rem; font-size: .8rem; cursor: pointer;
               white-space: nowrap; }}
  .save-btn:hover {{ opacity: .85; }}
  .save-status {{ font-size: .75rem; color: var(--muted); min-height: 1.2em; }}
  .empty {{ text-align: center; color: var(--muted); padding: 3rem; }}
</style>
</head>
<body>

<div class="header">
  <h1>🏠 Lead Review Dashboard</h1>
  <span class="total">{len(all_leads)} total accepted leads</span>
</div>

<nav class="tabs">
  <ul>{tab_items}</ul>
</nav>

<div class="body">
  {empty_msg}
  {cards}
</div>

<script>
const API = '/api/v1/leads';
const STATUS_LABELS = {{}};
const STATUS_COLORS = {{}};
</script>
<script>
// Inject status metadata from server
const _labels = {dict([(s.value, REVIEW_STATUS_LABELS[s]) for s in LeadReviewStatus])};
const _colors  = {dict([(s.value, REVIEW_STATUS_COLORS[s]) for s in LeadReviewStatus])};
Object.assign(STATUS_LABELS, _labels);
Object.assign(STATUS_COLORS, _colors);

async function patchReview(leadId, body) {{
  const resp = await fetch(`${{API}}/${{leadId}}/review`, {{
    method: 'PATCH',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(body),
  }});
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}}

async function updateStatus(sel) {{
  const leadId = sel.dataset.lead;
  const newStatus = sel.value;
  if (!newStatus) return;
  const statusEl = document.getElementById(`status-badge-${{leadId}}`);
  const saveEl   = document.getElementById(`save-status-${{leadId}}`);
  try {{
    await patchReview(leadId, {{review_status: newStatus}});
    statusEl.textContent = STATUS_LABELS[newStatus] || newStatus;
    statusEl.style.background = STATUS_COLORS[newStatus] || '#6b7280';
    sel.value = '';
    saveEl.textContent = '✓ Status updated';
    setTimeout(() => {{ saveEl.textContent = ''; }}, 2500);
  }} catch(e) {{
    saveEl.textContent = '✗ ' + e.message;
  }}
}}

async function saveNotes(leadId) {{
  const notes    = document.getElementById(`notes-${{leadId}}`).value;
  const reviewer = document.getElementById(`reviewer-${{leadId}}`).value;
  const saveEl   = document.getElementById(`save-status-${{leadId}}`);
  try {{
    await patchReview(leadId, {{human_notes: notes, reviewed_by: reviewer}});
    saveEl.textContent = '✓ Saved';
    setTimeout(() => {{ saveEl.textContent = ''; }}, 2500);
  }} catch(e) {{
    saveEl.textContent = '✗ ' + e.message;
  }}
}}
</script>
</body>
</html>"""
