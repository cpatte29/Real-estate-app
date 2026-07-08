"""
Daily investor report generator.

Produces both structured JSON and an HTML report from a completed scanner run.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.scanner.config import config
from app.scanner.models import (
    ARVConfidence,
    PropertyPipelineResult,
    RejectionReason,
    ScannerRunState,
)


# ──────────────────────────────────────────────────────────────────────────────
# JSON report — machine-readable summary
# ──────────────────────────────────────────────────────────────────────────────

def _lead_to_dict(result: PropertyPipelineResult) -> dict:
    prop = result.property
    deal = result.deal_analysis
    arv = result.arv_result
    rehab = result.rehab_estimate

    return {
        "property_id": str(prop.id),
        "address": {
            "street": prop.street_address,
            "city": prop.city,
            "state": prop.state,
            "zip": prop.zip,
            "county": prop.county,
        },
        "property_details": {
            "type": prop.property_type,
            "bedrooms": prop.bedrooms,
            "bathrooms": prop.bathrooms,
            "sqft": prop.sqft,
            "year_built": prop.year_built,
        },
        "distress": {
            "types": [d.value for d in prop.distress_types],
            "score": prop.distress_score,
        },
        "arv": {
            "value": arv.arv if arv else None,
            "confidence": arv.confidence.value if arv else None,
            "confidence_score": arv.confidence_score if arv else None,
            "comp_count": arv.comp_count if arv else 0,
            "price_per_sqft_avg": arv.price_per_sqft_avg if arv else None,
        },
        "rehab": {
            "level": rehab.rehab_level.value if rehab else None,
            "total_cost": rehab.total_cost if rehab else None,
            "cost_per_sqft": rehab.cost_per_sqft if rehab else None,
            "regional_multiplier": rehab.regional_multiplier if rehab else None,
        },
        "deal": {
            "list_price": deal.list_price if deal else None,
            "max_allowable_offer": deal.max_allowable_offer if deal else None,
            "estimated_profit": deal.estimated_profit if deal else None,
            "roi_pct": deal.roi_pct if deal else None,
            "cash_invested": deal.cash_invested if deal else None,
            "equity_spread": deal.equity_spread if deal else None,
            "holding_months": deal.holding_months if deal else None,
            "total_holding_cost": deal.total_holding_cost if deal else None,
        },
        "score": {
            "total": deal.opportunity_score if deal else None,
            "breakdown": {
                "profit_margin": deal.score_breakdown.profit_margin_component if deal else None,
                "roi": deal.score_breakdown.roi_component if deal else None,
                "arv_confidence": deal.score_breakdown.arv_confidence_component if deal else None,
                "distress": deal.score_breakdown.distress_component if deal else None,
                "equity_spread": deal.score_breakdown.equity_spread_component if deal else None,
            } if deal else {},
        },
    }


def build_json_report(state: ScannerRunState) -> dict:
    top_leads = sorted(
        state.accepted_leads,
        key=lambda r: r.deal_analysis.opportunity_score if r.deal_analysis else 0,
        reverse=True,
    )

    rejection_summary = {}
    for reason, count in state.rejection_breakdown.items():
        rejection_summary[reason] = count

    return {
        "report_type": "daily_lead_scanner",
        "run_id": str(state.run_id),
        "run_date": state.run_date.isoformat(),
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "stop_condition": state.stop_reason,
        "summary": {
            "iterations": state.iteration,
            "candidates_found": state.candidates_found,
            "candidates_checked": state.candidates_checked,
            "leads_accepted": state.leads_accepted,
            "leads_rejected": state.leads_rejected,
            "acceptance_rate_pct": round(
                state.leads_accepted / state.candidates_checked * 100, 1
            ) if state.candidates_checked else 0,
        },
        "rejection_breakdown": rejection_summary,
        "scanner_config": {
            "max_qualified_leads": config.max_qualified_leads,
            "max_candidates_checked": config.max_candidates_checked,
            "max_iterations": config.max_iterations,
            "min_comp_count": config.min_comp_count,
            "min_arv_confidence_score": config.min_arv_confidence_score,
            "min_estimated_profit": config.min_estimated_profit,
            "mao_arv_multiplier": config.mao_arv_multiplier,
        },
        "leads": [_lead_to_dict(r) for r in top_leads],
    }


# ──────────────────────────────────────────────────────────────────────────────
# HTML report — investor-facing daily digest
# ──────────────────────────────────────────────────────────────────────────────

_SCORE_COLOR = lambda s: (
    "#22c55e" if s >= 75 else
    "#eab308" if s >= 55 else
    "#ef4444"
)

_CONF_BADGE = {
    "high": ("HIGH", "#22c55e"),
    "medium": ("MED", "#eab308"),
    "low": ("LOW", "#ef4444"),
    "insufficient": ("N/A", "#6b7280"),
}


def _fmt_money(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"${v:,.0f}"


def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v:.1f}%"


def _lead_card_html(rank: int, result: PropertyPipelineResult) -> str:
    prop = result.property
    deal = result.deal_analysis
    arv = result.arv_result
    rehab = result.rehab_estimate

    score = deal.opportunity_score if deal else 0
    score_color = _SCORE_COLOR(score)
    conf_label, conf_color = _CONF_BADGE.get(
        arv.confidence.value if arv else "insufficient", ("N/A", "#6b7280")
    )
    distress_badges = " ".join(
        f'<span class="badge">{d.value.replace("_"," ").title()}</span>'
        for d in prop.distress_types
    )

    return f"""
    <div class="card">
      <div class="card-header">
        <div class="rank">#{rank}</div>
        <div class="address-block">
          <div class="address">{prop.street_address}</div>
          <div class="sub-address">{prop.city}, {prop.state} {prop.zip}</div>
          <div class="badges">{distress_badges}</div>
        </div>
        <div class="score-circle" style="background:{score_color}">
          <div class="score-num">{score:.0f}</div>
          <div class="score-lbl">Score</div>
        </div>
      </div>

      <div class="metrics-grid">
        <div class="metric">
          <div class="metric-label">ARV</div>
          <div class="metric-value">{_fmt_money(deal.arv if deal else None)}</div>
          <div class="metric-sub">
            <span class="conf-badge" style="background:{conf_color}">{conf_label}</span>
            {arv.comp_count if arv else 0} comps · ${arv.price_per_sqft_avg if arv else 0:,.0f}/sqft
          </div>
        </div>
        <div class="metric">
          <div class="metric-label">List Price</div>
          <div class="metric-value">{_fmt_money(deal.list_price if deal else None)}</div>
          <div class="metric-sub">Assessed: {_fmt_money(prop.assessed_value)}</div>
        </div>
        <div class="metric">
          <div class="metric-label">Rehab ({rehab.rehab_level.value.title() if rehab else "—"})</div>
          <div class="metric-value">{_fmt_money(rehab.total_cost if rehab else None)}</div>
          <div class="metric-sub">{_fmt_money(rehab.cost_per_sqft if rehab else None)}/sqft
            · {rehab.regional_multiplier if rehab else 1.0:.2f}× regional</div>
        </div>
        <div class="metric highlight-green">
          <div class="metric-label">Est. Profit</div>
          <div class="metric-value">{_fmt_money(deal.estimated_profit if deal else None)}</div>
          <div class="metric-sub">ROI: {_fmt_pct(deal.roi_pct if deal else None)}</div>
        </div>
        <div class="metric">
          <div class="metric-label">Max Offer (70%)</div>
          <div class="metric-value">{_fmt_money(deal.max_allowable_offer if deal else None)}</div>
          <div class="metric-sub">Spread: {_fmt_money(deal.equity_spread if deal else None)}</div>
        </div>
        <div class="metric">
          <div class="metric-label">Holding Cost</div>
          <div class="metric-value">{_fmt_money(deal.total_holding_cost if deal else None)}</div>
          <div class="metric-sub">{deal.holding_months if deal else 0} months</div>
        </div>
      </div>

      <div class="prop-details">
        {prop.bedrooms}bd / {prop.bathrooms}ba ·
        {prop.sqft:,} sqft · Built {prop.year_built} ·
        {prop.city}, {prop.state} ·
        Owner: {prop.owner_name or "Unknown"}
      </div>

      <div class="score-bar-section">
        <div class="score-bar-label">Score breakdown</div>
        <div class="score-bars">
          {"".join(f'''
          <div class="score-row">
            <span class="score-cat">{cat}</span>
            <div class="bar-track">
              <div class="bar-fill" style="width:{pct_w}%;background:{_SCORE_COLOR(val*10 if val <= 10 else val)}"></div>
            </div>
            <span class="score-val">{val:.1f}</span>
          </div>''' for cat, val, pct_w in [
            ("Profit Margin", deal.score_breakdown.profit_margin_component if deal else 0,
             (deal.score_breakdown.profit_margin_component / 30 * 100) if deal else 0),
            ("ROI", deal.score_breakdown.roi_component if deal else 0,
             (deal.score_breakdown.roi_component / 25 * 100) if deal else 0),
            ("ARV Confidence", deal.score_breakdown.arv_confidence_component if deal else 0,
             (deal.score_breakdown.arv_confidence_component / 20 * 100) if deal else 0),
            ("Distress Score", deal.score_breakdown.distress_component if deal else 0,
             (deal.score_breakdown.distress_component / 15 * 100) if deal else 0),
            ("Equity Spread", deal.score_breakdown.equity_spread_component if deal else 0,
             (deal.score_breakdown.equity_spread_component / 10 * 100) if deal else 0),
          ])}
        </div>
      </div>
    </div>"""


def build_html_report(state: ScannerRunState) -> str:
    top_leads = sorted(
        state.accepted_leads,
        key=lambda r: r.deal_analysis.opportunity_score if r.deal_analysis else 0,
        reverse=True,
    )

    total_profit = sum(
        r.deal_analysis.estimated_profit for r in top_leads if r.deal_analysis
    )
    avg_score = (
        sum(r.deal_analysis.opportunity_score for r in top_leads if r.deal_analysis)
        / len(top_leads) if top_leads else 0
    )
    avg_roi = (
        sum(r.deal_analysis.roi_pct for r in top_leads if r.deal_analysis)
        / len(top_leads) if top_leads else 0
    )

    rejection_rows = "\n".join(
        f"<tr><td>{r.replace('_',' ').title()}</td><td>{c}</td></tr>"
        for r, c in sorted(state.rejection_breakdown.items(), key=lambda x: -x[1])
    )

    lead_cards = "\n".join(
        _lead_card_html(rank + 1, result)
        for rank, result in enumerate(top_leads)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Daily Lead Report — {state.run_date}</title>
<style>
  :root {{
    --green: #22c55e; --yellow: #eab308; --red: #ef4444;
    --blue: #3b82f6; --gray: #6b7280; --dark: #111827;
    --card-bg: #1f2937; --bg: #111827;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: var(--bg); color: #f9fafb; padding: 24px; }}
  h1 {{ font-size: 1.75rem; font-weight: 700; margin-bottom: 4px; }}
  .subtitle {{ color: var(--gray); margin-bottom: 32px; font-size: 0.9rem; }}

  /* Summary strip */
  .summary-strip {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(140px,1fr));
                    gap: 16px; margin-bottom: 32px; }}
  .stat {{ background: var(--card-bg); border-radius: 12px; padding: 20px; }}
  .stat-value {{ font-size: 1.8rem; font-weight: 700; color: var(--green); }}
  .stat-label {{ font-size: 0.8rem; color: var(--gray); margin-top: 4px; }}

  /* Rejection table */
  .section-title {{ font-size: 1.1rem; font-weight: 600; margin-bottom: 12px; }}
  .rejection-section {{ background: var(--card-bg); border-radius: 12px;
                        padding: 20px; margin-bottom: 32px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ padding: 8px 12px; text-align: left; font-size: 0.875rem; }}
  th {{ color: var(--gray); border-bottom: 1px solid #374151; }}
  tr:not(:last-child) td {{ border-bottom: 1px solid #374151; }}

  /* Lead card */
  .card {{ background: var(--card-bg); border-radius: 16px; padding: 24px;
           margin-bottom: 24px; border: 1px solid #374151; }}
  .card-header {{ display: flex; gap: 16px; align-items: flex-start;
                  margin-bottom: 20px; }}
  .rank {{ font-size: 1.4rem; font-weight: 700; color: var(--gray);
           min-width: 36px; }}
  .address {{ font-size: 1.1rem; font-weight: 600; }}
  .sub-address {{ color: var(--gray); font-size: 0.85rem; margin-top: 2px; }}
  .badges {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }}
  .badge {{ background: #374151; color: #e5e7eb; font-size: 0.7rem;
            padding: 2px 8px; border-radius: 9999px; }}
  .address-block {{ flex: 1; }}
  .score-circle {{ width: 68px; height: 68px; border-radius: 50%;
                   display: flex; flex-direction: column; align-items: center;
                   justify-content: center; flex-shrink: 0; }}
  .score-num {{ font-size: 1.3rem; font-weight: 700; color: white; line-height: 1; }}
  .score-lbl {{ font-size: 0.6rem; color: rgba(255,255,255,0.8); text-transform: uppercase; }}

  .metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(150px,1fr));
                   gap: 16px; margin-bottom: 16px; }}
  .metric {{ background: #374151; border-radius: 10px; padding: 14px; }}
  .highlight-green {{ border: 1px solid var(--green); }}
  .metric-label {{ font-size: 0.7rem; text-transform: uppercase; letter-spacing: .05em;
                   color: var(--gray); margin-bottom: 4px; }}
  .metric-value {{ font-size: 1.15rem; font-weight: 700; }}
  .metric-sub {{ font-size: 0.75rem; color: var(--gray); margin-top: 4px; }}
  .conf-badge {{ color: white; font-size: 0.65rem; padding: 1px 6px;
                 border-radius: 4px; font-weight: 600; }}
  .prop-details {{ font-size: 0.8rem; color: var(--gray); margin-bottom: 16px; }}

  /* Score bars */
  .score-bar-section {{ background: #374151; border-radius: 10px; padding: 14px; }}
  .score-bar-label {{ font-size: 0.7rem; text-transform: uppercase;
                      color: var(--gray); margin-bottom: 10px; }}
  .score-row {{ display: flex; align-items: center; gap: 8px;
                margin-bottom: 6px; font-size: 0.8rem; }}
  .score-cat {{ width: 120px; color: #d1d5db; flex-shrink: 0; }}
  .bar-track {{ flex: 1; background: #1f2937; border-radius: 4px; height: 8px; }}
  .bar-fill {{ height: 100%; border-radius: 4px; transition: width .3s; }}
  .score-val {{ width: 32px; text-align: right; color: var(--gray); }}

  @media (max-width: 600px) {{
    .summary-strip {{ grid-template-columns: 1fr 1fr; }}
    .metrics-grid {{ grid-template-columns: 1fr 1fr; }}
  }}
</style>
</head>
<body>

<h1>🏠 Daily Lead Scanner Report</h1>
<div class="subtitle">
  Run date: <strong>{state.run_date}</strong> ·
  Run ID: {str(state.run_id)[:8]}… ·
  Stop: {state.stop_reason.replace("_"," ").title()} ·
  Generated: {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}
</div>

<div class="summary-strip">
  <div class="stat">
    <div class="stat-value">{state.leads_accepted}</div>
    <div class="stat-label">Qualified Leads</div>
  </div>
  <div class="stat">
    <div class="stat-value">{state.candidates_checked}</div>
    <div class="stat-label">Candidates Checked</div>
  </div>
  <div class="stat">
    <div class="stat-value">{state.iteration}</div>
    <div class="stat-label">Iterations</div>
  </div>
  <div class="stat">
    <div class="stat-value" style="color:#eab308">{state.leads_rejected}</div>
    <div class="stat-label">Rejected</div>
  </div>
  <div class="stat">
    <div class="stat-value">{_fmt_money(total_profit)}</div>
    <div class="stat-label">Total Est. Profit Pool</div>
  </div>
  <div class="stat">
    <div class="stat-value">{avg_score:.1f}</div>
    <div class="stat-label">Avg Opportunity Score</div>
  </div>
  <div class="stat">
    <div class="stat-value">{_fmt_pct(avg_roi)}</div>
    <div class="stat-label">Avg Estimated ROI</div>
  </div>
  <div class="stat">
    <div class="stat-value">
      {round(state.leads_accepted / state.candidates_checked * 100, 1) if state.candidates_checked else 0}%
    </div>
    <div class="stat-label">Acceptance Rate</div>
  </div>
</div>

<div class="rejection-section">
  <div class="section-title">Rejection Breakdown</div>
  <table>
    <thead><tr><th>Reason</th><th>Count</th></tr></thead>
    <tbody>{rejection_rows}</tbody>
  </table>
</div>

<div class="section-title">Top Qualified Leads</div>
{lead_cards}

</body>
</html>"""


# ──────────────────────────────────────────────────────────────────────────────
# File writer
# ──────────────────────────────────────────────────────────────────────────────

class ReportGenerator:
    def __init__(self, output_dir: str = None):
        self.output_dir = Path(output_dir or config.report_output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, state: ScannerRunState) -> dict[str, str]:
        """Generate JSON + HTML reports. Returns paths."""
        date_str = state.run_date.isoformat()

        json_report = build_json_report(state)
        html_report = build_html_report(state)

        json_path = self.output_dir / f"leads_{date_str}.json"
        html_path = self.output_dir / f"leads_{date_str}.html"

        json_path.write_text(json.dumps(json_report, indent=2, default=str))
        html_path.write_text(html_report, encoding="utf-8")

        return {
            "json": str(json_path),
            "html": str(html_path),
        }
