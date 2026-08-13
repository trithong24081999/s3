from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO, Iterator, Optional

class StorageError(Exception):
    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient

class StorageObjectNotFoundError(StorageError):
    def __init__(self, message: str):
        super().__init__(message, transient=False)


@dataclass(frozen=True)
class PresignedUpload:
    """
    A short-lived credential letting a browser POST one object straight to the
    storage backend. `fields` must be sent as form fields *before* the file
    part -- S3 ignores anything that follows the file.
    """
    url: str
    fields: dict
    key: str
    expires_in: int
    max_bytes: int


@dataclass(frozen=True)
class ObjectInfo:
    """Metadata about a stored object, without its body."""
    key: str
    size: int
    last_modified: datetime
    content_type: str = ""


class ObjectBody:
    """
    A streaming object body. Use as a context manager so the underlying HTTP
    connection is released even when the caller aborts mid-read.
    """

    def __init__(self, stream: BinaryIO, content_type: str = "", size: int = 0):
        self.stream = stream
        self.content_type = content_type
        self.size = size

    def __enter__(self) -> "ObjectBody":
        return self

    def __exit__(self, *exc_info) -> None:
        close = getattr(self.stream, "close", None)
        if close is not None:
            close()


class StorageInterface(ABC):
    @abstractmethod
    def upload_file(
        self,
        stream: BinaryIO,
        object_key: str,
        content_type: str = "",
        bucket: Optional[str] = None,
    ) -> None:
        ...

    @abstractmethod
    def generate_presigned_upload(
        self,
        object_key: str,
        content_type: str = "",
        max_bytes: int = 25 * 1024 * 1024,
        expires_in: int = 900,
        bucket: Optional[str] = None,
    ) -> PresignedUpload:
        """
        Credential for a direct browser -> storage upload.

        `content_type` and `max_bytes` are signed into the policy, so the
        backend rejects an upload that does not match. They are the only
        enforcement available once the bytes stop passing through this service.
        """
        ...

    @abstractmethod
    def download_file(
        self,
        object_key: str,
        bucket: Optional[str] = None,
    ) -> bytes:
        """Return the full object body. Raises StorageObjectNotFoundError if missing."""
        ...

    @abstractmethod
    def open_file(
        self,
        object_key: str,
        bucket: Optional[str] = None,
    ) -> ObjectBody:
        """
        Stream the object instead of buffering it. Preferred for copies, where
        holding a whole object in memory buys nothing.
        """
        ...

    @abstractmethod
    def head_file(
        self,
        object_key: str,
        bucket: Optional[str] = None,
    ) -> ObjectInfo:
        """Metadata only. Raises StorageObjectNotFoundError if missing."""
        ...

    @abstractmethod
    def list_objects(
        self,
        prefix: str = "",
        bucket: Optional[str] = None,
    ) -> Iterator[ObjectInfo]:
        """Yield every object under `prefix`, paginating as needed."""
        ...

    @abstractmethod
    def delete_file(
        self,
        object_key: str,
        bucket: Optional[str] = None,
    ) -> None:
        """Idempotent: deleting a missing key is not an error."""
        ...
