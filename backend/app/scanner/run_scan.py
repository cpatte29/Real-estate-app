"""
CLI entry point — run the scanner directly (without Celery/FastAPI).

Usage:
    python -m app.scanner.run_scan
    python -m app.scanner.run_scan --date 2026-06-17 --max-leads 10
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)


def main():
    parser = argparse.ArgumentParser(description="Run the daily lead scanner")
    parser.add_argument("--date", help="Run date (YYYY-MM-DD), default: today")
    parser.add_argument("--max-leads", type=int, help="Override max qualified leads")
    parser.add_argument("--max-candidates", type=int, help="Override max candidates checked")
    parser.add_argument("--no-report", action="store_true", help="Skip report generation")
    parser.add_argument("--open-report", action="store_true", help="Open HTML report in browser")
    args = parser.parse_args()

    run_date = date.fromisoformat(args.date) if args.date else date.today()

    from app.scanner.config import ScannerConfig
    overrides = {}
    if args.max_leads:
        overrides["max_qualified_leads"] = args.max_leads
    if args.max_candidates:
        overrides["max_candidates_checked"] = args.max_candidates

    cfg = ScannerConfig(**overrides) if overrides else None

    from app.scanner.pipeline import LeadScannerPipeline
    pipeline = LeadScannerPipeline(cfg=cfg)

    print(f"\n{'='*60}")
    print(f"  Daily Lead Scanner — {run_date}")
    print(f"{'='*60}\n")

    state = pipeline.run(run_date=run_date)

    # ── Terminal summary ──────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  SCAN COMPLETE — {state.stop_reason.replace('_',' ').upper()}")
    print(f"{'='*60}")
    print(f"  Iterations:         {state.iteration}")
    print(f"  Candidates checked: {state.candidates_checked:,}")
    print(f"  Leads accepted:     {state.leads_accepted}")
    print(f"  Leads rejected:     {state.leads_rejected}")
    print()
    print("  Rejection breakdown:")
    for reason, count in sorted(state.rejection_breakdown.items(), key=lambda x: -x[1]):
        print(f"    {reason.replace('_',' ').title():<30} {count}")

    if state.accepted_leads:
        print()
        print("  Top 5 leads by score:")
        top5 = sorted(
            state.accepted_leads,
            key=lambda r: r.deal_analysis.opportunity_score if r.deal_analysis else 0,
            reverse=True,
        )[:5]
        for i, r in enumerate(top5):
            d = r.deal_analysis
            print(
                f"    #{i+1} {r.property.street_address:<35} "
                f"Score:{d.opportunity_score:5.1f}  "
                f"Profit:${d.estimated_profit:>8,.0f}  "
                f"ROI:{d.roi_pct:5.1f}%"
            )

    if not args.no_report:
        from app.reports.generator import ReportGenerator
        gen = ReportGenerator()
        paths = gen.generate(state)
        print(f"\n  Reports saved:")
        print(f"    JSON: {paths['json']}")
        print(f"    HTML: {paths['html']}")

        if args.open_report:
            import webbrowser
            webbrowser.open(f"file://{paths['html']}")

    print()
    return state


if __name__ == "__main__":
    main()
