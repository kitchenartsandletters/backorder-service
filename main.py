import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import ALLOWED_ORIGINS
from routes.admin_backorders import router as admin_backorders_router
from routes.webhooks import router as webhooks_router

app = FastAPI(title="backorder-service")

# CORS for admin dashboard
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logger = logging.getLogger("uvicorn.error")
logger.info("🚀 Backorder service fully started and accepting requests")

app.include_router(webhooks_router)
app.include_router(
    admin_backorders_router, prefix="/admin/backorders", tags=["admin_backorders"]
)


@app.get("/healthz")
def healthz():
    return {"ok": True}
