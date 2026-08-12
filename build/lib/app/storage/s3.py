from typing import BinaryIO, Optional
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, BotoCoreError
from boto3.s3.transfer import TransferConfig

from .interface import StorageInterface, StorageError, StorageObjectNotFoundError

class S3Storage(StorageInterface):
    def __init__(
        self,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        region: str,
        default_bucket: str,
        multipart_threshold: int = 8388608,
        multipart_chunksize: int = 8388608,
        max_concurrency: int = 4,
        use_threads: bool = True,
    ):
        self.endpoint_url = endpoint_url
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.default_bucket = default_bucket

        self.multipart_threshold = multipart_threshold
        self.multipart_chunksize = multipart_chunksize
        self.max_concurrency = max_concurrency
        self.use_threads = use_threads

        self._client = None

    @property
    def client(self):
        if self._client is None:
            config_kwargs = {
                "s3": {"addressing_style": "path"},
                "signature_version": "s3v4"
            }

            try:
                Config(**config_kwargs, request_checksum_calculation="when_required")
                config_kwargs["request_checksum_calculation"] = "when_required"
                config_kwargs["response_checksum_validation"] = "when_required"
            except TypeError:
                pass 

            config = Config(**config_kwargs)

            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name=self.region,
                config=config
            )
        return self._client

    def upload_file(
        self,
        stream: BinaryIO,
        object_key: str,
        content_type: str = "",
        bucket: Optional[str] = None,
    ) -> None:
        target_bucket = bucket or self.default_bucket
        transfer_config = TransferConfig(
            multipart_threshold=self.multipart_threshold,
            multipart_chunksize=self.multipart_chunksize,
            max_concurrency=self.max_concurrency,
            use_threads=self.use_threads,
        )

        extra_args = {}
        if content_type:
            extra_args["ContentType"] = content_type

        try:
            self.client.upload_fileobj(
                Fileobj=stream,
                Bucket=target_bucket,
                Key=object_key,
                ExtraArgs=extra_args or None,
                Config=transfer_config,
            )
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            
            if error_code in ('404', 'NoSuchKey', 'NoSuchBucket', 'NotFound'):
                raise StorageObjectNotFoundError(f"S3 Object/Bucket not found: {error_code}")
                
            transient_codes = (
                '500', '502', '503', '504', 'InternalError', 
                'RequestTimeout', 'ServiceUnavailable', 'SlowDown', 
                'Throttling', 'ThrottlingException'
            )
            if error_code in transient_codes:
                raise StorageError(f"Transient S3 Error: {error_code}", transient=True)
                
            raise StorageError(f"S3 ClientError: {error_code}", transient=False)
        except BotoCoreError as e:
            raise StorageError(f"BotoCore error: {str(e)}", transient=True)
        except Exception as e:
            raise StorageError(f"Unexpected upload error: {str(e)}", transient=False)
