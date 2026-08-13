from typing import BinaryIO, Iterator, Optional
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, BotoCoreError
from boto3.s3.transfer import TransferConfig

from .interface import (
    ObjectBody,
    ObjectInfo,
    PresignedUpload,
    StorageInterface,
    StorageError,
    StorageObjectNotFoundError,
)

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
        public_endpoint_url: Optional[str] = None,
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

        # Presigned URLs are consumed by a browser, which often cannot resolve
        # the address this service uses (`http://localstack:4566` inside a
        # compose network, say). The signature covers the host, so a presign
        # for the wrong host cannot simply be rewritten afterwards -- it has to
        # be signed against the public address from the start.
        self.public_endpoint_url = public_endpoint_url or endpoint_url

        self._client = None
        self._presign_client = None

    def _build_client(self, endpoint_url: str):
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

        return boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region,
            config=Config(**config_kwargs),
        )

    @property
    def client(self):
        if self._client is None:
            self._client = self._build_client(self.endpoint_url)
        return self._client

    @property
    def presign_client(self):
        """Signs against the browser-reachable address, which may differ."""
        if self.public_endpoint_url == self.endpoint_url:
            return self.client
        if self._presign_client is None:
            self._presign_client = self._build_client(self.public_endpoint_url)
        return self._presign_client

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
            raise self._translate_client_error(e)
        except BotoCoreError as e:
            raise StorageError(f"BotoCore error: {str(e)}", transient=True)
        except StorageError:
            raise
        except Exception as e:
            raise StorageError(f"Unexpected upload error: {str(e)}", transient=False)

    def generate_presigned_upload(
        self,
        object_key: str,
        content_type: str = "",
        max_bytes: int = 25 * 1024 * 1024,
        expires_in: int = 900,
        bucket: Optional[str] = None,
    ) -> PresignedUpload:
        target_bucket = bucket or self.default_bucket

        # Signed into the policy: the backend itself rejects a mismatch, so a
        # client cannot upload something larger or of a different type than
        # what was handed out.
        conditions = [["content-length-range", 0, max_bytes]]
        fields = {}
        if content_type:
            conditions.append({"Content-Type": content_type})
            fields["Content-Type"] = content_type

        try:
            presigned = self.presign_client.generate_presigned_post(
                Bucket=target_bucket,
                Key=object_key,
                Fields=fields,
                Conditions=conditions,
                ExpiresIn=expires_in,
            )
        except ClientError as e:
            raise self._translate_client_error(e)
        except BotoCoreError as e:
            raise StorageError(f"BotoCore error: {str(e)}", transient=True)
        except Exception as e:
            raise StorageError(f"Unexpected presign error: {str(e)}", transient=False)

        fields_out = dict(presigned["fields"])
        # boto3 puts {"bucket": ...} in the policy conditions but omits the
        # matching form field, because AWS infers the bucket from the URL.
        # Stricter implementations (Garage) require every condition key to be
        # present as a field and reject the POST with "Key 'bucket' is required
        # in policy" otherwise. Sending it satisfies both: the value matches
        # the condition either way.
        fields_out.setdefault("bucket", target_bucket)

        return PresignedUpload(
            url=presigned["url"],
            fields=fields_out,
            key=object_key,
            expires_in=expires_in,
            max_bytes=max_bytes,
        )

    def download_file(
        self,
        object_key: str,
        bucket: Optional[str] = None,
    ) -> bytes:
        with self.open_file(object_key, bucket=bucket) as body:
            try:
                return body.stream.read()
            except (ClientError, BotoCoreError) as e:
                raise StorageError(f"Download failed: {str(e)}", transient=True)

    def open_file(
        self,
        object_key: str,
        bucket: Optional[str] = None,
    ) -> ObjectBody:
        target_bucket = bucket or self.default_bucket

        try:
            response = self.client.get_object(Bucket=target_bucket, Key=object_key)
        except ClientError as e:
            raise self._translate_client_error(e)
        except BotoCoreError as e:
            raise StorageError(f"BotoCore error: {str(e)}", transient=True)
        except Exception as e:
            raise StorageError(f"Unexpected download error: {str(e)}", transient=False)

        return ObjectBody(
            stream=response["Body"],
            content_type=response.get("ContentType", ""),
            size=response.get("ContentLength", 0),
        )

    def head_file(
        self,
        object_key: str,
        bucket: Optional[str] = None,
    ) -> ObjectInfo:
        target_bucket = bucket or self.default_bucket

        try:
            response = self.client.head_object(Bucket=target_bucket, Key=object_key)
        except ClientError as e:
            raise self._translate_client_error(e)
        except BotoCoreError as e:
            raise StorageError(f"BotoCore error: {str(e)}", transient=True)
        except Exception as e:
            raise StorageError(f"Unexpected head error: {str(e)}", transient=False)

        return ObjectInfo(
            key=object_key,
            size=response.get("ContentLength", 0),
            last_modified=response["LastModified"],
            content_type=response.get("ContentType", ""),
        )

    def list_objects(
        self,
        prefix: str = "",
        bucket: Optional[str] = None,
    ) -> Iterator[ObjectInfo]:
        target_bucket = bucket or self.default_bucket

        try:
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=target_bucket, Prefix=prefix):
                for item in page.get("Contents", []):
                    # A "directory placeholder" is a zero-byte key ending in '/'.
                    if item["Key"].endswith("/"):
                        continue
                    yield ObjectInfo(
                        key=item["Key"],
                        size=item.get("Size", 0),
                        last_modified=item["LastModified"],
                    )
        except ClientError as e:
            raise self._translate_client_error(e)
        except BotoCoreError as e:
            raise StorageError(f"BotoCore error: {str(e)}", transient=True)

    def delete_file(
        self,
        object_key: str,
        bucket: Optional[str] = None,
    ) -> None:
        target_bucket = bucket or self.default_bucket

        try:
            # S3 DELETE is idempotent; a missing key still returns 204.
            self.client.delete_object(Bucket=target_bucket, Key=object_key)
        except ClientError as e:
            raise self._translate_client_error(e)
        except BotoCoreError as e:
            raise StorageError(f"BotoCore error: {str(e)}", transient=True)
        except Exception as e:
            raise StorageError(f"Unexpected delete error: {str(e)}", transient=False)

    @staticmethod
    def _translate_client_error(e: ClientError) -> StorageError:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')

        if error_code in ('404', 'NoSuchKey', 'NoSuchBucket', 'NotFound'):
            return StorageObjectNotFoundError(f"S3 Object/Bucket not found: {error_code}")

        transient_codes = (
            '500', '502', '503', '504', 'InternalError',
            'RequestTimeout', 'ServiceUnavailable', 'SlowDown',
            'Throttling', 'ThrottlingException'
        )
        if error_code in transient_codes:
            return StorageError(f"Transient S3 Error: {error_code}", transient=True)

        return StorageError(f"S3 ClientError: {error_code}", transient=False)
