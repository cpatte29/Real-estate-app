# Product Requirements Document: Real Estate Investor Lead-Generation Platform

**Version:** 1.0
**Date:** 2026-06-16
**Status:** Draft

---

## 1. Overview

### 1.1 Problem Statement

Real estate investors spend significant time manually searching for distressed properties, estimating repair costs, calculating after-repair values (ARV), and determining profit potential. Data is fragmented across county records, MLS listings, contractor estimates, and mapping tools, making the deal-sourcing process slow and error-prone.

### 1.2 Product Vision

A unified platform that automatically identifies distressed property opportunities, calculates investment metrics (ARV, rehab costs, potential profit), and presents actionable leads on an interactive map — enabling investors to find, evaluate, and act on deals faster than competitors.

### 1.3 Target Users

| Persona | Description |
|---------|-------------|
| **Fix-and-Flip Investor** | Buys distressed properties, renovates, and resells for profit. Needs accurate ARV and rehab estimates. |
| **Buy-and-Hold Investor** | Acquires undervalued rental properties. Needs cash-flow projections alongside deal metrics. |
| **Wholesaler** | Finds deals and assigns contracts. Needs high lead volume and fast property evaluation. |
| **Real Estate Agent (Investor-Focused)** | Serves investor clients and needs deal-sourcing tools to provide value. |

---

## 2. User Stories

### 2.1 Property Discovery

- **US-1:** As an investor, I want to view distressed properties on a map so I can identify opportunities in my target neighborhoods.
- **US-2:** As an investor, I want to filter properties by distress indicators (pre-foreclosure, tax lien, vacancy, code violations) so I can focus on the most motivated sellers.
- **US-3:** As an investor, I want to set geographic boundaries (city, zip, custom polygon) so I only see deals in my market.
- **US-4:** As an investor, I want to save searches and receive alerts when new matching properties appear.

### 2.2 Property Valuation

- **US-5:** As an investor, I want the platform to calculate ARV using comparable recent sales so I can trust the projected value.
- **US-6:** As an investor, I want to see the comparable properties used in the ARV calculation so I can verify accuracy.
- **US-7:** As an investor, I want to adjust comp filters (radius, date range, sq ft tolerance) so the ARV reflects my local expertise.
- **US-8:** As an investor, I want to view price-per-square-foot trends for a neighborhood so I can assess market direction.

### 2.3 Rehab Cost Estimation

- **US-9:** As an investor, I want an automated rehab cost estimate based on property condition and scope of work so I can quickly evaluate deals.
- **US-10:** As an investor, I want to select from predefined rehab levels (cosmetic, moderate, full gut) so I get a fast ballpark estimate.
- **US-11:** As an investor, I want to customize line-item rehab costs (kitchen, bathroom, roof, HVAC, etc.) so I can refine the estimate.
- **US-12:** As an investor, I want rehab cost estimates adjusted for my local market so numbers are realistic.

### 2.4 Profit Analysis

- **US-13:** As an investor, I want to see estimated profit calculated as ARV minus purchase price, rehab costs, holding costs, and transaction costs.
- **US-14:** As an investor, I want to input my own purchase price and financing terms so I can model different offer scenarios.
- **US-15:** As an investor, I want to see ROI and cash-on-cash return so I can compare deals.
- **US-16:** As a wholesaler, I want to estimate assignment fee potential based on the spread between asking price and ARV minus rehab.

### 2.5 Map and Visualization

- **US-17:** As an investor, I want properties color-coded on the map by deal quality (profit margin) so I can spot the best opportunities at a glance.
- **US-18:** As an investor, I want to click a map pin and see a summary card with key metrics (ARV, rehab estimate, profit, photos).
- **US-19:** As an investor, I want to draw a custom area on the map and see all distressed properties within it.
- **US-20:** As an investor, I want to view heat maps of foreclosure density, price trends, and rental demand.

### 2.6 Account and Workflow

- **US-21:** As an investor, I want to save properties to a watchlist so I can track deals over time.
- **US-22:** As an investor, I want to export a deal analysis as a PDF so I can share it with partners or lenders.
- **US-23:** As an investor, I want to add notes and a status (researching, offer made, under contract) to saved properties.
- **US-24:** As a team lead, I want to share saved properties and lists with team members.

---

## 3. MVP Features (v1.0)

### 3.1 Core Feature Set

| # | Feature | Priority | Description |
|---|---------|----------|-------------|
| F1 | **Distressed Property Ingestion** | P0 | Aggregate pre-foreclosure, tax lien, and auction data from public records. |
| F2 | **Interactive Map** | P0 | Display properties on a map with clustering, zoom, and pan. Property pins with color coding by deal score. |
| F3 | **ARV Calculator** | P0 | Automated comp-based ARV using recent sales within configurable radius, date range, and property similarity filters. |
| F4 | **Rehab Cost Estimator** | P0 | Preset rehab tiers (cosmetic/moderate/full) with per-square-foot cost defaults. User-adjustable line items. |
| F5 | **Profit Calculator** | P0 | Computes: `Profit = ARV - Purchase Price - Rehab - Holding Costs - Transaction Costs`. Configurable inputs for financing, holding period, closing costs. |
| F6 | **Property Detail View** | P0 | Property attributes, photos (when available), distress indicators, owner info, tax history, and calculated metrics. |
| F7 | **Search and Filters** | P0 | Filter by location, property type, distress type, price range, bedrooms/bathrooms, lot size, equity percentage. |
| F8 | **User Authentication** | P0 | Email/password and OAuth (Google) registration and login. |
| F9 | **Saved Searches and Alerts** | P1 | Save filter configurations. Email notifications for new matching properties (daily digest). |
| F10 | **Watchlist** | P1 | Save individual properties. Add notes, status labels, and custom tags. |
| F11 | **Deal Export (PDF)** | P1 | Export property analysis with metrics, comps, and map screenshot to PDF. |

### 3.2 Out of Scope for MVP

- Direct mail / marketing campaign management
- CRM with call tracking and follow-up sequences
- Rental cash-flow analysis and cap rate calculations
- Mobile native apps (responsive web only for MVP)
- MLS data integration (requires brokerage partnerships)
- Contractor marketplace or bid management
- AI-driven offer price recommendation

---

## 4. Database Requirements

### 4.1 Entity-Relationship Summary

```
Users ──< SavedSearches
Users ──< Watchlists ──< WatchlistItems >── Properties
Properties ──< DistressIndicators
Properties ──< Comparables
Properties ──< RehabEstimates
Properties ──< DealAnalyses
```

### 4.2 Core Tables

#### `users`
| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| email | VARCHAR(255) | UNIQUE, NOT NULL |
| password_hash | VARCHAR(255) | NOT NULL |
| full_name | VARCHAR(255) | |
| created_at | TIMESTAMP | DEFAULT NOW() |
| updated_at | TIMESTAMP | |
| subscription_tier | ENUM('free','pro','team') | DEFAULT 'free' |
| settings_json | JSONB | |

#### `properties`
| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| address | VARCHAR(500) | NOT NULL |
| city | VARCHAR(100) | |
| state | VARCHAR(2) | |
| zip | VARCHAR(10) | |
| county | VARCHAR(100) | |
| latitude | DECIMAL(10,7) | NOT NULL |
| longitude | DECIMAL(10,7) | NOT NULL |
| property_type | ENUM('SFR','multi_family','condo','townhouse','land') | |
| bedrooms | SMALLINT | |
| bathrooms | DECIMAL(3,1) | |
| sqft | INT | |
| lot_sqft | INT | |
| year_built | SMALLINT | |
| assessed_value | DECIMAL(12,2) | |
| tax_amount | DECIMAL(10,2) | |
| owner_name | VARCHAR(255) | |
| owner_mailing_address | VARCHAR(500) | |
| owner_occupied | BOOLEAN | |
| last_sale_date | DATE | |
| last_sale_price | DECIMAL(12,2) | |
| estimated_equity_pct | DECIMAL(5,2) | |
| data_source | VARCHAR(50) | |
| source_record_id | VARCHAR(100) | |
| raw_data_json | JSONB | |
| created_at | TIMESTAMP | DEFAULT NOW() |
| updated_at | TIMESTAMP | |

**Indexes:** `(latitude, longitude)` via PostGIS GIST index, `(state, city)`, `(zip)`, `(property_type)`.

#### `distress_indicators`
| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| property_id | UUID | FK → properties |
| indicator_type | ENUM('pre_foreclosure','tax_lien','auction','code_violation','vacancy','probate','bankruptcy','divorce') | NOT NULL |
| status | VARCHAR(50) | |
| recorded_date | DATE | |
| amount_owed | DECIMAL(12,2) | |
| source_url | TEXT | |
| raw_data_json | JSONB | |
| created_at | TIMESTAMP | DEFAULT NOW() |

#### `comparables`
| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| subject_property_id | UUID | FK → properties |
| comp_property_id | UUID | FK → properties |
| sale_date | DATE | NOT NULL |
| sale_price | DECIMAL(12,2) | NOT NULL |
| distance_miles | DECIMAL(5,2) | |
| similarity_score | DECIMAL(5,2) | 0-100 score |
| price_per_sqft | DECIMAL(8,2) | |
| adjustments_json | JSONB | |
| created_at | TIMESTAMP | DEFAULT NOW() |

#### `rehab_estimates`
| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| property_id | UUID | FK → properties |
| user_id | UUID | FK → users (NULL for system estimates) |
| rehab_level | ENUM('cosmetic','moderate','full') | |
| total_cost | DECIMAL(12,2) | NOT NULL |
| cost_per_sqft | DECIMAL(8,2) | |
| line_items_json | JSONB | |
| created_at | TIMESTAMP | DEFAULT NOW() |

`line_items_json` schema:
```json
[
  { "category": "kitchen", "description": "Full remodel", "cost": 15000 },
  { "category": "bathroom", "description": "Cosmetic update x2", "cost": 8000 },
  { "category": "flooring", "description": "LVP throughout", "cost": 6000 }
]
```

#### `deal_analyses`
| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| property_id | UUID | FK → properties |
| user_id | UUID | FK → users |
| purchase_price | DECIMAL(12,2) | |
| arv | DECIMAL(12,2) | |
| rehab_cost | DECIMAL(12,2) | |
| holding_cost | DECIMAL(12,2) | |
| closing_cost_buy | DECIMAL(12,2) | |
| closing_cost_sell | DECIMAL(12,2) | |
| agent_commission_pct | DECIMAL(4,2) | |
| estimated_profit | DECIMAL(12,2) | |
| roi_pct | DECIMAL(6,2) | |
| financing_json | JSONB | |
| notes | TEXT | |
| status | ENUM('researching','offer_made','under_contract','closed','passed') | DEFAULT 'researching' |
| created_at | TIMESTAMP | DEFAULT NOW() |
| updated_at | TIMESTAMP | |

#### `saved_searches`
| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| user_id | UUID | FK → users |
| name | VARCHAR(255) | |
| filters_json | JSONB | NOT NULL |
| alert_frequency | ENUM('instant','daily','weekly','none') | DEFAULT 'daily' |
| last_alerted_at | TIMESTAMP | |
| created_at | TIMESTAMP | DEFAULT NOW() |

#### `watchlist_items`
| Column | Type | Constraints |
|--------|------|-------------|
| id | UUID | PK |
| user_id | UUID | FK → users |
| property_id | UUID | FK → properties |
| tags | TEXT[] | |
| notes | TEXT | |
| created_at | TIMESTAMP | DEFAULT NOW() |

**Unique constraint:** `(user_id, property_id)`.

### 4.3 Database Technology

- **Primary DB:** PostgreSQL 16 with PostGIS extension for geospatial queries.
- **Cache:** Redis for session management, rate limiting, and caching frequently queried map tiles / property clusters.
- **Search:** PostgreSQL full-text search for MVP; Elasticsearch for future scale.

---

## 5. API Requirements

### 5.1 General

- RESTful JSON API
- Base URL: `/api/v1`
- Authentication: JWT Bearer tokens
- Rate limiting: 100 requests/min (free), 1000 requests/min (pro)
- Pagination: cursor-based for list endpoints
- Error format: `{ "error": { "code": "string", "message": "string" } }`

### 5.2 Endpoints

#### Authentication

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/auth/register` | Create account (email, password, name) |
| POST | `/auth/login` | Returns JWT access + refresh tokens |
| POST | `/auth/refresh` | Exchange refresh token for new access token |
| POST | `/auth/oauth/google` | OAuth login via Google |
| POST | `/auth/password-reset` | Initiate password reset email |

#### Properties

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/properties` | List properties with filters (query params below) |
| GET | `/properties/:id` | Full property detail with distress indicators |
| GET | `/properties/map` | Clustered properties for map viewport (bounds, zoom) |
| GET | `/properties/:id/comps` | Comparable sales for a property |
| GET | `/properties/:id/photos` | Property photos |

**Filter query parameters for `GET /properties`:**
- `bounds` — `sw_lat,sw_lng,ne_lat,ne_lng`
- `zip`, `city`, `state`, `county`
- `property_type` — comma-separated
- `distress_type` — comma-separated
- `min_price`, `max_price`
- `min_beds`, `max_beds`, `min_baths`, `max_baths`
- `min_sqft`, `max_sqft`
- `min_equity_pct`
- `sort` — `newest`, `price_asc`, `price_desc`, `profit_desc`
- `cursor`, `limit`

#### ARV Calculation

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/properties/:id/arv` | Get calculated ARV with default comp parameters |
| POST | `/properties/:id/arv` | Calculate ARV with custom parameters |

**POST body:**
```json
{
  "radius_miles": 0.5,
  "date_range_months": 6,
  "sqft_tolerance_pct": 20,
  "property_types": ["SFR"],
  "min_comps": 3,
  "max_comps": 10
}
```

**Response:**
```json
{
  "arv": 285000,
  "confidence": "high",
  "comp_count": 6,
  "price_per_sqft_avg": 178.50,
  "comps": [ { "address": "...", "sale_price": 275000, "sale_date": "2026-03-15", "sqft": 1450, "distance_miles": 0.3, "similarity_score": 88 } ]
}
```

#### Rehab Estimates

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/properties/:id/rehab` | Get system-generated rehab estimates (all tiers) |
| POST | `/properties/:id/rehab` | Create/update custom rehab estimate |

#### Deal Analysis

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/properties/:id/analyze` | Run full deal analysis |
| GET | `/analyses` | List user's saved analyses |
| GET | `/analyses/:id` | Get specific analysis detail |
| PUT | `/analyses/:id` | Update analysis (purchase price, status, notes) |
| DELETE | `/analyses/:id` | Delete analysis |
| GET | `/analyses/:id/export` | Export analysis as PDF |

**POST `/properties/:id/analyze` body:**
```json
{
  "purchase_price": 150000,
  "rehab_level": "moderate",
  "custom_rehab_cost": null,
  "holding_months": 6,
  "financing": {
    "type": "conventional",
    "down_payment_pct": 20,
    "interest_rate_pct": 7.5,
    "loan_term_months": 360
  },
  "closing_cost_buy_pct": 2,
  "closing_cost_sell_pct": 2,
  "agent_commission_pct": 5
}
```

#### Saved Searches

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/saved-searches` | List user's saved searches |
| POST | `/saved-searches` | Create saved search with alert preferences |
| PUT | `/saved-searches/:id` | Update saved search |
| DELETE | `/saved-searches/:id` | Delete saved search |

#### Watchlist

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/watchlist` | List watchlist items |
| POST | `/watchlist` | Add property to watchlist |
| PUT | `/watchlist/:id` | Update notes/tags |
| DELETE | `/watchlist/:id` | Remove from watchlist |

### 5.3 External Data Sources (Ingestion APIs)

| Source | Data | Integration Method |
|--------|------|--------------------|
| County recorder / assessor | Property records, tax liens, ownership | Batch ETL from public data portals (CKAN, Socrata) |
| Foreclosure listings | Pre-foreclosure, auction dates | API or scraping (ATTOM, PropertyShark, or public court records) |
| ATTOM Data / CoreLogic | Property attributes, AVM, sales history | REST API subscription |
| Census / ACS | Demographics, income data for neighborhood scoring | Public API |
| Google Maps / Mapbox | Geocoding, map tiles, Street View imagery | Client-side SDK + server geocoding API |
| USPS | Vacancy indicators (mail forwarding, no-stat) | Batch data licensing |

---

## 6. Non-Functional Requirements

| Category | Requirement |
|----------|-------------|
| **Performance** | Map loads in < 2s. Property search results in < 1s. ARV calculation in < 3s. |
| **Scalability** | Support 10k concurrent users at launch. Horizontal scaling for API and worker tiers. |
| **Availability** | 99.5% uptime SLA. |
| **Security** | HTTPS only. Passwords hashed with bcrypt. JWT with short-lived access tokens (15 min). OWASP Top 10 compliance. SOC 2 readiness. |
| **Data Freshness** | Distress data updated daily. Sales comps updated within 48 hours of recording. |
| **Browser Support** | Chrome, Firefox, Safari, Edge (latest 2 versions). Responsive down to 768px. |

---

## 7. Technical Architecture (Recommended)

```
┌──────────────┐     ┌──────────────┐     ┌──────────────────┐
│   React SPA  │────▶│   API Server │────▶│   PostgreSQL     │
│  + Mapbox GL │     │  (Node/Express│     │   + PostGIS      │
└──────────────┘     │   or FastAPI) │     └──────────────────┘
                     └──────┬───────┘              │
                            │                      │
                     ┌──────▼───────┐     ┌────────▼─────────┐
                     │  Redis Cache │     │  Background Jobs  │
                     │              │     │  (data ingestion, │
                     └──────────────┘     │   alerts, PDF gen)│
                                          └──────────────────┘
```

- **Frontend:** React + TypeScript, Mapbox GL JS, TailwindCSS
- **Backend:** Node.js/Express or Python/FastAPI
- **Database:** PostgreSQL 16 + PostGIS
- **Cache/Queue:** Redis + Bull (Node) or Celery (Python)
- **Hosting:** AWS (ECS/Fargate or EKS), RDS for PostgreSQL
- **CI/CD:** GitHub Actions

---

## 8. Success Metrics

| Metric | Target (6 months post-launch) |
|--------|-------------------------------|
| Registered users | 5,000 |
| Weekly active users | 1,500 |
| Properties in database | 500,000+ |
| Analyses created per week | 3,000 |
| Free → Pro conversion rate | 8% |
| ARV accuracy (within 10% of actual sale) | 75% of calculated ARVs |

---

## 9. Future Roadmap

### v1.1 — Enhanced Analytics
- Rental cash-flow calculator (cap rate, cash-on-cash, DSCR)
- Neighborhood scoring (schools, crime, walkability, appreciation trends)
- Batch analysis — evaluate multiple properties at once

### v1.2 — Marketing and CRM
- Direct mail campaign builder (skip trace, letter templates, mail merge)
- Built-in CRM with lead status pipeline
- Call tracking with disposition logging
- SMS/email drip campaigns for seller outreach

### v1.3 — Mobile and Collaboration
- Native iOS and Android apps with GPS-based "drive for dollars" mode
- Team workspaces with shared watchlists and role-based permissions
- In-app messaging between team members

### v2.0 — Intelligence Layer
- ML-based deal scoring (predict likelihood of seller motivation and deal close)
- AI-powered offer price recommendation based on local market dynamics
- Automated comp selection using ML similarity matching
- Computer vision for property condition assessment from photos
- Predictive analytics for neighborhood appreciation

### v2.1 — Marketplace and Integrations
- Contractor bid marketplace for rehab estimates
- Integration with investor-friendly lenders for pre-qualification
- MLS data integration via brokerage partnerships
- Title company integration for closing workflow
- Zapier/webhook integrations for external workflow automation

---

## 10. Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Public record data quality varies by county | Inaccurate property data | Multi-source validation; user-reported corrections; confidence scores on data fields |
| ARV calculations may be inaccurate in thin markets | Loss of user trust | Show confidence level based on comp count; require minimum 3 comps; allow manual adjustment |
| Data licensing costs for premium sources (ATTOM, CoreLogic) | High operational costs | Start with public records only; add paid sources as revenue grows; negotiate volume pricing |
| Competitor platforms (PropStream, DealMachine, BatchLeads) | User acquisition difficulty | Differentiate on UX, pricing, and accuracy; focus on underserved mid-market investors |
| Regulatory changes to foreclosure data access | Reduced data availability | Diversify distress indicators beyond foreclosure; monitor legislative changes |

---

## Appendix A: Rehab Cost Defaults (per sq ft, national median)

| Rehab Level | Cost/sqft | Typical Scope |
|-------------|-----------|---------------|
| Cosmetic | $15–25 | Paint, flooring, fixtures, landscaping |
| Moderate | $25–50 | Cosmetic + kitchen/bath remodel, some mechanical |
| Full | $50–100+ | Down to studs: new electrical, plumbing, HVAC, layout changes |

Regional multipliers applied based on metro area cost-of-living index.

## Appendix B: ARV Calculation Method

1. Identify sold properties within configurable radius (default 0.5 mi) and date range (default 6 months).
2. Filter for similar property type, bedroom count (±1), square footage (±20%).
3. Score each comp on similarity (distance, size, age, condition, lot size).
4. Apply adjustments for differences (e.g., +$5k per extra bedroom, -$3k for no garage).
5. Compute weighted average of adjusted comp prices, weighted by similarity score.
6. Assign confidence level: High (6+ comps), Medium (3–5 comps), Low (< 3 comps).

## Appendix C: Deal Analysis Formula

```
Estimated Profit = ARV
                 - Purchase Price
                 - Rehab Cost
                 - Holding Costs (mortgage payments × months + insurance + taxes + utilities)
                 - Buying Closing Costs (purchase_price × closing_cost_buy_pct)
                 - Selling Closing Costs (ARV × closing_cost_sell_pct)
                 - Agent Commission (ARV × agent_commission_pct)

ROI = Estimated Profit / Total Cash Invested × 100

Total Cash Invested = Down Payment + Buying Closing Costs + Rehab Cost + Holding Costs
```
