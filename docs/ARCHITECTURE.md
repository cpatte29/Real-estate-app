# Architecture Design Document: Real Estate Investor SaaS Platform

**Version:** 1.0
**Date:** 2026-06-17
**Status:** Draft

---

## 1. System Architecture Overview

```
                                    ┌─────────────────┐
                                    │   CloudFront     │
                                    │   CDN            │
                                    └────────┬────────┘
                                             │
                              ┌──────────────┼──────────────┐
                              │              │              │
                     ┌────────▼───────┐      │     ┌────────▼────────┐
                     │  React SPA     │      │     │  Static Assets  │
                     │  (S3 Bucket)   │      │     │  (S3 Bucket)    │
                     └────────┬───────┘      │     └─────────────────┘
                              │              │
                              ▼              ▼
                     ┌─────────────────────────────────┐
                     │     Application Load Balancer    │
                     │     (SSL Termination)            │
                     └──────────────┬──────────────────┘
                                    │
                     ┌──────────────┼──────────────┐
                     │              │              │
              ┌──────▼──────┐ ┌────▼─────┐ ┌──────▼──────┐
              │  FastAPI    │ │ FastAPI  │ │  FastAPI    │
              │  Instance 1 │ │ Inst. 2  │ │  Instance N │
              │  (ECS Task) │ │(ECS Task)│ │  (ECS Task) │
              └──────┬──────┘ └────┬─────┘ └──────┬──────┘
                     │             │              │
          ┌──────────┼─────────────┼──────────────┤
          │          │             │              │
   ┌──────▼──────┐ ┌─▼───────┐ ┌──▼──────┐ ┌────▼────────┐
   │ PostgreSQL  │ │  Redis  │ │   S3    │ │  SQS/Worker │
   │ RDS (Primary│ │ElastiCa.│ │ Storage │ │  (Celery)   │
   │  + Replica) │ │         │ │         │ │             │
   └─────────────┘ └─────────┘ └─────────┘ └─────────────┘
```

---

## 2. Technology Stack

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| **Frontend** | React 18, TypeScript, Vite | Type safety, fast builds, large ecosystem |
| **Maps** | Mapbox GL JS | Vector tiles, custom styling, clustering, drawing tools |
| **UI Framework** | TailwindCSS + shadcn/ui | Rapid development, consistent design, accessible components |
| **State Management** | TanStack Query + Zustand | Server state caching with minimal client state |
| **Backend** | FastAPI (Python 3.12) | Async-native, auto-generated OpenAPI docs, Pydantic validation |
| **ORM** | SQLAlchemy 2.0 + Alembic | Async support, mature migration system |
| **Database** | PostgreSQL 16 + PostGIS 3.4 | Geospatial queries, JSONB, CTEs, window functions |
| **Cache** | Redis 7 | Session store, rate limiting, map tile cache, pub/sub |
| **Task Queue** | Celery + SQS | Background jobs: data ingestion, alerts, PDF generation |
| **Search** | PostgreSQL FTS (MVP) → OpenSearch | Full-text property search with geo filtering |
| **Auth** | JWT (access + refresh) + OAuth 2.0 | Stateless auth, third-party login |
| **Storage** | S3 | Property photos, PDF exports, data file uploads |
| **Infrastructure** | AWS ECS Fargate, RDS, ElastiCache | Serverless containers, managed DB, no server maintenance |
| **CI/CD** | GitHub Actions | Build, test, deploy pipeline |
| **Monitoring** | Datadog / CloudWatch + Sentry | APM, log aggregation, error tracking |

---

## 3. Database Schema

### 3.1 Extensions

```sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "postgis";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";  -- fuzzy text search
```

### 3.2 Complete Schema

```sql
-- ============================================================
-- ENUMS
-- ============================================================

CREATE TYPE subscription_tier AS ENUM ('free', 'pro', 'team');
CREATE TYPE property_type AS ENUM ('sfr', 'multi_family', 'condo', 'townhouse', 'land');
CREATE TYPE distress_type AS ENUM (
    'pre_foreclosure', 'tax_lien', 'auction', 'code_violation',
    'vacancy', 'probate', 'bankruptcy', 'divorce'
);
CREATE TYPE rehab_level AS ENUM ('cosmetic', 'moderate', 'full');
CREATE TYPE deal_status AS ENUM (
    'new', 'researching', 'offer_made', 'under_contract',
    'closed_won', 'closed_lost', 'passed'
);
CREATE TYPE alert_frequency AS ENUM ('instant', 'daily', 'weekly', 'none');
CREATE TYPE arv_confidence AS ENUM ('high', 'medium', 'low');

-- ============================================================
-- USERS & AUTH
-- ============================================================

CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email           VARCHAR(255) NOT NULL UNIQUE,
    password_hash   VARCHAR(255),
    full_name       VARCHAR(255) NOT NULL,
    avatar_url      VARCHAR(500),
    subscription_tier subscription_tier NOT NULL DEFAULT 'free',
    stripe_customer_id VARCHAR(100),
    is_active       BOOLEAN NOT NULL DEFAULT true,
    email_verified  BOOLEAN NOT NULL DEFAULT false,
    settings        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE oauth_accounts (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider        VARCHAR(50) NOT NULL,  -- 'google', 'apple'
    provider_user_id VARCHAR(255) NOT NULL,
    access_token    TEXT,
    refresh_token   TEXT,
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (provider, provider_user_id)
);

CREATE INDEX idx_oauth_accounts_user_id ON oauth_accounts(user_id);

CREATE TABLE refresh_tokens (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash      VARCHAR(255) NOT NULL UNIQUE,
    device_info     VARCHAR(500),
    expires_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_refresh_tokens_user_id ON refresh_tokens(user_id);
CREATE INDEX idx_refresh_tokens_expires ON refresh_tokens(expires_at);

-- ============================================================
-- PROPERTIES
-- ============================================================

CREATE TABLE properties (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- Address
    street_address      VARCHAR(500) NOT NULL,
    city                VARCHAR(100) NOT NULL,
    state               VARCHAR(2) NOT NULL,
    zip                 VARCHAR(10) NOT NULL,
    county              VARCHAR(100),
    apn                 VARCHAR(50),  -- assessor parcel number
    -- Geolocation
    location            GEOGRAPHY(POINT, 4326) NOT NULL,
    -- Attributes
    property_type       property_type,
    bedrooms            SMALLINT,
    bathrooms           DECIMAL(3,1),
    sqft                INTEGER,
    lot_sqft            INTEGER,
    year_built          SMALLINT,
    stories             SMALLINT,
    garage_spaces       SMALLINT DEFAULT 0,
    pool                BOOLEAN DEFAULT false,
    -- Valuation
    assessed_value      DECIMAL(12,2),
    estimated_value     DECIMAL(12,2),
    tax_annual          DECIMAL(10,2),
    -- Ownership
    owner_name          VARCHAR(255),
    owner_mailing_addr  VARCHAR(500),
    owner_occupied      BOOLEAN,
    ownership_length_yrs DECIMAL(4,1),
    -- Sales History
    last_sale_date      DATE,
    last_sale_price     DECIMAL(12,2),
    estimated_equity_pct DECIMAL(5,2),
    -- Metadata
    data_source         VARCHAR(50) NOT NULL,
    source_record_id    VARCHAR(100),
    raw_data            JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_properties_location ON properties USING GIST(location);
CREATE INDEX idx_properties_state_city ON properties(state, city);
CREATE INDEX idx_properties_zip ON properties(zip);
CREATE INDEX idx_properties_type ON properties(property_type);
CREATE INDEX idx_properties_source ON properties(data_source, source_record_id);
CREATE INDEX idx_properties_address_trgm ON properties
    USING GIN(street_address gin_trgm_ops);

-- ============================================================
-- DISTRESS INDICATORS
-- ============================================================

CREATE TABLE distress_indicators (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id     UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    indicator_type  distress_type NOT NULL,
    status          VARCHAR(50),
    recorded_date   DATE,
    amount_owed     DECIMAL(12,2),
    auction_date    DATE,
    case_number     VARCHAR(100),
    source_url      TEXT,
    raw_data        JSONB,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_distress_property ON distress_indicators(property_id);
CREATE INDEX idx_distress_type ON distress_indicators(indicator_type);
CREATE INDEX idx_distress_active ON distress_indicators(is_active)
    WHERE is_active = true;

-- ============================================================
-- COMPARABLE SALES
-- ============================================================

CREATE TABLE sales_history (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id     UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    sale_date       DATE NOT NULL,
    sale_price      DECIMAL(12,2) NOT NULL,
    price_per_sqft  DECIMAL(8,2),
    buyer_name      VARCHAR(255),
    seller_name     VARCHAR(255),
    sale_type       VARCHAR(50),  -- 'arms_length', 'foreclosure', 'short_sale'
    data_source     VARCHAR(50),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_sales_property ON sales_history(property_id);
CREATE INDEX idx_sales_date ON sales_history(sale_date DESC);

CREATE TABLE arv_calculations (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    subject_property_id UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    user_id             UUID REFERENCES users(id) ON DELETE SET NULL,
    -- Parameters
    radius_miles        DECIMAL(4,2) NOT NULL DEFAULT 0.5,
    date_range_months   SMALLINT NOT NULL DEFAULT 6,
    sqft_tolerance_pct  SMALLINT NOT NULL DEFAULT 20,
    -- Results
    arv                 DECIMAL(12,2) NOT NULL,
    confidence          arv_confidence NOT NULL,
    price_per_sqft_avg  DECIMAL(8,2),
    comp_count          SMALLINT NOT NULL,
    -- Comp details stored as JSONB for query efficiency
    comps_json          JSONB NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_arv_subject ON arv_calculations(subject_property_id);
CREATE INDEX idx_arv_user ON arv_calculations(user_id);

-- ============================================================
-- REHAB ESTIMATES
-- ============================================================

CREATE TABLE rehab_estimates (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id     UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    user_id         UUID REFERENCES users(id) ON DELETE SET NULL,
    rehab_level     rehab_level NOT NULL,
    total_cost      DECIMAL(12,2) NOT NULL,
    cost_per_sqft   DECIMAL(8,2),
    line_items      JSONB NOT NULL DEFAULT '[]',
    regional_multiplier DECIMAL(4,2) NOT NULL DEFAULT 1.0,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_rehab_property ON rehab_estimates(property_id);
CREATE INDEX idx_rehab_user ON rehab_estimates(user_id);

-- ============================================================
-- DEAL ANALYSES
-- ============================================================

CREATE TABLE deal_analyses (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    property_id         UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- Inputs
    purchase_price      DECIMAL(12,2) NOT NULL,
    arv                 DECIMAL(12,2) NOT NULL,
    rehab_cost          DECIMAL(12,2) NOT NULL DEFAULT 0,
    holding_months      SMALLINT NOT NULL DEFAULT 6,
    -- Holding costs
    monthly_mortgage    DECIMAL(10,2) DEFAULT 0,
    monthly_taxes       DECIMAL(10,2) DEFAULT 0,
    monthly_insurance   DECIMAL(10,2) DEFAULT 0,
    monthly_utilities   DECIMAL(10,2) DEFAULT 0,
    -- Transaction costs
    closing_cost_buy    DECIMAL(10,2) DEFAULT 0,
    closing_cost_sell   DECIMAL(10,2) DEFAULT 0,
    agent_commission    DECIMAL(10,2) DEFAULT 0,
    -- Financing
    financing           JSONB NOT NULL DEFAULT '{}',
    -- Computed
    total_holding_cost  DECIMAL(12,2) NOT NULL,
    total_cost          DECIMAL(12,2) NOT NULL,
    estimated_profit    DECIMAL(12,2) NOT NULL,
    roi_pct             DECIMAL(8,2),
    cash_on_cash_pct    DECIMAL(8,2),
    cash_invested       DECIMAL(12,2),
    -- Status
    status              deal_status NOT NULL DEFAULT 'new',
    deal_score          SMALLINT,  -- 0-100 computed score
    notes               TEXT,
    tags                TEXT[] DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_deals_user ON deal_analyses(user_id);
CREATE INDEX idx_deals_property ON deal_analyses(property_id);
CREATE INDEX idx_deals_status ON deal_analyses(user_id, status);
CREATE INDEX idx_deals_score ON deal_analyses(deal_score DESC NULLS LAST);

-- ============================================================
-- SAVED SEARCHES & WATCHLIST
-- ============================================================

CREATE TABLE saved_searches (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            VARCHAR(255) NOT NULL,
    filters         JSONB NOT NULL,
    alert_frequency alert_frequency NOT NULL DEFAULT 'daily',
    last_alerted_at TIMESTAMPTZ,
    result_count    INTEGER DEFAULT 0,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_saved_searches_user ON saved_searches(user_id);
CREATE INDEX idx_saved_searches_alert ON saved_searches(alert_frequency, last_alerted_at)
    WHERE is_active = true AND alert_frequency != 'none';

CREATE TABLE watchlist_items (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    property_id     UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    notes           TEXT,
    tags            TEXT[] DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, property_id)
);

CREATE INDEX idx_watchlist_user ON watchlist_items(user_id);

-- ============================================================
-- AUDIT & INGESTION TRACKING
-- ============================================================

CREATE TABLE data_ingestion_runs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source          VARCHAR(50) NOT NULL,
    region          VARCHAR(100),
    status          VARCHAR(20) NOT NULL DEFAULT 'running',
    records_fetched INTEGER DEFAULT 0,
    records_created INTEGER DEFAULT 0,
    records_updated INTEGER DEFAULT 0,
    errors          JSONB DEFAULT '[]',
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);

-- ============================================================
-- ROW-LEVEL SECURITY (multi-tenant isolation)
-- ============================================================

ALTER TABLE deal_analyses ENABLE ROW LEVEL SECURITY;
ALTER TABLE watchlist_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE saved_searches ENABLE ROW LEVEL SECURITY;

CREATE POLICY deal_analyses_user_policy ON deal_analyses
    USING (user_id = current_setting('app.current_user_id')::UUID);
CREATE POLICY watchlist_user_policy ON watchlist_items
    USING (user_id = current_setting('app.current_user_id')::UUID);
CREATE POLICY saved_searches_user_policy ON saved_searches
    USING (user_id = current_setting('app.current_user_id')::UUID);

-- ============================================================
-- UPDATED_AT TRIGGER
-- ============================================================

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_users_updated_at BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_properties_updated_at BEFORE UPDATE ON properties
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_distress_updated_at BEFORE UPDATE ON distress_indicators
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_rehab_updated_at BEFORE UPDATE ON rehab_estimates
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_deals_updated_at BEFORE UPDATE ON deal_analyses
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_saved_searches_updated_at BEFORE UPDATE ON saved_searches
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_watchlist_updated_at BEFORE UPDATE ON watchlist_items
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
```

### 3.3 Key Geospatial Queries

```sql
-- Find properties within a map viewport (bounding box)
SELECT p.*, array_agg(DISTINCT di.indicator_type) AS distress_types
FROM properties p
LEFT JOIN distress_indicators di ON di.property_id = p.id AND di.is_active = true
WHERE p.location && ST_MakeEnvelope(:sw_lng, :sw_lat, :ne_lng, :ne_lat, 4326)
GROUP BY p.id
LIMIT :limit OFFSET :offset;

-- Find comparable sales within radius
SELECT p.*, sh.sale_price, sh.sale_date, sh.price_per_sqft,
       ST_Distance(p.location, subject.location) / 1609.34 AS distance_miles
FROM properties p
JOIN sales_history sh ON sh.property_id = p.id
CROSS JOIN (SELECT location FROM properties WHERE id = :subject_id) subject
WHERE ST_DWithin(p.location, subject.location, :radius_meters)
  AND sh.sale_date >= CURRENT_DATE - INTERVAL ':months months'
  AND p.property_type = :property_type
  AND p.sqft BETWEEN :min_sqft AND :max_sqft
  AND p.id != :subject_id
ORDER BY sh.sale_date DESC
LIMIT :max_comps;

-- Cluster properties for map (server-side clustering at low zoom)
SELECT ST_ClusterKMeans(location::geometry, :num_clusters)
           OVER() AS cluster_id,
       id, location
FROM properties
WHERE location && ST_MakeEnvelope(:sw_lng, :sw_lat, :ne_lng, :ne_lat, 4326);
```

---

## 4. Backend Architecture (FastAPI)

### 4.1 Project Structure

```
backend/
├── alembic/                    # Database migrations
│   ├── versions/
│   └── env.py
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app factory
│   ├── config.py               # Settings via pydantic-settings
│   ├── database.py             # Async SQLAlchemy engine & session
│   ├── dependencies.py         # Shared FastAPI dependencies
│   ├── middleware/
│   │   ├── rate_limit.py
│   │   ├── cors.py
│   │   └── request_id.py
│   ├── auth/
│   │   ├── router.py
│   │   ├── service.py
│   │   ├── schemas.py
│   │   ├── jwt.py
│   │   └── oauth.py
│   ├── properties/
│   │   ├── router.py
│   │   ├── service.py
│   │   ├── schemas.py
│   │   └── queries.py
│   ├── arv/
│   │   ├── router.py
│   │   ├── service.py          # Comp selection & ARV calculation
│   │   └── schemas.py
│   ├── rehab/
│   │   ├── router.py
│   │   ├── service.py
│   │   ├── schemas.py
│   │   └── cost_tables.py      # Regional cost data
│   ├── deals/
│   │   ├── router.py
│   │   ├── service.py
│   │   └── schemas.py
│   ├── watchlist/
│   │   ├── router.py
│   │   ├── service.py
│   │   └── schemas.py
│   ├── saved_searches/
│   │   ├── router.py
│   │   ├── service.py
│   │   └── schemas.py
│   ├── export/
│   │   └── pdf_generator.py
│   ├── models/                 # SQLAlchemy models
│   │   ├── user.py
│   │   ├── property.py
│   │   ├── distress.py
│   │   ├── arv.py
│   │   ├── rehab.py
│   │   ├── deal.py
│   │   └── search.py
│   └── workers/
│       ├── celery_app.py
│       ├── tasks/
│       │   ├── ingest_properties.py
│       │   ├── send_alerts.py
│       │   └── generate_pdf.py
│       └── schedules.py
├── tests/
├── Dockerfile
├── pyproject.toml
└── alembic.ini
```

### 4.2 Core Application Setup

```python
# app/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import engine
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_id import RequestIDMiddleware
from app.auth.router import router as auth_router
from app.properties.router import router as properties_router
from app.arv.router import router as arv_router
from app.rehab.router import router as rehab_router
from app.deals.router import router as deals_router
from app.watchlist.router import router as watchlist_router
from app.saved_searches.router import router as searches_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="REI Lead Platform",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.debug else None,
    )

    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(RateLimitMiddleware, redis_url=settings.redis_url)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth_router, prefix="/api/v1/auth", tags=["auth"])
    app.include_router(properties_router, prefix="/api/v1/properties", tags=["properties"])
    app.include_router(arv_router, prefix="/api/v1/properties", tags=["arv"])
    app.include_router(rehab_router, prefix="/api/v1/properties", tags=["rehab"])
    app.include_router(deals_router, prefix="/api/v1/analyses", tags=["deals"])
    app.include_router(watchlist_router, prefix="/api/v1/watchlist", tags=["watchlist"])
    app.include_router(searches_router, prefix="/api/v1/saved-searches", tags=["searches"])

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


app = create_app()
```

```python
# app/config.py
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    debug: bool = False
    database_url: str
    redis_url: str = "redis://localhost:6379"
    cors_origins: list[str] = ["http://localhost:5173"]

    jwt_secret: str
    jwt_access_expire_minutes: int = 15
    jwt_refresh_expire_days: int = 30

    google_client_id: str = ""
    google_client_secret: str = ""

    aws_s3_bucket: str = ""
    aws_region: str = "us-east-1"

    mapbox_token: str = ""

    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    sentry_dsn: str = ""

    rate_limit_free: int = 100
    rate_limit_pro: int = 1000

    model_config = {"env_file": ".env"}


settings = Settings()
```

```python
# app/database.py
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.config import settings

engine = create_async_engine(
    settings.database_url,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
```

### 4.3 Authentication Flow

```python
# app/auth/jwt.py
from datetime import datetime, timedelta, timezone
import jwt
from app.config import settings


def create_access_token(user_id: str, tier: str) -> str:
    payload = {
        "sub": user_id,
        "tier": tier,
        "type": "access",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_access_expire_minutes),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def create_refresh_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "type": "refresh",
        "exp": datetime.now(timezone.utc) + timedelta(days=settings.jwt_refresh_expire_days),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
```

```python
# app/dependencies.py
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.auth.jwt import decode_token
from app.models.user import User

security = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    try:
        payload = decode_token(credentials.credentials)
        if payload.get("type") != "access":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    user = await db.get(User, payload["sub"])
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    await db.execute(f"SET LOCAL app.current_user_id = '{user.id}'")
    return user


def require_tier(minimum: str):
    tier_rank = {"free": 0, "pro": 1, "team": 2}

    async def check(user: User = Depends(get_current_user)):
        if tier_rank.get(user.subscription_tier, 0) < tier_rank[minimum]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires {minimum} subscription",
            )
        return user

    return check
```

### 4.4 Authentication Flow Diagram

```
┌──────────┐                ┌──────────┐               ┌──────────┐
│  Client  │                │  FastAPI │               │   DB     │
└────┬─────┘                └────┬─────┘               └────┬─────┘
     │                           │                          │
     │  POST /auth/register      │                          │
     │  {email, password, name}  │                          │
     │──────────────────────────▶│                          │
     │                           │  hash password (bcrypt)  │
     │                           │  INSERT user             │
     │                           │─────────────────────────▶│
     │                           │                          │
     │  {access_token,           │                          │
     │   refresh_token}          │                          │
     │◀──────────────────────────│                          │
     │                           │                          │
     │  GET /properties          │                          │
     │  Authorization: Bearer <access>                      │
     │──────────────────────────▶│                          │
     │                           │  decode JWT              │
     │                           │  verify user exists      │
     │                           │  SET LOCAL user_id (RLS) │
     │                           │─────────────────────────▶│
     │  {properties: [...]}      │                          │
     │◀──────────────────────────│                          │
     │                           │                          │
     │  POST /auth/refresh       │                          │
     │  {refresh_token}          │  (when access expires)   │
     │──────────────────────────▶│                          │
     │                           │  verify refresh token    │
     │                           │  rotate: revoke old,     │
     │                           │  issue new pair          │
     │  {access_token,           │─────────────────────────▶│
     │   refresh_token}          │                          │
     │◀──────────────────────────│                          │
     │                           │                          │
     │  POST /auth/oauth/google  │                          │
     │  {id_token}               │                          │
     │──────────────────────────▶│                          │
     │                           │  verify with Google      │
     │                           │  find/create user        │
     │                           │  link oauth_account      │
     │  {access_token,           │─────────────────────────▶│
     │   refresh_token}          │                          │
     │◀──────────────────────────│                          │
```

---

## 5. API Endpoint Reference

### 5.1 Full Route Table

| Method | Path | Auth | Tier | Description |
|--------|------|------|------|-------------|
| **Auth** |
| POST | `/api/v1/auth/register` | No | — | Create account |
| POST | `/api/v1/auth/login` | No | — | Login, returns JWT pair |
| POST | `/api/v1/auth/refresh` | No | — | Rotate tokens |
| POST | `/api/v1/auth/logout` | Yes | any | Revoke refresh token |
| POST | `/api/v1/auth/oauth/google` | No | — | Google OAuth login |
| POST | `/api/v1/auth/password-reset` | No | — | Send reset email |
| POST | `/api/v1/auth/password-reset/confirm` | No | — | Set new password with token |
| GET | `/api/v1/auth/me` | Yes | any | Current user profile |
| PATCH | `/api/v1/auth/me` | Yes | any | Update profile / settings |
| **Properties** |
| GET | `/api/v1/properties` | Yes | any | Search with filters |
| GET | `/api/v1/properties/map` | Yes | any | Clustered pins for viewport |
| GET | `/api/v1/properties/heatmap` | Yes | pro | Heatmap tile data |
| GET | `/api/v1/properties/:id` | Yes | any | Full property detail |
| GET | `/api/v1/properties/:id/photos` | Yes | any | Property photos |
| GET | `/api/v1/properties/:id/history` | Yes | any | Sales and tax history |
| **ARV** |
| GET | `/api/v1/properties/:id/arv` | Yes | any | ARV with default params |
| POST | `/api/v1/properties/:id/arv` | Yes | any | ARV with custom comp params |
| **Rehab** |
| GET | `/api/v1/properties/:id/rehab` | Yes | any | System rehab estimates (3 tiers) |
| POST | `/api/v1/properties/:id/rehab` | Yes | any | Save custom rehab estimate |
| **Deal Analysis** |
| POST | `/api/v1/analyses` | Yes | any | Create deal analysis |
| GET | `/api/v1/analyses` | Yes | any | List user's analyses |
| GET | `/api/v1/analyses/:id` | Yes | any | Get analysis detail |
| PATCH | `/api/v1/analyses/:id` | Yes | any | Update analysis |
| DELETE | `/api/v1/analyses/:id` | Yes | any | Delete analysis |
| GET | `/api/v1/analyses/:id/export` | Yes | pro | Export as PDF |
| **Watchlist** |
| GET | `/api/v1/watchlist` | Yes | any | List watchlist |
| POST | `/api/v1/watchlist` | Yes | any | Add to watchlist |
| PATCH | `/api/v1/watchlist/:id` | Yes | any | Update notes/tags |
| DELETE | `/api/v1/watchlist/:id` | Yes | any | Remove from watchlist |
| **Saved Searches** |
| GET | `/api/v1/saved-searches` | Yes | any | List saved searches |
| POST | `/api/v1/saved-searches` | Yes | any | Create saved search |
| PATCH | `/api/v1/saved-searches/:id` | Yes | any | Update filters/alerts |
| DELETE | `/api/v1/saved-searches/:id` | Yes | any | Delete saved search |
| **Billing** |
| POST | `/api/v1/billing/checkout` | Yes | any | Create Stripe checkout session |
| POST | `/api/v1/billing/portal` | Yes | pro | Stripe customer portal URL |
| POST | `/api/v1/billing/webhook` | No | — | Stripe webhook handler |

### 5.2 Example: Property Search with Filters

```python
# app/properties/router.py
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.properties.schemas import PropertyListResponse, PropertyFilters
from app.properties.service import PropertyService

router = APIRouter()


@router.get("", response_model=PropertyListResponse)
async def list_properties(
    sw_lat: float | None = Query(None),
    sw_lng: float | None = Query(None),
    ne_lat: float | None = Query(None),
    ne_lng: float | None = Query(None),
    zip: str | None = None,
    city: str | None = None,
    state: str | None = None,
    property_type: list[str] | None = Query(None),
    distress_type: list[str] | None = Query(None),
    min_price: float | None = None,
    max_price: float | None = None,
    min_beds: int | None = None,
    min_sqft: int | None = None,
    min_equity_pct: float | None = None,
    sort: str = "newest",
    cursor: str | None = None,
    limit: int = Query(50, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    filters = PropertyFilters(
        bounds=(sw_lat, sw_lng, ne_lat, ne_lng) if sw_lat else None,
        zip=zip, city=city, state=state,
        property_type=property_type,
        distress_type=distress_type,
        min_price=min_price, max_price=max_price,
        min_beds=min_beds, min_sqft=min_sqft,
        min_equity_pct=min_equity_pct,
        sort=sort,
    )
    svc = PropertyService(db)
    return await svc.search(filters, cursor=cursor, limit=limit)


@router.get("/map")
async def map_properties(
    sw_lat: float = Query(...),
    sw_lng: float = Query(...),
    ne_lat: float = Query(...),
    ne_lng: float = Query(...),
    zoom: int = Query(12),
    distress_type: list[str] | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    svc = PropertyService(db)
    return await svc.get_map_clusters(
        bounds=(sw_lat, sw_lng, ne_lat, ne_lng),
        zoom=zoom,
        distress_type=distress_type,
    )
```

### 5.3 Example: ARV Calculation Service

```python
# app/arv/service.py
from decimal import Decimal
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class ARVService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def calculate(
        self,
        property_id: str,
        radius_miles: float = 0.5,
        date_range_months: int = 6,
        sqft_tolerance_pct: int = 20,
        max_comps: int = 10,
    ) -> dict:
        radius_meters = radius_miles * 1609.34

        result = await self.db.execute(text("""
            WITH subject AS (
                SELECT id, location, property_type, sqft, bedrooms, year_built
                FROM properties WHERE id = :property_id
            ),
            candidates AS (
                SELECT
                    p.id,
                    p.street_address,
                    p.sqft,
                    p.bedrooms,
                    p.year_built,
                    sh.sale_price,
                    sh.sale_date,
                    sh.price_per_sqft,
                    ST_Distance(p.location, s.location) / 1609.34 AS distance_miles,
                    -- Similarity score: weighted combination of factors
                    GREATEST(0, 100
                        - ABS(p.sqft - s.sqft)::float / NULLIF(s.sqft, 0) * 100
                        - ABS(p.bedrooms - s.bedrooms) * 5
                        - ABS(p.year_built - s.year_built)::float / 10
                        - ST_Distance(p.location, s.location) / 1609.34 * 20
                    ) AS similarity_score
                FROM properties p
                JOIN sales_history sh ON sh.property_id = p.id
                CROSS JOIN subject s
                WHERE ST_DWithin(p.location, s.location, :radius_meters)
                  AND sh.sale_date >= CURRENT_DATE - make_interval(months => :months)
                  AND p.property_type = s.property_type
                  AND p.sqft BETWEEN s.sqft * (1 - :sqft_tol / 100.0)
                                    AND s.sqft * (1 + :sqft_tol / 100.0)
                  AND p.id != s.id
                  AND sh.sale_type = 'arms_length'
                ORDER BY similarity_score DESC
                LIMIT :max_comps
            )
            SELECT
                ROUND(SUM(sale_price * similarity_score) / NULLIF(SUM(similarity_score), 0)) AS arv,
                ROUND(AVG(price_per_sqft)::numeric, 2) AS price_per_sqft_avg,
                COUNT(*) AS comp_count,
                json_agg(json_build_object(
                    'address', street_address,
                    'sale_price', sale_price,
                    'sale_date', sale_date,
                    'sqft', sqft,
                    'distance_miles', ROUND(distance_miles::numeric, 2),
                    'similarity_score', ROUND(similarity_score::numeric, 1)
                ) ORDER BY similarity_score DESC) AS comps
            FROM candidates
        """), {
            "property_id": property_id,
            "radius_meters": radius_meters,
            "months": date_range_months,
            "sqft_tol": sqft_tolerance_pct,
            "max_comps": max_comps,
        })

        row = result.mappings().first()
        comp_count = row["comp_count"] or 0

        if comp_count >= 6:
            confidence = "high"
        elif comp_count >= 3:
            confidence = "medium"
        else:
            confidence = "low"

        return {
            "arv": float(row["arv"] or 0),
            "confidence": confidence,
            "comp_count": comp_count,
            "price_per_sqft_avg": float(row["price_per_sqft_avg"] or 0),
            "comps": row["comps"] or [],
        }
```

---

## 6. Frontend Architecture

### 6.1 Project Structure

```
frontend/
├── public/
├── src/
│   ├── main.tsx
│   ├── App.tsx
│   ├── routes.tsx                # React Router config
│   ├── api/
│   │   ├── client.ts             # Axios instance with interceptors
│   │   ├── auth.ts
│   │   ├── properties.ts
│   │   ├── arv.ts
│   │   ├── deals.ts
│   │   └── watchlist.ts
│   ├── hooks/
│   │   ├── useAuth.ts
│   │   ├── useProperties.ts      # TanStack Query hooks
│   │   ├── useMapViewport.ts
│   │   └── useDebounce.ts
│   ├── stores/
│   │   ├── authStore.ts          # Zustand: tokens, user
│   │   ├── mapStore.ts           # Zustand: viewport, filters
│   │   └── uiStore.ts
│   ├── components/
│   │   ├── ui/                   # shadcn/ui primitives
│   │   ├── layout/
│   │   │   ├── AppShell.tsx
│   │   │   ├── Sidebar.tsx
│   │   │   └── Header.tsx
│   │   ├── map/
│   │   │   ├── PropertyMap.tsx   # Mapbox GL wrapper
│   │   │   ├── PropertyPin.tsx
│   │   │   ├── ClusterLayer.tsx
│   │   │   ├── DrawControl.tsx
│   │   │   └── HeatmapLayer.tsx
│   │   ├── property/
│   │   │   ├── PropertyCard.tsx
│   │   │   ├── PropertyDetail.tsx
│   │   │   ├── PropertyList.tsx
│   │   │   └── DistressBadge.tsx
│   │   ├── analysis/
│   │   │   ├── ARVPanel.tsx
│   │   │   ├── RehabEstimator.tsx
│   │   │   ├── DealCalculator.tsx
│   │   │   └── ProfitSummary.tsx
│   │   └── filters/
│   │       ├── FilterBar.tsx
│   │       └── FilterPanel.tsx
│   ├── pages/
│   │   ├── Dashboard.tsx
│   │   ├── MapExplorer.tsx
│   │   ├── PropertyPage.tsx
│   │   ├── DealsPage.tsx
│   │   ├── WatchlistPage.tsx
│   │   ├── LoginPage.tsx
│   │   └── RegisterPage.tsx
│   └── lib/
│       ├── formatters.ts
│       └── constants.ts
├── tailwind.config.ts
├── vite.config.ts
├── tsconfig.json
└── package.json
```

### 6.2 API Client with Token Refresh

```typescript
// src/api/client.ts
import axios from "axios";
import { useAuthStore } from "../stores/authStore";

const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL || "/api/v1",
});

api.interceptors.request.use((config) => {
  const token = useAuthStore.getState().accessToken;
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

let refreshPromise: Promise<string> | null = null;

api.interceptors.response.use(
  (res) => res,
  async (error) => {
    const original = error.config;
    if (error.response?.status !== 401 || original._retry) {
      return Promise.reject(error);
    }
    original._retry = true;

    if (!refreshPromise) {
      refreshPromise = refreshAccessToken().finally(
        () => (refreshPromise = null)
      );
    }

    const newToken = await refreshPromise;
    original.headers.Authorization = `Bearer ${newToken}`;
    return api(original);
  }
);

async function refreshAccessToken(): Promise<string> {
  const { refreshToken, setTokens, logout } = useAuthStore.getState();
  if (!refreshToken) {
    logout();
    throw new Error("No refresh token");
  }
  try {
    const { data } = await axios.post("/api/v1/auth/refresh", {
      refresh_token: refreshToken,
    });
    setTokens(data.access_token, data.refresh_token);
    return data.access_token;
  } catch {
    logout();
    throw new Error("Refresh failed");
  }
}

export default api;
```

---

## 7. Rate Limiting Strategy

```python
# app/middleware/rate_limit.py
import time
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
import redis.asyncio as redis

from app.config import settings


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, redis_url: str):
        super().__init__(app)
        self.redis = redis.from_url(redis_url)

    async def dispatch(self, request: Request, call_next):
        if request.url.path in ("/health", "/api/docs", "/api/openapi.json"):
            return await call_next(request)

        # Extract user from JWT if present (without full auth)
        user_id, tier = self._extract_identity(request)
        key = f"ratelimit:{user_id or request.client.host}"
        limit = settings.rate_limit_pro if tier == "pro" else settings.rate_limit_free
        window = 60

        current = await self.redis.get(key)
        if current and int(current) >= limit:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded",
                headers={"Retry-After": str(window)},
            )

        pipe = self.redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, window)
        await pipe.execute()

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(
            max(0, limit - int(current or 0) - 1)
        )
        return response

    def _extract_identity(self, request: Request):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return None, "free"
        try:
            from app.auth.jwt import decode_token
            payload = decode_token(auth[7:])
            return payload["sub"], payload.get("tier", "free")
        except Exception:
            return None, "free"
```

---

## 8. Background Job Architecture

```python
# app/workers/celery_app.py
from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery = Celery("rei_platform")
celery.config_from_object({
    "broker_url": settings.redis_url,
    "result_backend": settings.redis_url,
    "task_serializer": "json",
    "accept_content": ["json"],
    "timezone": "UTC",
    "task_routes": {
        "app.workers.tasks.ingest_*": {"queue": "ingestion"},
        "app.workers.tasks.send_*": {"queue": "notifications"},
        "app.workers.tasks.generate_*": {"queue": "export"},
    },
})

celery.conf.beat_schedule = {
    "ingest-foreclosures-daily": {
        "task": "app.workers.tasks.ingest_properties.ingest_foreclosures",
        "schedule": crontab(hour=2, minute=0),
    },
    "ingest-tax-liens-daily": {
        "task": "app.workers.tasks.ingest_properties.ingest_tax_liens",
        "schedule": crontab(hour=3, minute=0),
    },
    "send-daily-alerts": {
        "task": "app.workers.tasks.send_alerts.send_daily_digest",
        "schedule": crontab(hour=8, minute=0),
    },
    "refresh-property-valuations-weekly": {
        "task": "app.workers.tasks.ingest_properties.refresh_valuations",
        "schedule": crontab(day_of_week=0, hour=4, minute=0),
    },
}
```

---

## 9. Cloud Infrastructure (AWS)

### 9.1 Infrastructure Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│  AWS Account                                                     │
│                                                                  │
│  ┌──────────────┐    ┌────────────────────────────────────────┐  │
│  │ Route 53     │───▶│ CloudFront Distribution                │  │
│  │ DNS          │    │  ├─ /api/*  → ALB (backend)            │  │
│  └──────────────┘    │  └─ /*     → S3 (frontend)             │  │
│                      └────────────────────────────────────────┘  │
│                                    │                             │
│  ┌─────────────────────────────────▼─────────────────────────┐  │
│  │  VPC (10.0.0.0/16)                                         │  │
│  │                                                             │  │
│  │  ┌─ Public Subnets ──────────────────────────────────────┐ │  │
│  │  │  ALB (Application Load Balancer)                       │ │  │
│  │  │  NAT Gateway                                           │ │  │
│  │  └────────────────────────────────────────────────────────┘ │  │
│  │                                                             │  │
│  │  ┌─ Private Subnets (App) ───────────────────────────────┐ │  │
│  │  │  ECS Fargate Cluster                                   │ │  │
│  │  │  ├─ api-service (2-10 tasks, auto-scaling)             │ │  │
│  │  │  ├─ worker-ingestion (1-3 tasks)                       │ │  │
│  │  │  ├─ worker-notifications (1-2 tasks)                   │ │  │
│  │  │  └─ worker-export (1-2 tasks)                          │ │  │
│  │  └────────────────────────────────────────────────────────┘ │  │
│  │                                                             │  │
│  │  ┌─ Private Subnets (Data) ──────────────────────────────┐ │  │
│  │  │  RDS PostgreSQL 16 (Multi-AZ, db.r6g.large)           │ │  │
│  │  │  ├─ Primary (write)                                    │ │  │
│  │  │  └─ Read Replica (read-heavy queries)                  │ │  │
│  │  │                                                        │ │  │
│  │  │  ElastiCache Redis 7 (2-node cluster)                  │ │  │
│  │  └────────────────────────────────────────────────────────┘ │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐  ┌────────────────┐  │
│  │ S3       │  │ SES      │  │ Secrets   │  │ CloudWatch     │  │
│  │ Buckets  │  │ Email    │  │ Manager   │  │ + Alarms       │  │
│  └──────────┘  └──────────┘  └───────────┘  └────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

### 9.2 Auto-Scaling Policy

```yaml
# ECS Service Auto-Scaling
api-service:
  min_capacity: 2
  max_capacity: 10
  scaling_policies:
    - metric: ECSServiceAverageCPUUtilization
      target: 60
      scale_in_cooldown: 300
      scale_out_cooldown: 60
    - metric: ALBRequestCountPerTarget
      target: 1000
      scale_in_cooldown: 300
      scale_out_cooldown: 60

worker-ingestion:
  min_capacity: 1
  max_capacity: 3
  scaling_policies:
    - metric: SQS ApproximateNumberOfMessagesVisible
      target: 100  # scale when queue depth exceeds 100
```

### 9.3 Estimated Monthly Costs (Launch)

| Service | Spec | Est. Cost |
|---------|------|-----------|
| ECS Fargate (API, 2 tasks) | 1 vCPU, 2GB each | $70 |
| ECS Fargate (Workers, 3 tasks) | 0.5 vCPU, 1GB each | $45 |
| RDS PostgreSQL Multi-AZ | db.r6g.large + replica | $350 |
| ElastiCache Redis | cache.t4g.medium, 2-node | $95 |
| ALB | — | $25 |
| CloudFront + S3 | — | $15 |
| NAT Gateway | — | $35 |
| SES, Secrets Manager, CloudWatch | — | $20 |
| **Total** | | **~$655/mo** |

---

## 10. Scalability Considerations

### 10.1 Database Scaling Path

| Stage | Properties | Strategy |
|-------|-----------|----------|
| **Launch** (0-500k) | Single RDS + read replica | Sufficient for MVP load |
| **Growth** (500k-5M) | Partition `properties` by state | Table partitioning reduces scan size |
| **Scale** (5M+) | Citus or RDS sharding by geography | Horizontal scale for writes |

```sql
-- Range partitioning by state (growth phase)
CREATE TABLE properties (
    id UUID NOT NULL DEFAULT uuid_generate_v4(),
    state VARCHAR(2) NOT NULL,
    ...
) PARTITION BY LIST (state);

CREATE TABLE properties_ca PARTITION OF properties FOR VALUES IN ('CA');
CREATE TABLE properties_tx PARTITION OF properties FOR VALUES IN ('TX');
CREATE TABLE properties_fl PARTITION OF properties FOR VALUES IN ('FL');
-- ... per active state
CREATE TABLE properties_other PARTITION OF properties DEFAULT;
```

### 10.2 Caching Strategy

| Data | Cache | TTL | Invalidation |
|------|-------|-----|-------------|
| Map clusters (by viewport hash) | Redis | 5 min | On new ingestion run |
| Property detail | Redis | 15 min | On property update |
| ARV calculation (by params hash) | Redis | 1 hour | On new comps ingested |
| User session/profile | Redis | 15 min | On profile update |
| Rehab cost tables | In-memory | App restart | Deploy |
| Search results (by filter hash) | Redis | 2 min | On new ingestion run |

### 10.3 Read Replica Routing

```python
# app/database.py — read replica support
from sqlalchemy.ext.asyncio import create_async_engine

write_engine = create_async_engine(settings.database_url, pool_size=10)
read_engine = create_async_engine(
    settings.database_read_url or settings.database_url,
    pool_size=20,
)

# Use read_engine for: property search, map queries, comp lookups
# Use write_engine for: user writes, deal CRUD, watchlist mutations
```

### 10.4 Performance Targets

| Operation | Target | Approach |
|-----------|--------|----------|
| Map load (viewport query) | < 200ms | PostGIS GIST index + Redis cache + clustering |
| Property search (filtered) | < 500ms | Composite indexes + cursor pagination |
| ARV calculation | < 2s | Optimized spatial query + result caching |
| PDF export | < 10s | Async generation via Celery, return download URL |
| Auth endpoints | < 100ms | Stateless JWT, Redis rate limit |

---

## 11. Security Architecture

### 11.1 Defense Layers

| Layer | Measure |
|-------|---------|
| **Network** | VPC isolation, private subnets for data, security groups restrict port access |
| **Transport** | TLS 1.3 everywhere, HSTS headers, CloudFront SSL termination |
| **Authentication** | bcrypt password hashing (cost=12), JWT with short-lived access tokens (15 min), refresh token rotation with revocation |
| **Authorization** | PostgreSQL Row-Level Security enforces tenant isolation at the DB layer |
| **Input Validation** | Pydantic models validate all API input; parameterized queries prevent SQL injection |
| **Rate Limiting** | Per-user sliding window via Redis; tiered limits by subscription |
| **Secrets** | AWS Secrets Manager for all credentials; never in env files or code |
| **Headers** | CSP, X-Frame-Options, X-Content-Type-Options via CloudFront response headers policy |
| **Monitoring** | Sentry for error tracking; CloudWatch alarms for anomalous request patterns |
| **Data** | RDS encryption at rest (AES-256); S3 bucket policy denies public access |

### 11.2 CORS Configuration

```python
cors_origins = [
    "https://app.reiplatform.com",
    "https://staging.reiplatform.com",
]
# localhost origins only in debug mode
```

---

## 12. CI/CD Pipeline

```yaml
# .github/workflows/deploy.yml
name: Build & Deploy

on:
  push:
    branches: [main, staging]

jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgis/postgis:16-3.4
        env:
          POSTGRES_DB: test_db
          POSTGRES_PASSWORD: test
        ports: ["5432:5432"]
      redis:
        image: redis:7
        ports: ["6379:6379"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -e ".[test]"
      - run: pytest --cov=app --cov-report=xml
      - uses: actions/setup-node@v4
        with: { node-version: "20" }
      - run: cd frontend && npm ci && npm run lint && npm run test

  deploy:
    needs: test
    runs-on: ubuntu-latest
    steps:
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_ARN }}
          aws-region: us-east-1
      - uses: aws-actions/amazon-ecr-login@v2
      - run: |
          docker build -t $ECR_REPO:$GITHUB_SHA .
          docker push $ECR_REPO:$GITHUB_SHA
      - run: |
          aws ecs update-service \
            --cluster rei-platform \
            --service api \
            --force-new-deployment
      - run: |
          cd frontend && npm ci && npm run build
          aws s3 sync dist/ s3://$FRONTEND_BUCKET --delete
          aws cloudfront create-invalidation \
            --distribution-id $CF_DIST_ID --paths "/*"
```

---

## 13. Monitoring and Observability

| Concern | Tool | Key Metrics |
|---------|------|-------------|
| **APM** | Datadog / X-Ray | p50/p95/p99 latency per endpoint, throughput |
| **Errors** | Sentry | Unhandled exceptions, error rate by endpoint |
| **Logs** | CloudWatch Logs | Structured JSON logs, request ID correlation |
| **Infrastructure** | CloudWatch | CPU, memory, DB connections, cache hit rate |
| **Uptime** | CloudWatch Synthetics | Canary checks on /health and key flows |
| **Alerts** | PagerDuty / SNS | Error rate > 5%, p99 > 5s, DB connections > 80% |
| **Business** | Datadog dashboards | Signups, analyses created, search volume, conversion |
