# Daily Distressed Property Lead Scanner — System Document

---

## 1. System Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     DAILY LEAD SCANNER LOOP                             │
│                                                                         │
│   ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐         │
│   │ DISCOVER │───▶│   PLAN   │───▶│ EXECUTE  │───▶│  VERIFY  │         │
│   │          │    │          │    │          │    │          │         │
│   │ Fetch    │    │ Determine│    │ ARV calc │    │ 7-rule   │         │
│   │ distress │    │ data     │    │ Rehab    │    │ rejection│         │
│   │ candidate│    │ needed   │    │ estimate │    │ gate     │         │
│   │ batch    │    │ per prop │    │ Deal     │    │          │         │
│   └──────────┘    └──────────┘    │ analysis │    └────┬─────┘         │
│         ▲                         │ Scoring  │         │               │
│         │                         └──────────┘    Pass │  Fail         │
│         │                                         ┌────▼──┐ ┌────────┐ │
│   ┌─────┴──────┐                                  │ACCEPT │ │REJECT  │ │
│   │  ITERATE   │◀─────────────────────────────────│       │ │+reason │ │
│   │            │     stop condition not hit        └───────┘ └────────┘ │
│   │ Next batch │                                                         │
│   └────────────┘                                                         │
│                                                                          │
│   STOP when: 25 qualified leads OR 500 candidates OR 8 iterations       │
└─────────────────────────────────────────────────────────────────────────┘
                              │ stop condition
                              ▼
                    ┌─────────────────┐
                    │  STATE PERSIST  │
                    │  scanner_state  │
                    │  DB tables      │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │ REPORT GENERATE │
                    │ JSON + HTML     │
                    │ S3 upload       │
                    └─────────────────┘
```

### Component Map

| Component | File | Responsibility |
|-----------|------|----------------|
| Pipeline orchestrator | `scanner/pipeline.py` | Loop control, phase sequencing, stop conditions |
| Discovery | `scanner/discovery.py` | Candidate property fetching from any source |
| ARV Engine | `scanner/arv.py` | Comp selection, similarity scoring, weighted ARV |
| Rehab Estimator | `scanner/rehab.py` | Level inference, line-item cost calculation |
| Deal Calculator | `scanner/scoring.py` | MAO, profit, ROI, opportunity score |
| Verifier | `scanner/verification.py` | All 7 rejection rules |
| Report Generator | `reports/generator.py` | JSON + HTML daily investor report |
| FastAPI endpoints | `api/scanner.py` | REST interface for triggering and querying |
| Celery task | `workers/daily_scan.py` | Scheduled 06:00 UTC execution |
| Config | `scanner/config.py` | All tunable thresholds in one place |

---

## 2. Database Schema Summary

### Core Tables

| Table | Purpose |
|-------|---------|
| `scanner_runs` | One row per daily run; tracks counters, stop reason, report path |
| `properties` | Master property record with geolocation (PostGIS GEOGRAPHY) |
| `comparable_sales` | Comp transactions used in ARV calculation |
| `arv_calculations` | ARV result per property per run |
| `rehab_estimates` | Rehab level + line items per property per run |
| `deal_analyses` | Full financial analysis: MAO, profit, ROI, score |
| `leads` | Accepted qualified leads (denormalized snapshot for fast reporting) |
| `rejected_properties` | Audit trail of all rejections with reason code |
| `scanner_state` | Persistent KV store for cross-run state |
| `data_ingestion_runs` | Tracks external data fetch operations |

### Key Design Decisions

- **PostGIS** `GEOGRAPHY(POINT, 4326)` on `properties.location` enables radius queries with `ST_DWithin`.
- **`address_fingerprint`** (SHA-256 of normalized address) enforces dedup across runs without full-table scans.
- **Row-Level Security** disabled on scanner tables (scanner runs as a service account, not per-user).
- **JSONB** used for `line_items`, `score_breakdown`, `condition_signals` — flexible without extra tables.

---

## 3. API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/scanner/run` | Trigger daily scan (async, returns run_id) |
| `GET` | `/api/v1/scanner/runs` | List all runs with status |
| `GET` | `/api/v1/scanner/runs/{id}` | Run detail + rejection breakdown |
| `GET` | `/api/v1/scanner/runs/{id}/report` | HTML report for a completed run |
| `GET` | `/api/v1/scanner/leads` | All accepted leads across runs (filterable) |
| `GET` | `/api/v1/scanner/state` | Persistent scanner state + config |

### POST `/api/v1/scanner/run`
```json
{
  "run_date": "2026-06-17",
  "max_qualified_leads": 25,
  "max_candidates_checked": 500,
  "dry_run": false
}
```
Response `202 Accepted`:
```json
{
  "run_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "queued",
  "message": "Scan queued for 2026-06-17. Poll /runs/{run_id} for status."
}
```

---

## 4. Scoring Formula

```
Opportunity Score (0–100) = sum of 5 weighted components

Component            Weight   Basis                            Full Score At
───────────────────  ──────   ────────────────────────────     ─────────────
Profit Margin          30%    (profit / ARV)                   ≥ 30% margin
ROI                    25%    (profit / cash invested)         ≥ 40% ROI
ARV Confidence         20%    confidence_score (0–100)         score = 100
Distress Score         15%    property distress signal (0–100) score = 100
Equity Spread          10%    how far list_price < MAO         $50k+ below MAO
```

**Score interpretation:**
| Range | Grade | Action |
|-------|-------|--------|
| 80–100 | Excellent | Pursue immediately |
| 65–79 | Good | Strong candidate |
| 50–64 | Fair | Review carefully |
| < 50 | Poor | Do not accept (filtered by verifier) |

---

## 5. Verification Rules

All 7 rules must pass. Properties are rejected on first failure.

| # | Rule | Threshold | Rejection Code |
|---|------|-----------|----------------|
| 1 | Address completeness | street_address, city, state, zip all non-empty | `missing_address` |
| 2 | Duplicate detection | address_fingerprint not in seen set | `duplicate` |
| 3 | Minimum comp count | ≥ 3 comparable sales found | `insufficient_comps` |
| 4 | ARV confidence floor | confidence_score ≥ 70.0 | `low_arv_confidence` |
| 5 | Repair estimate present | rehab.total_cost > 0 | `missing_repair_estimate` |
| 6 | Minimum profit | estimated_profit ≥ $20,000 | `insufficient_profit` |
| 7 | MAO rule | list_price ≤ MAO × 1.10 | `failed_mao_rule` |

---

## 6. Daily Report Format

### JSON Report (`leads_YYYY-MM-DD.json`)
```json
{
  "report_type": "daily_lead_scanner",
  "run_id": "...",
  "run_date": "2026-06-17",
  "generated_at": "2026-06-17T06:42:00Z",
  "stop_condition": "qualified_limit",
  "summary": {
    "iterations": 4,
    "candidates_found": 200,
    "candidates_checked": 187,
    "leads_accepted": 25,
    "leads_rejected": 162,
    "acceptance_rate_pct": 13.4
  },
  "rejection_breakdown": {
    "insufficient_comps": 48,
    "low_arv_confidence": 35,
    "insufficient_profit": 42,
    "failed_mao_rule": 18,
    "missing_address": 12,
    "duplicate": 5,
    "missing_repair_estimate": 2
  },
  "scanner_config": { ... },
  "leads": [
    {
      "property_id": "...",
      "address": { "street": "123 Oak St", "city": "Atlanta", ... },
      "property_details": { "bedrooms": 3, "sqft": 1400, ... },
      "distress": { "types": ["pre_foreclosure","tax_lien"], "score": 75 },
      "arv": { "value": 185000, "confidence": "high", "comp_count": 6 },
      "rehab": { "level": "moderate", "total_cost": 38000 },
      "deal": {
        "list_price": 100000,
        "max_allowable_offer": 91500,
        "estimated_profit": 27300,
        "roi_pct": 21.4
      },
      "score": { "total": 71.3, "breakdown": { ... } }
    }
  ]
}
```

### HTML Report
Dark-themed investor dashboard with:
- Summary stat strip (leads, checked, profit pool, avg ROI, acceptance rate)
- Rejection breakdown table
- Property cards sorted by score, each showing:
  - Address, distress badges, score circle (color-coded)
  - 6-metric grid: ARV, list price, rehab, profit, MAO, holding cost
  - Score bar chart per component

---

## 7. Deployment Plan

### Phase 1 — Local / Dev (immediate)
```bash
# Install dependencies
cd backend
pip install -e ".[test]"

# Run scanner directly
python -m app.scanner.run_scan --max-leads 5

# Run tests
pytest tests/test_scanner.py -v --cov=app

# Start API server
uvicorn app.main:app --reload
```

### Phase 2 — Docker Compose (staging)
```yaml
# docker-compose.yml
services:
  api:
    build: ./backend
    ports: ["8000:8000"]
    environment:
      - SCANNER_DATABASE_URL=postgresql+asyncpg://rei:rei@db/rei_platform
      - SCANNER_SIMULATE_DATA=true
    depends_on: [db, redis]

  worker:
    build: ./backend
    command: celery -A app.workers.daily_scan worker -l info
    depends_on: [db, redis]

  beat:
    build: ./backend
    command: celery -A app.workers.daily_scan beat -l info
    depends_on: [redis]

  db:
    image: postgis/postgis:16-3.4
    environment:
      POSTGRES_DB: rei_platform
      POSTGRES_PASSWORD: rei

  redis:
    image: redis:7-alpine
```

### Phase 3 — AWS Production
```
ECS Fargate:
  - api service (2 tasks, auto-scale to 10)
  - worker-scanner (1 task, scales to 3 during runs)
  - beat (1 task, singleton)

RDS PostgreSQL 16 Multi-AZ (db.r6g.large)
ElastiCache Redis 7 (cache.t4g.medium)
S3 bucket: rei-scanner-reports-{env}
CloudWatch Events: backup schedule trigger (cron(0 6 * * ? *))
```

### CI/CD (GitHub Actions)
```yaml
on: push
jobs:
  test:
    - pytest --cov=app --cov-fail-under=80
  deploy:
    - docker build + push to ECR
    - ecs update-service --force-new-deployment
```

---

## 8. Scanner Run — State Record

At every point the scanner maintains:

```python
ScannerRunState:
  run_id                UUID           # unique run identifier
  run_date              date           # date of execution
  iteration             int            # current loop iteration (max 8)
  candidates_found      int            # total discovered (max 500)
  candidates_checked    int            # evaluated through pipeline
  leads_accepted        int            # passed all 7 verification rules
  leads_rejected        int            # failed at any rule
  checked_property_ids  set[str]       # fingerprints seen this run (dedup)
  accepted_leads        list           # full pipeline results
  rejected_leads        list           # (result, reason) tuples
  rejection_breakdown   dict[str,int]  # counts per rejection reason
  stop_reason           str            # which stop condition fired
  started_at            datetime
```

Persisted to `scanner_state` table:
```json
{
  "checked_property_ids": ["abc123", "def456", ...],
  "accepted_lead_ids": ["prop-uuid-1", ...],
  "last_run_date": "2026-06-17",
  "total_runs": 14,
  "total_leads_all_time": 287
}
```

---

## 9. Future Improvements

### Near-term (v1.1)

| Improvement | Impact |
|-------------|--------|
| **Real data adapters** — ATTOM, county scraper, MLS feed | Eliminates simulation; production-grade lead quality |
| **Owner skip tracing** — integrate BatchSkipTracing API | Enables direct mail / outreach campaigns |
| **Actual MLS comps** — pull from RESO Web API or ATTOM | Higher ARV accuracy; reduces INSUFFICIENT_COMPS rejections |
| **Parallel property evaluation** — `asyncio.gather` per batch | 5-10× throughput for large batches |
| **Persistent state in PostgreSQL** — replace in-memory store | Cross-run dedup, restartable runs |

### Medium-term (v1.2)

| Improvement | Impact |
|-------------|--------|
| **ML-based comp selection** — embedding similarity on property features | Better ARV accuracy vs. rules-based filtering |
| **Condition assessment from photos** — vision model on Zillow/property images | Replaces year-built heuristic with actual visual condition |
| **Automated valuation model (AVM)** — train on local sales history | First-party ARV; not dependent on comp count |
| **Email/SMS report delivery** — send digest to investor list | Distribution without dashboard login |
| **Multi-market config** — per-market MAO multipliers and cost tables | Accurate estimates across diverse geographies |

### Long-term (v2.0)

| Improvement | Impact |
|-------------|--------|
| **Offer price recommendation** — ML model trained on closed deal outcomes | Optimal pricing beyond static 70% rule |
| **Seller motivation scoring** — combine distress signals with demographic and behavioral data | Prioritize properties with most motivated sellers |
| **Deal pipeline CRM integration** — push accepted leads to HubSpot/Salesforce | No manual hand-off from lead to deal tracking |
| **Reinforcement learning scanner** — agent learns which property types produce closed deals | Continuous improvement of lead quality over time |
