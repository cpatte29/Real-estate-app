-- =============================================================
-- Migration 002: Comp provenance, dedup, and ingestion audit trail
-- =============================================================
-- Supports Phase 2 of the comparable sales subsystem: a Postgres/PostGIS-
-- backed CompRepository with idempotent CSV ingestion.
-- Safe to run multiple times (idempotent via IF NOT EXISTS).

-- Step 1 — Provenance + dedup columns on comparable_sales
ALTER TABLE comparable_sales
    ADD COLUMN IF NOT EXISTS source_id      VARCHAR(100),
    ADD COLUMN IF NOT EXISTS source_name    VARCHAR(50)  NOT NULL DEFAULT 'csv',
    ADD COLUMN IF NOT EXISTS ingested_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS dedup_key      VARCHAR(64),
    ADD COLUMN IF NOT EXISTS pool           BOOLEAN,
    ADD COLUMN IF NOT EXISTS garage_spaces  SMALLINT,
    ADD COLUMN IF NOT EXISTS condition      VARCHAR(20);

-- dedup_key uniqueness enforced via a partial unique index (NULLs allowed
-- for legacy/simulated rows that predate this migration).
CREATE UNIQUE INDEX IF NOT EXISTS idx_comps_dedup_key
    ON comparable_sales(dedup_key)
    WHERE dedup_key IS NOT NULL;

-- Step 2 — Query-path indexes for the PostGIS-backed repository
CREATE INDEX IF NOT EXISTS idx_comps_location   ON comparable_sales USING GIST(location);
CREATE INDEX IF NOT EXISTS idx_comps_sale_date  ON comparable_sales(sale_date DESC);
CREATE INDEX IF NOT EXISTS idx_comps_source     ON comparable_sales(source_name);

-- Step 3 — Ingestion run audit trail
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'ingestion_status') THEN
        CREATE TYPE ingestion_status AS ENUM ('success', 'partial', 'failed');
    END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS comp_ingestion_runs (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_name      VARCHAR(50)  NOT NULL,
    file_name        VARCHAR(500),
    records_read     INTEGER NOT NULL DEFAULT 0,
    records_added    INTEGER NOT NULL DEFAULT 0,
    records_skipped  INTEGER NOT NULL DEFAULT 0,
    error_count      INTEGER NOT NULL DEFAULT 0,
    errors           JSONB NOT NULL DEFAULT '[]',
    started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at     TIMESTAMPTZ,
    status           ingestion_status NOT NULL DEFAULT 'success'
);

CREATE INDEX IF NOT EXISTS idx_comp_ingestion_source
    ON comp_ingestion_runs(source_name);
CREATE INDEX IF NOT EXISTS idx_comp_ingestion_started
    ON comp_ingestion_runs(started_at DESC);
