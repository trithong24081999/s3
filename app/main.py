from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.files import router as files_router, get_storage_interface
from app.config import get_hot_storage, get_settings, get_storage

# Settings and storage construction moved to app.config so the tiering CLI can
# read them without building the web app. Re-exported for existing importers.
__all__ = ["app", "get_hot_storage", "get_settings", "get_storage"]

app = FastAPI(title="FastAPI S3 Upload")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.dependency_overrides[get_storage_interface] = get_storage

app.include_router(files_router, prefix="/api")
