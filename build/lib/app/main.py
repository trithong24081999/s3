from functools import lru_cache
from fastapi import FastAPI
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.api.files import router as files_router, get_storage_interface
from app.storage.s3 import S3Storage

class Settings(BaseSettings):
    # Thiết lập giá trị mặc định chạy với LocalStack
    s3_endpoint: str = "http://127.0.0.1:4566"
    aws_access_key_id: str = "test"
    aws_secret_access_key: str = "test"
    aws_region: str = "us-east-1"
    s3_bucket: str = "my-bucket"
    
    multipart_threshold: int = 8 * 1024 * 1024
    multipart_chunksize: int = 8 * 1024 * 1024
    max_concurrency: int = 4
    use_threads: bool = True

    model_config = SettingsConfigDict(
        env_file=".env", 
        env_file_encoding="utf-8",
        extra="ignore"
    )

@lru_cache()
def get_settings():
    return Settings()

@lru_cache()
def get_s3_storage() -> S3Storage:
    settings = get_settings()
    return S3Storage(
        endpoint_url=settings.s3_endpoint,
        access_key=settings.aws_access_key_id,
        secret_key=settings.aws_secret_access_key,
        region=settings.aws_region,
        default_bucket=settings.s3_bucket,
        multipart_threshold=settings.multipart_threshold,
        multipart_chunksize=settings.multipart_chunksize,
        max_concurrency=settings.max_concurrency,
        use_threads=settings.use_threads,
    )

app = FastAPI(title="FastAPI S3 Upload")

app.dependency_overrides[get_storage_interface] = get_s3_storage

app.include_router(files_router, prefix="/api")