"""
Mandari API - Main Application Entry Point

FastAPI application for kommunalpolitische Transparenz.
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.core.config import settings
from src.core.database import init_db
from src.oparl.router import router as oparl_router
from src.public.router import router as public_router
from src.work.router import router as work_router
from src.auth.router import router as auth_router
from src.ai.router import router as ai_router
from src.search.router import router as search_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""
    # Startup
    await init_db()
    yield
    # Shutdown
    pass


app = FastAPI(
    title="Mandari API",
    description="API für kommunalpolitische Transparenz - basierend auf OParl",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Health Check
@app.get("/health", tags=["system"])
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy", "version": "0.1.0"}


# Include Routers
app.include_router(oparl_router, prefix="/api/v1")
app.include_router(public_router, prefix="/api/v1")
app.include_router(work_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1/auth")
app.include_router(ai_router, prefix="/api/v1")
app.include_router(search_router, prefix="/api/v1")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=True)
