from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.storage.interface import StorageInterface
from app.storage.s3 import S3Storage
from app.storage.tiered import TieredStorage


class Settings(BaseSettings):
    # --- Hot tier: LocalStack / AWS S3 -------------------------------------
    # Thiết lập giá trị mặc định chạy với LocalStack
    s3_endpoint: str = "http://127.0.0.1:4566"
    aws_access_key_id: str = "test"
    aws_secret_access_key: str = "test"
    aws_region: str = "us-east-1"
    s3_bucket: str = "my-bucket"

    # The address a *browser* uses to reach storage, when it differs from the
    # one this service uses. Presigned URLs are signed against it.
    s3_public_endpoint: str = ""

    # --- Direct browser uploads --------------------------------------------
    # Both are signed into the presigned POST policy, so storage enforces them.
    max_upload_bytes: int = 25 * 1024 * 1024
    presign_expires_seconds: int = 900

    multipart_threshold: int = 8 * 1024 * 1024
    multipart_chunksize: int = 8 * 1024 * 1024
    max_concurrency: int = 4
    use_threads: bool = True

    # --- Cold tier: self-hosted Garage -------------------------------------
    # Leave the key pair empty to run hot-only; nothing else has to change.
    garage_endpoint: str = "http://127.0.0.1:3910"
    garage_access_key_id: str = ""
    garage_secret_access_key: str = ""
    garage_region: str = "garage"
    garage_bucket: str = "uploads-archive"

    # --- Tiering job --------------------------------------------------------
    tiering_prefix: str = "uploads/"
    tiering_max_age_days: int = 30

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @property
    def cold_tier_configured(self) -> bool:
        return bool(self.garage_access_key_id and self.garage_secret_access_key)


@lru_cache()
def get_settings() -> Settings:
    return Settings()


@lru_cache()
def get_hot_storage() -> S3Storage:
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
        public_endpoint_url=settings.s3_public_endpoint or None,
    )


@lru_cache()
def get_cold_storage() -> Optional[S3Storage]:
    """
    Garage speaks S3, so the same client works — it only needs path-style
    addressing and SigV4, which S3Storage already configures.
    """
    settings = get_settings()
    if not settings.cold_tier_configured:
        return None

    return S3Storage(
        endpoint_url=settings.garage_endpoint,
        access_key=settings.garage_access_key_id,
        secret_key=settings.garage_secret_access_key,
        region=settings.garage_region,
        default_bucket=settings.garage_bucket,
        multipart_threshold=settings.multipart_threshold,
        multipart_chunksize=settings.multipart_chunksize,
        max_concurrency=settings.max_concurrency,
        use_threads=settings.use_threads,
    )


@lru_cache()
def get_storage() -> StorageInterface:
    """Tiered when Garage is configured, plain hot storage otherwise."""
    cold = get_cold_storage()
    if cold is None:
        return get_hot_storage()
    return TieredStorage(hot=get_hot_storage(), cold=cold)
