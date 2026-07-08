"""
Rehab cost estimation engine.

Determines the rehab level (cosmetic / moderate / full) from property
condition signals, then computes a line-item estimate with a regional
cost-of-living multiplier.
"""
from __future__ import annotations

from uuid import UUID

from app.scanner.config import config
from app.scanner.models import (
    CandidateProperty,
    RehabEstimate,
    RehabLevel,
    RehabLineItem,
)

# ──────────────────────────────────────────────────────────────────────────────
# Regional cost multipliers by state
# ──────────────────────────────────────────────────────────────────────────────

REGIONAL_MULTIPLIERS: dict[str, float] = {
    "CA": 1.55, "NY": 1.50, "WA": 1.35, "MA": 1.40, "CO": 1.25,
    "FL": 1.10, "TX": 1.05, "GA": 1.00, "TN": 0.95, "AL": 0.88,
    "OH": 0.92, "MI": 0.90, "IN": 0.93, "MO": 0.91, "KY": 0.89,
    "AR": 0.87, "MS": 0.85, "LA": 0.88, "SC": 0.92, "NC": 0.95,
}


def _regional_multiplier(state: str) -> float:
    return REGIONAL_MULTIPLIERS.get(state.upper(), 1.00)


# ──────────────────────────────────────────────────────────────────────────────
# Condition signal detector — infers rehab level from available data
# ──────────────────────────────────────────────────────────────────────────────

def _infer_rehab_level(prop: CandidateProperty) -> tuple[RehabLevel, dict]:
    """
    Infer rehab level from property characteristics.
    Returns (level, signals_dict).
    """
    signals: dict[str, str] = {}
    full_points = 0
    moderate_points = 0

    year = prop.year_built or 1975
    age = 2026 - year

    if age >= 50:
        signals["age"] = f"Built {year} — {age} years old, high probability of major system replacement"
        full_points += 3
    elif age >= 30:
        signals["age"] = f"Built {year} — {age} years old, likely moderate mechanical wear"
        moderate_points += 2
    else:
        signals["age"] = f"Built {year} — relatively modern"

    from app.scanner.models import DistressType
    if DistressType.VACANCY in prop.distress_types:
        signals["vacancy"] = "Property listed as vacant — weather intrusion and vandalism risk"
        full_points += 2
    if DistressType.CODE_VIOLATION in prop.distress_types:
        signals["code_violation"] = "Active code violations — may require structural or safety remediation"
        full_points += 3
    if DistressType.FORECLOSURE if hasattr(DistressType, 'FORECLOSURE') else DistressType.PRE_FORECLOSURE in prop.distress_types:
        signals["foreclosure"] = "Pre-foreclosure/distress — deferred maintenance expected"
        moderate_points += 2
    if DistressType.PRE_FORECLOSURE in prop.distress_types:
        signals["pre_foreclosure"] = "Pre-foreclosure — deferred maintenance expected"
        moderate_points += 2

    if prop.estimated_equity_pct is not None and prop.estimated_equity_pct < 10:
        signals["equity"] = "Very low equity — owner likely unable to fund upkeep"
        moderate_points += 1

    if prop.ownership_length_yrs and prop.ownership_length_yrs > 20:
        signals["long_ownership"] = "Long-term ownership — significant deferred maintenance probable"
        moderate_points += 1

    if full_points >= 4:
        return RehabLevel.FULL, signals
    elif moderate_points + full_points >= 3:
        return RehabLevel.MODERATE, signals
    else:
        return RehabLevel.COSMETIC, signals


# ──────────────────────────────────────────────────────────────────────────────
# Line-item cost templates (base costs, national median, pre-multiplier)
# ──────────────────────────────────────────────────────────────────────────────

COSMETIC_LINE_ITEMS = [
    ("interior_paint",   "Interior paint — whole house",         3_500),
    ("exterior_paint",   "Exterior paint / pressure wash",       2_000),
    ("flooring",         "LVP flooring throughout",              5_000),
    ("fixtures",         "Light fixtures and hardware",          1_500),
    ("landscaping",      "Landscaping and cleanup",              1_500),
    ("cleaning",         "Deep cleaning and haul-out",           1_000),
    ("doors",            "Interior door hardware and touch-ups", 800),
]

MODERATE_LINE_ITEMS = COSMETIC_LINE_ITEMS + [
    ("kitchen_cosmetic", "Kitchen cabinet repaint, new countertop, appliances", 12_000),
    ("bathroom_update",  "Bathroom tile, vanity, fixtures × 2",                10_000),
    ("plumbing_minor",   "Minor plumbing repairs, valves, supply lines",        3_000),
    ("electrical_minor", "Panel inspection, GFCI upgrades, minor repairs",      2_500),
    ("hvac_service",     "HVAC service and minor repair",                        1_500),
    ("roof_repair",      "Roof repair / patch (not full replacement)",           3_500),
    ("windows_minor",    "Caulk, weatherstrip, 2-3 window replacements",        3_000),
]

FULL_LINE_ITEMS = [
    ("demo",             "Demolition and haul-out",                  4_000),
    ("framing",          "Framing repairs",                          5_000),
    ("roof_full",        "Full roof replacement",                   12_000),
    ("hvac_full",        "Full HVAC replacement",                    8_000),
    ("electrical_full",  "Full electrical upgrade / rewire",        10_000),
    ("plumbing_full",    "Full plumbing replacement",                9_000),
    ("insulation",       "Insulation — attic and walls",             4_000),
    ("drywall",          "Drywall throughout",                       6_000),
    ("kitchen_full",     "Full kitchen remodel",                    22_000),
    ("bathrooms_full",   "Full bathroom remodel × 2",               18_000),
    ("flooring_full",    "Hardwood / LVP throughout",                7_000),
    ("paint_full",       "Interior and exterior paint",              5_000),
    ("windows_full",     "Full window replacement",                  8_000),
    ("doors_full",       "Exterior and interior doors",              4_000),
    ("landscaping_full", "Grading, landscaping, driveway",           5_000),
    ("permits_misc",     "Permits, contingency, misc",               6_000),
]


def _build_line_items(level: RehabLevel, sqft: int, multiplier: float) -> list[RehabLineItem]:
    sqft_factor = sqft / 1_500  # scale relative to a 1,500 sqft baseline

    template = {
        RehabLevel.COSMETIC: COSMETIC_LINE_ITEMS,
        RehabLevel.MODERATE: MODERATE_LINE_ITEMS,
        RehabLevel.FULL: FULL_LINE_ITEMS,
    }.get(level, MODERATE_LINE_ITEMS)

    items = []
    for category, description, base_cost in template:
        adjusted = round(base_cost * sqft_factor * multiplier, -2)
        items.append(RehabLineItem(category=category, description=description, cost=adjusted))
    return items


# ──────────────────────────────────────────────────────────────────────────────
# Public interface
# ──────────────────────────────────────────────────────────────────────────────

class RehabEstimator:
    def estimate(self, prop: CandidateProperty, scanner_run_id: UUID) -> RehabEstimate:
        level, signals = _infer_rehab_level(prop)
        multiplier = _regional_multiplier(prop.state)
        sqft = prop.sqft or 1_500

        line_items = _build_line_items(level, sqft, multiplier)
        total_cost = sum(item.cost for item in line_items)
        cost_per_sqft = round(total_cost / sqft, 2) if sqft else 0.0

        return RehabEstimate(
            property_id=prop.id,
            scanner_run_id=scanner_run_id,
            rehab_level=level,
            total_cost=round(total_cost, 2),
            cost_per_sqft=cost_per_sqft,
            regional_multiplier=multiplier,
            line_items=line_items,
            condition_signals=signals,
        )
