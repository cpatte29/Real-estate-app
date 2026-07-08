"""FastAPI application entry point."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.scanner import router as scanner_router
from app.api.leads import router as leads_router
from app.api.admin_comps import router as admin_comps_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="REI Lead Scanner Platform",
        version="1.0.0",
        description="Daily distressed property lead scanner with ARV, rehab, and deal analysis.",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(scanner_router)
    app.include_router(leads_router)
    app.include_router(admin_comps_router)

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "rei-scanner"}

    return app


app = create_app()
