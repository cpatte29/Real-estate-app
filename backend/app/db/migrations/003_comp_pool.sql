-- =============================================================
-- Migration 003: Subject-independent comparable sales pool
-- =============================================================
-- comparable_sales (schema.sql) is a per-subject snapshot: it requires
-- subject_property_id NOT NULL and stores the similarity/adjustment values
-- computed for one specific ARV run. The Phase 2 comp ingestion pipeline
-- (PostgresCompRepository) instead stores a subject-independent pool of raw
-- sold comps that many subjects can later query — a different table with a
-- different lifecycle, not a relaxation of comparable_sales' NOT NULL
-- constraint.
--
-- comp_pool is that table. ARVEngine still writes its per-subject, scored
-- results to comparable_sales as before; PostgresCompRepository reads/writes
-- comp_pool exclusively.
--
-- Safe to run multiple times (idempotent via IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS comp_pool (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    address         VARCHAR(500) NOT NULL,
    city            VARCHAR(100),
    state           VARCHAR(2),
    zip             VARCHAR(10),
    location        GEOGRAPHY(POINT, 4326) NOT NULL,

    sale_date       DATE NOT NULL,
    sale_price      DECIMAL(12,2) NOT NULL,
    sqft            INTEGER,
    bedrooms        SMALLINT,
    bathrooms       DECIMAL(3,1),
    year_built      SMALLINT,
    property_type   property_type,
    pool            BOOLEAN,
    garage_spaces   SMALLINT,
    condition       VARCHAR(20),
    price_per_sqft  DECIMAL(8,2),

    source_id       VARCHAR(100),
    source_name     VARCHAR(50)  NOT NULL DEFAULT 'csv',

    -- Full uniqueness (not a partial index) — every comp_pool row must have
    -- a dedup_key, so ON CONFLICT (dedup_key) DO NOTHING always has a
    -- matching constraint to target.
    dedup_key       VARCHAR(64)  NOT NULL UNIQUE,

    ingested_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_comp_pool_location   ON comp_pool USING GIST(location);
CREATE INDEX IF NOT EXISTS idx_comp_pool_sale_date  ON comp_pool(sale_date DESC);
CREATE INDEX IF NOT EXISTS idx_comp_pool_source     ON comp_pool(source_name);
