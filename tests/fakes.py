import io
from datetime import datetime, timezone

from app.storage.interface import (
    ObjectBody,
    ObjectInfo,
    PresignedUpload,
    StorageInterface,
    StorageError,
    StorageObjectNotFoundError,
)


class InMemoryStorage(StorageInterface):
    """A full StorageInterface backed by a dict, for tests."""

    def __init__(self, name="mem"):
        self.name = name
        self.objects = {}          # key -> (bytes, content_type, last_modified)
        self.uploads = []          # upload call log
        self.presigns = []         # presign call log
        self.should_fail = False
        self.fail_not_found = False

    # --- helpers used by tests --------------------------------------------
    def put(self, key, data=b"data", content_type="", last_modified=None):
        self.objects[key] = (
            data,
            content_type,
            last_modified or datetime.now(timezone.utc),
        )

    def _get(self, key):
        if key not in self.objects:
            raise StorageObjectNotFoundError(f"{self.name}: no such key {key}")
        return self.objects[key]

    def _guard(self):
        if self.should_fail:
            raise StorageError("Mocked error", transient=True)
        if self.fail_not_found:
            raise StorageObjectNotFoundError("Mocked not found error")

    # --- StorageInterface --------------------------------------------------
    def upload_file(self, stream, object_key, content_type="", bucket=None):
        self._guard()
        self.uploads.append({
            "key": object_key,
            "content_type": content_type,
            "bucket": bucket,
        })
        self.put(object_key, stream.read(), content_type)

    def generate_presigned_upload(self, object_key, content_type="",
                                  max_bytes=25 * 1024 * 1024, expires_in=900,
                                  bucket=None):
        self._guard()
        self.presigns.append({
            "key": object_key,
            "content_type": content_type,
            "max_bytes": max_bytes,
            "expires_in": expires_in,
        })
        return PresignedUpload(
            url=f"http://storage.test/{bucket or 'bucket'}",
            fields={"key": object_key, "Content-Type": content_type,
                    "policy": "fake-policy", "x-amz-signature": "fake-sig"},
            key=object_key,
            expires_in=expires_in,
            max_bytes=max_bytes,
        )

    def download_file(self, object_key, bucket=None):
        self._guard()
        return self._get(object_key)[0]

    def open_file(self, object_key, bucket=None):
        self._guard()
        data, content_type, _ = self._get(object_key)
        return ObjectBody(io.BytesIO(data), content_type=content_type, size=len(data))

    def head_file(self, object_key, bucket=None):
        self._guard()
        data, content_type, last_modified = self._get(object_key)
        return ObjectInfo(
            key=object_key,
            size=len(data),
            last_modified=last_modified,
            content_type=content_type,
        )

    def list_objects(self, prefix="", bucket=None):
        self._guard()
        for key in sorted(self.objects):
            if key.startswith(prefix):
                data, content_type, last_modified = self.objects[key]
                yield ObjectInfo(
                    key=key,
                    size=len(data),
                    last_modified=last_modified,
                    content_type=content_type,
                )

    def delete_file(self, object_key, bucket=None):
        self._guard()
        self.objects.pop(object_key, None)
