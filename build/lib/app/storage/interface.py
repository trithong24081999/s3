from abc import ABC, abstractmethod
from typing import BinaryIO, Optional

class StorageError(Exception):
    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient

class StorageObjectNotFoundError(StorageError):
    def __init__(self, message: str):
        super().__init__(message, transient=False)

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
