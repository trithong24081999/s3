"""
Two-tier storage: a hot backend for fresh uploads and a cold backend that
holds whatever the tiering job has aged out.

Objects keep the *same key* in both tiers, so nothing outside this class needs
to know where a given object currently lives. Reads try hot first and fall back
to cold, which means a read stays correct while the mover is mid-flight.
"""

from typing import BinaryIO, Iterator, Optional

from .interface import (
    ObjectBody,
    ObjectInfo,
    PresignedUpload,
    StorageInterface,
    StorageError,
    StorageObjectNotFoundError,
)


class TieredStorage(StorageInterface):
    def __init__(self, hot: StorageInterface, cold: StorageInterface):
        self.hot = hot
        self.cold = cold

    # --- writes always land in the hot tier -------------------------------
    def upload_file(
        self,
        stream: BinaryIO,
        object_key: str,
        content_type: str = "",
        bucket: Optional[str] = None,
    ) -> None:
        self.hot.upload_file(
            stream=stream,
            object_key=object_key,
            content_type=content_type,
            bucket=bucket,
        )

    def generate_presigned_upload(
        self,
        object_key: str,
        content_type: str = "",
        max_bytes: int = 25 * 1024 * 1024,
        expires_in: int = 900,
        bucket: Optional[str] = None,
    ) -> PresignedUpload:
        # Direct browser uploads land in hot storage like any other write; the
        # tiering job relocates them later.
        return self.hot.generate_presigned_upload(
            object_key=object_key,
            content_type=content_type,
            max_bytes=max_bytes,
            expires_in=expires_in,
            bucket=bucket,
        )

    # --- reads fall through hot -> cold -----------------------------------
    def download_file(self, object_key: str, bucket: Optional[str] = None) -> bytes:
        try:
            return self.hot.download_file(object_key, bucket=bucket)
        except StorageObjectNotFoundError:
            return self.cold.download_file(object_key)

    def open_file(self, object_key: str, bucket: Optional[str] = None) -> ObjectBody:
        try:
            return self.hot.open_file(object_key, bucket=bucket)
        except StorageObjectNotFoundError:
            return self.cold.open_file(object_key)

    def head_file(self, object_key: str, bucket: Optional[str] = None) -> ObjectInfo:
        try:
            return self.hot.head_file(object_key, bucket=bucket)
        except StorageObjectNotFoundError:
            return self.cold.head_file(object_key)

    def list_objects(
        self,
        prefix: str = "",
        bucket: Optional[str] = None,
    ) -> Iterator[ObjectInfo]:
        """
        Hot then cold. A key mid-move can appear in both tiers, so already-seen
        keys are suppressed; the hot copy wins because it is the newer one.
        """
        seen = set()
        for info in self.hot.list_objects(prefix=prefix, bucket=bucket):
            seen.add(info.key)
            yield info
        for info in self.cold.list_objects(prefix=prefix):
            if info.key not in seen:
                yield info

    def delete_file(self, object_key: str, bucket: Optional[str] = None) -> None:
        """
        Deletes from both tiers. Removing only the tier we happen to find first
        would let a stale copy in the other tier resurface on the next read.
        """
        errors = []
        for tier in (self.hot, self.cold):
            try:
                tier.delete_file(object_key)
            except StorageObjectNotFoundError:
                pass
            except StorageError as e:
                errors.append(e)

        if errors:
            raise errors[0]
