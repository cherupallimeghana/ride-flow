"""
Application entrypoint. Wires together routers, middleware, CORS,
structured logging, and global exception handling.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.logging_config import configure_logging, get_logger
from app.rate_limit import RateLimitMiddleware
from app.routers import auth, drivers, health, rides, websocket

settings = get_settings()
configure_logging(debug=settings.debug)
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("app_startup", environment=settings.environment)
    yield
    log.info("app_shutdown")


app = FastAPI(
    title=settings.app_name,
    description="Production-oriented ride booking platform API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RateLimitMiddleware)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    log.warning("http_exception", path=request.url.path, status_code=exc.status_code, detail=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.exception("unhandled_exception", path=request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(drivers.router)
app.include_router(rides.router)
app.include_router(websocket.router)


@app.get("/")
async def root():
    return {"service": settings.app_name, "status": "running", "docs": "/docs"}
