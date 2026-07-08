-- =============================================================
-- Migration 001: Lead review / investor CRM columns
-- =============================================================
-- Adds investor CRM review state to the leads table.
-- Safe to run multiple times (idempotent via IF NOT EXISTS / DO blocks).

-- Step 1 — Create the review status enum
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'lead_review_status') THEN
        CREATE TYPE lead_review_status AS ENUM (
            'new',
            'reviewed',
            'rejected_by_human',
            'contacted',
            'under_contract',
            'closed'
        );
    END IF;
END;
$$;

-- Step 2 — Add review columns to leads
ALTER TABLE leads
    ADD COLUMN IF NOT EXISTS review_status  lead_review_status NOT NULL DEFAULT 'new',
    ADD COLUMN IF NOT EXISTS human_notes    TEXT               NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS reviewed_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS reviewed_by    VARCHAR(200)       NOT NULL DEFAULT '';

-- Step 3 — Index for status-filtered dashboard queries
CREATE INDEX IF NOT EXISTS idx_leads_review_status
    ON leads(review_status);
