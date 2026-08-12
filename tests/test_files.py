import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.api.files import get_storage_interface
from app.storage.interface import StorageInterface, StorageError, StorageObjectNotFoundError
from app.storage.s3 import S3Storage

class MockStorage(StorageInterface):
    def __init__(self):
        self.uploads = []
        self.should_fail = False
        self.fail_not_found = False

    def upload_file(self, stream, object_key, content_type="", bucket=None):
        if self.should_fail:
            raise StorageError("Mocked error", transient=True)
        if self.fail_not_found:
            raise StorageObjectNotFoundError("Mocked not found error")
        
        self.uploads.append({
            "key": object_key,
            "content_type": content_type,
            "bucket": bucket
        })

@pytest.fixture
def mock_storage():
    storage = MockStorage()
    app.dependency_overrides[get_storage_interface] = lambda: storage
    yield storage
    app.dependency_overrides = {}

@pytest.fixture
def client():
    return TestClient(app)

def test_successful_upload(client, mock_storage):
    file_content = b"fake pdf content"
    response = client.post(
        "/api/files",
        files={"file": ("../../secret.pdf", file_content, "application/pdf")}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert "key" in data
    assert data["filename"] == "../../secret.pdf"
    
    assert data["key"].startswith("uploads/")
    assert ".." not in data["key"]
    
    assert len(mock_storage.uploads) == 1
    upload = mock_storage.uploads[0]
    assert upload["content_type"] == "application/pdf"
    assert upload["key"] == data["key"]

def test_storage_error_returns_500(client, mock_storage):
    mock_storage.should_fail = True
    response = client.post(
        "/api/files",
        files={"file": ("test.txt", b"test", "text/plain")}
    )
    assert response.status_code == 500

def test_transient_error_classification():
    from botocore.exceptions import ClientError
    
    s3_storage = S3Storage(
        endpoint_url="http://mock", access_key="k", secret_key="s", region="r", default_bucket="b"
    )
    s3_storage._client = MagicMock()
    
    error_response = {'Error': {'Code': 'ServiceUnavailable', 'Message': 'Busy'}}
    s3_storage.client.upload_fileobj.side_effect = ClientError(error_response, 'PutObject')
    
    with pytest.raises(StorageError) as exc_info:
        s3_storage.upload_file(None, "key")
        
    assert exc_info.value.transient is True

def test_missing_object_classification():
    from botocore.exceptions import ClientError
    
    s3_storage = S3Storage(
        endpoint_url="http://mock", access_key="k", secret_key="s", region="r", default_bucket="b"
    )
    s3_storage._client = MagicMock()
    
    error_response = {'Error': {'Code': 'NoSuchBucket', 'Message': 'Not Found'}}
    s3_storage.client.upload_fileobj.side_effect = ClientError(error_response, 'PutObject')
    
    with pytest.raises(StorageObjectNotFoundError) as exc_info:
        s3_storage.upload_file(None, "key")
        
    assert exc_info.value.transient is False
