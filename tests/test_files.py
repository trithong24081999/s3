import io

import pytest
from PIL import Image
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.api.files import get_storage_interface
from app.storage.interface import StorageError, StorageObjectNotFoundError
from app.storage.s3 import S3Storage
from tests.fakes import InMemoryStorage

def make_image_bytes(size=(200, 100), fmt="PNG", mode="RGB"):
    buffer = io.BytesIO()
    Image.new(mode, size, color="red").save(buffer, format=fmt)
    return buffer.getvalue()

@pytest.fixture
def mock_storage():
    storage = InMemoryStorage()
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

def test_resize_preserves_aspect_ratio(client, mock_storage):
    mock_storage.put("uploads/a/pic.png", make_image_bytes((200, 100)))

    response = client.get("/api/files/resize", params={"key": "uploads/a/pic.png", "width": 50})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(response.content)) as img:
        assert img.size == (50, 25)


def test_resize_rgba_source_to_jpeg(client, mock_storage):
    mock_storage.put("uploads/a/pic.jpg", make_image_bytes((120, 60), fmt="JPEG"))

    response = client.get(
        "/api/files/resize",
        params={"key": "uploads/a/pic.jpg", "width": 60, "height": 30},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(response.content)) as img:
        assert img.size == (60, 30)


def test_resize_requires_a_dimension(client, mock_storage):
    response = client.get("/api/files/resize", params={"key": "uploads/a/pic.png"})
    assert response.status_code == 400


def test_resize_missing_object_returns_404(client, mock_storage):
    response = client.get("/api/files/resize", params={"key": "nope.png", "width": 10})
    assert response.status_code == 404


def test_resize_non_image_returns_400(client, mock_storage):
    mock_storage.put("uploads/a/doc.pdf", b"not an image")
    response = client.get("/api/files/resize", params={"key": "uploads/a/doc.pdf", "width": 10})
    assert response.status_code == 400


def test_resize_storage_failure_returns_503(client, mock_storage):
    mock_storage.should_fail = True
    response = client.get("/api/files/resize", params={"key": "uploads/a/pic.png", "width": 10})
    assert response.status_code == 503


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
