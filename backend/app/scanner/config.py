"""
Scanner configuration — all tunable constants in one place.
Override via environment variables or .env file.
"""
from pydantic_settings import BaseSettings


class ScannerConfig(BaseSettings):
    # ── Stop conditions ────────────────────────────────────────────
    max_qualified_leads: int = 25
    max_candidates_checked: int = 500
    max_iterations: int = 8

    # ── Verification thresholds ────────────────────────────────────
    min_comp_count: int = 3
    min_arv_confidence_score: float = 70.0   # 0-100
    min_estimated_profit: float = 20_000.0
    min_opportunity_score: float = 50.0

    # ── ARV calculation ────────────────────────────────────────────
    arv_radius_miles: float = 0.5
    arv_date_range_months: int = 6
    arv_sqft_tolerance_pct: int = 20
    arv_max_comps: int = 10

    # ── MAO (Max Allowable Offer) ──────────────────────────────────
    # MAO = ARV * mao_arv_multiplier - rehab_cost
    mao_arv_multiplier: float = 0.70

    # ── Rehab cost defaults ($/sqft, national median) ─────────────
    rehab_cost_cosmetic_per_sqft: float = 20.0
    rehab_cost_moderate_per_sqft: float = 37.5
    rehab_cost_full_per_sqft: float = 75.0

    # ── Deal cost assumptions ──────────────────────────────────────
    closing_cost_buy_pct: float = 2.0
    closing_cost_sell_pct: float = 2.0
    agent_commission_pct: float = 5.0
    holding_months: int = 6
    monthly_insurance: float = 150.0
    monthly_utilities: float = 200.0

    # ── Opportunity score weights (must sum to 1.0) ────────────────
    score_weight_profit_margin: float = 0.30
    score_weight_roi: float = 0.25
    score_weight_arv_confidence: float = 0.20
    score_weight_distress_score: float = 0.15
    score_weight_equity_spread: float = 0.10

    # ── Data simulation (used when real APIs are unavailable) ──────
    simulate_data: bool = True
    simulation_seed: int = 42
    candidate_batch_size: int = 50

    # ── Database ───────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://rei:rei@localhost:5432/rei_platform"

    # ── Report output ──────────────────────────────────────────────
    report_output_dir: str = "reports"
    report_s3_bucket: str = ""

    model_config = {"env_file": ".env", "env_prefix": "SCANNER_"}


# Module-level singleton
config = ScannerConfig()
