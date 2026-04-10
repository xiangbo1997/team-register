from fastapi import FastAPI

from api.mailbox_service import router as mailbox_service_router
from services.mailbox_service import init_mailbox_service_db


app = FastAPI(title="email-provider", version="0.1.0")


@app.on_event("startup")
def on_startup():
    init_mailbox_service_db()


app.include_router(mailbox_service_router, prefix="/api")


@app.get("/")
def root():
    return {"name": "email-provider", "ok": True}
