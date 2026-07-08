-- =============================================================
-- Daily Distressed Property Lead Scanner — Database Schema
-- PostgreSQL 16 + PostGIS 3.4
-- =============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "postgis";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- =============================================================
-- ENUMS
-- =============================================================

CREATE TYPE property_type     AS ENUM ('sfr','multi_family','condo','townhouse','land');
CREATE TYPE distress_type     AS ENUM (
    'pre_foreclosure','tax_lien','auction','code_violation',
    'vacancy','probate','bankruptcy','divorce'
);
CREATE TYPE arv_confidence    AS ENUM ('high','medium','low','insufficient');
CREATE TYPE lead_status       AS ENUM (
    'pending','verified','rejected','accepted','under_review','archived'
);
CREATE TYPE rejection_reason  AS ENUM (
    'missing_address','insufficient_comps','low_arv_confidence',
    'insufficient_profit','missing_repair_estimate','duplicate',
    'negative_equity','bad_data','failed_mao_rule'
);
CREATE TYPE run_status        AS ENUM ('running','completed','failed','partial');
CREATE TYPE rehab_level       AS ENUM ('cosmetic','moderate','full','unknown');

-- =============================================================
-- SCANNER RUN STATE — one row per daily execution
-- =============================================================

CREATE TABLE scanner_runs (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_date            DATE NOT NULL UNIQUE,
    status              run_status NOT NULL DEFAULT 'running',

    -- Counters
    candidates_found    INTEGER NOT NULL DEFAULT 0,
    candidates_checked  INTEGER NOT NULL DEFAULT 0,
    leads_accepted      INTEGER NOT NULL DEFAULT 0,
    leads_rejected      INTEGER NOT NULL DEFAULT 0,
    iterations          SMALLINT NOT NULL DEFAULT 0,

    -- Stop condition that fired
    stop_reason         VARCHAR(50),   -- 'qualified_limit','candidate_limit','iteration_limit'

    -- Timing
    started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ,
    duration_seconds    DECIMAL(8,2),

    -- Per-rejection-reason counts (JSONB for flexibility)
    rejection_breakdown JSONB NOT NULL DEFAULT '{}',

    -- Run configuration snapshot
    config              JSONB NOT NULL DEFAULT '{}',

    -- Report storage
    report_s3_key       VARCHAR(500),
    report_html         TEXT,

    error_log           JSONB NOT NULL DEFAULT '[]'
);

CREATE INDEX idx_scanner_runs_date    ON scanner_runs(run_date DESC);
CREATE INDEX idx_scanner_runs_status  ON scanner_runs(status);

-- =============================================================
-- PROPERTIES — core property record
-- =============================================================

CREATE TABLE properties (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- Address
    street_address      VARCHAR(500) NOT NULL,
    city                VARCHAR(100) NOT NULL,
    state               VARCHAR(2)   NOT NULL,
    zip                 VARCHAR(10)  NOT NULL,
    county              VARCHAR(100),
    apn                 VARCHAR(50),

    -- Geolocation
    location            GEOGRAPHY(POINT, 4326),

    -- Attributes
    property_type       property_type,
    bedrooms            SMALLINT,
    bathrooms           DECIMAL(3,1),
    sqft                INTEGER,
    lot_sqft            INTEGER,
    year_built          SMALLINT,
    garage_spaces       SMALLINT DEFAULT 0,
    pool                BOOLEAN  DEFAULT false,

    -- Valuation
    assessed_value      DECIMAL(12,2),
    estimated_value     DECIMAL(12,2),
    tax_annual          DECIMAL(10,2),

    -- Ownership
    owner_name          VARCHAR(255),
    owner_mailing_addr  VARCHAR(500),
    owner_occupied      BOOLEAN,
    ownership_length_yrs DECIMAL(4,1),

    -- Sales history
    last_sale_date      DATE,
    last_sale_price     DECIMAL(12,2),
    estimated_equity_pct DECIMAL(5,2),

    -- Distress
    distress_types      distress_type[] NOT NULL DEFAULT '{}',
    distress_score      SMALLINT,            -- 0-100

    -- Data provenance
    data_source         VARCHAR(50)  NOT NULL DEFAULT 'simulated',
    source_record_id    VARCHAR(100),
    raw_data            JSONB,

    -- Dedup fingerprint (hash of address normalized)
    address_fingerprint VARCHAR(64) UNIQUE,

    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_properties_location  ON properties USING GIST(location);
CREATE INDEX idx_properties_zip       ON properties(zip);
CREATE INDEX idx_properties_state     ON properties(state, city);
CREATE INDEX idx_properties_apn       ON properties(apn) WHERE apn IS NOT NULL;
CREATE INDEX idx_properties_address   ON properties USING GIN(street_address gin_trgm_ops);

-- =============================================================
-- COMPARABLE SALES — closed sales used for ARV
-- =============================================================

CREATE TABLE comparable_sales (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    subject_property_id UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,

    -- Comp property info
    address             VARCHAR(500) NOT NULL,
    city                VARCHAR(100),
    state               VARCHAR(2),
    zip                 VARCHAR(10),
    location            GEOGRAPHY(POINT, 4326),

    -- Sale details
    sale_date           DATE NOT NULL,
    sale_price          DECIMAL(12,2) NOT NULL,
    sqft                INTEGER,
    bedrooms            SMALLINT,
    bathrooms           DECIMAL(3,1),
    year_built          SMALLINT,
    property_type       property_type,

    -- Computed
    price_per_sqft      DECIMAL(8,2),
    distance_miles      DECIMAL(5,3),
    similarity_score    DECIMAL(5,2),   -- 0-100
    price_adjustment    DECIMAL(10,2) DEFAULT 0,  -- +/- adjustment applied
    adjusted_price      DECIMAL(12,2),

    -- Source
    data_source         VARCHAR(50) DEFAULT 'simulated',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_comps_subject ON comparable_sales(subject_property_id);

-- =============================================================
-- ARV CALCULATIONS — one per property per scanner run
-- =============================================================

CREATE TABLE arv_calculations (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id         UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    scanner_run_id      UUID NOT NULL REFERENCES scanner_runs(id) ON DELETE CASCADE,

    -- Parameters used
    radius_miles        DECIMAL(4,2) NOT NULL DEFAULT 0.5,
    date_range_months   SMALLINT     NOT NULL DEFAULT 6,
    sqft_tolerance_pct  SMALLINT     NOT NULL DEFAULT 20,

    -- Results
    arv                 DECIMAL(12,2),
    confidence          arv_confidence NOT NULL DEFAULT 'insufficient',
    confidence_score    DECIMAL(5,2),  -- 0-100 numeric
    comp_count          SMALLINT NOT NULL DEFAULT 0,
    price_per_sqft_avg  DECIMAL(8,2),
    price_per_sqft_stddev DECIMAL(8,2),

    -- Comp IDs included in calculation
    comp_ids            UUID[] NOT NULL DEFAULT '{}',

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (property_id, scanner_run_id)
);

CREATE INDEX idx_arv_property    ON arv_calculations(property_id);
CREATE INDEX idx_arv_scanner_run ON arv_calculations(scanner_run_id);

-- =============================================================
-- REHAB ESTIMATES
-- =============================================================

CREATE TABLE rehab_estimates (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id         UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    scanner_run_id      UUID NOT NULL REFERENCES scanner_runs(id) ON DELETE CASCADE,

    rehab_level         rehab_level NOT NULL DEFAULT 'unknown',
    total_cost          DECIMAL(12,2),
    cost_per_sqft       DECIMAL(8,2),
    regional_multiplier DECIMAL(4,2) NOT NULL DEFAULT 1.0,

    -- Breakdown stored as JSON
    line_items          JSONB NOT NULL DEFAULT '[]',

    -- Condition signals used to determine level
    condition_signals   JSONB NOT NULL DEFAULT '{}',

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (property_id, scanner_run_id)
);

CREATE INDEX idx_rehab_property    ON rehab_estimates(property_id);
CREATE INDEX idx_rehab_scanner_run ON rehab_estimates(scanner_run_id);

-- =============================================================
-- DEAL ANALYSES — per-property investment metrics
-- =============================================================

CREATE TABLE deal_analyses (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id         UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    scanner_run_id      UUID NOT NULL REFERENCES scanner_runs(id) ON DELETE CASCADE,

    -- Inputs
    list_price          DECIMAL(12,2),
    arv                 DECIMAL(12,2) NOT NULL,
    rehab_cost          DECIMAL(12,2) NOT NULL,
    holding_months      SMALLINT NOT NULL DEFAULT 6,

    -- Holding cost components
    monthly_taxes       DECIMAL(10,2) DEFAULT 0,
    monthly_insurance   DECIMAL(10,2) DEFAULT 150,
    monthly_utilities   DECIMAL(10,2) DEFAULT 200,
    monthly_mortgage    DECIMAL(10,2) DEFAULT 0,
    total_holding_cost  DECIMAL(12,2) NOT NULL,

    -- Transaction costs
    closing_cost_buy_pct  DECIMAL(4,2) NOT NULL DEFAULT 2.0,
    closing_cost_sell_pct DECIMAL(4,2) NOT NULL DEFAULT 2.0,
    agent_commission_pct  DECIMAL(4,2) NOT NULL DEFAULT 5.0,
    closing_cost_buy      DECIMAL(12,2) NOT NULL,
    closing_cost_sell     DECIMAL(12,2) NOT NULL,
    agent_commission      DECIMAL(12,2) NOT NULL,

    -- Core outputs
    max_allowable_offer DECIMAL(12,2) NOT NULL,   -- ARV * 70% - rehab
    estimated_profit    DECIMAL(12,2) NOT NULL,
    roi_pct             DECIMAL(8,2),
    cash_invested       DECIMAL(12,2),
    equity_spread       DECIMAL(12,2),             -- list_price - MAO

    -- Scoring
    opportunity_score   DECIMAL(5,2) NOT NULL,     -- 0-100
    score_breakdown     JSONB NOT NULL DEFAULT '{}',

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (property_id, scanner_run_id)
);

CREATE INDEX idx_deals_property    ON deal_analyses(property_id);
CREATE INDEX idx_deals_scanner_run ON deal_analyses(scanner_run_id);
CREATE INDEX idx_deals_score       ON deal_analyses(opportunity_score DESC);

-- =============================================================
-- LEADS — scanner output, one row per accepted opportunity
-- =============================================================

CREATE TABLE leads (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id         UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    scanner_run_id      UUID NOT NULL REFERENCES scanner_runs(id) ON DELETE CASCADE,
    deal_analysis_id    UUID REFERENCES deal_analyses(id),
    arv_calculation_id  UUID REFERENCES arv_calculations(id),
    rehab_estimate_id   UUID REFERENCES rehab_estimates(id),

    status              lead_status NOT NULL DEFAULT 'pending',
    opportunity_score   DECIMAL(5,2) NOT NULL,

    -- Snapshot of key metrics at time of acceptance (denormalized for reporting)
    arv                 DECIMAL(12,2) NOT NULL,
    list_price          DECIMAL(12,2),
    rehab_cost          DECIMAL(12,2) NOT NULL,
    max_allowable_offer DECIMAL(12,2) NOT NULL,
    estimated_profit    DECIMAL(12,2) NOT NULL,
    roi_pct             DECIMAL(8,2),
    arv_confidence      arv_confidence NOT NULL,
    comp_count          SMALLINT NOT NULL,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (property_id, scanner_run_id)
);

CREATE INDEX idx_leads_scanner_run ON leads(scanner_run_id);
CREATE INDEX idx_leads_status      ON leads(status);
CREATE INDEX idx_leads_score       ON leads(opportunity_score DESC);

-- =============================================================
-- REJECTED PROPERTIES — audit trail for all rejections
-- =============================================================

CREATE TABLE rejected_properties (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id         UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    scanner_run_id      UUID NOT NULL REFERENCES scanner_runs(id) ON DELETE CASCADE,
    rejection_reason    rejection_reason NOT NULL,
    rejection_detail    VARCHAR(500),
    rejected_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (property_id, scanner_run_id)
);

CREATE INDEX idx_rejected_run    ON rejected_properties(scanner_run_id);
CREATE INDEX idx_rejected_reason ON rejected_properties(rejection_reason);

-- =============================================================
-- SCANNER STATE — persistent key-value store across runs
-- =============================================================

CREATE TABLE scanner_state (
    key         VARCHAR(255) PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed initial state
INSERT INTO scanner_state (key, value) VALUES
    ('checked_property_ids',  '[]'),
    ('accepted_lead_ids',     '[]'),
    ('last_run_date',         'null'),
    ('total_runs',            '0'),
    ('total_leads_all_time',  '0');

-- =============================================================
-- TRIGGERS — auto-update updated_at
-- =============================================================

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_properties_updated_at
    BEFORE UPDATE ON properties
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_leads_updated_at
    BEFORE UPDATE ON leads
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_scanner_state_updated_at
    BEFORE UPDATE ON scanner_state
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
