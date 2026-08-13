import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from app.main import app
from app.api.files import get_storage_interface
from app.config import Settings
from app.storage.s3 import S3Storage
from app.storage.tiered import TieredStorage
from tests.fakes import InMemoryStorage


@pytest.fixture
def mock_storage():
    storage = InMemoryStorage()
    app.dependency_overrides[get_storage_interface] = lambda: storage
    yield storage
    app.dependency_overrides = {}


@pytest.fixture
def client():
    return TestClient(app)


# --- the endpoint ---------------------------------------------------------

def test_presign_returns_a_form_post(client, mock_storage):
    response = client.post(
        "/api/files/presign",
        json={"filename": "photo.jpg", "content_type": "image/jpeg"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["key"].startswith("uploads/")
    assert data["key"].endswith("/photo.jpg")
    assert data["url"]
    assert data["fields"]["key"] == data["key"]
    assert data["expires_in"] == Settings().presign_expires_seconds
    assert data["max_bytes"] == Settings().max_upload_bytes


def test_server_chooses_the_key_not_the_client(client, mock_storage):
    """A client must not be able to aim its upload at an arbitrary key."""
    response = client.post(
        "/api/files/presign",
        json={"filename": "../../etc/passwd", "content_type": "text/plain"},
    )

    key = response.json()["key"]
    assert key.startswith("uploads/")
    assert ".." not in key


def test_two_presigns_never_collide(client, mock_storage):
    keys = {
        client.post("/api/files/presign", json={"filename": "same.jpg"}).json()["key"]
        for _ in range(5)
    }
    assert len(keys) == 5


def test_content_type_and_size_cap_reach_the_policy(client, mock_storage):
    client.post(
        "/api/files/presign",
        json={"filename": "clip.mp4", "content_type": "video/mp4"},
    )

    call = mock_storage.presigns[0]
    assert call["content_type"] == "video/mp4"
    assert call["max_bytes"] == Settings().max_upload_bytes


def test_filename_is_required(client, mock_storage):
    assert client.post("/api/files/presign", json={"filename": ""}).status_code == 422
    assert client.post("/api/files/presign", json={}).status_code == 422


def test_storage_failure_returns_500(client, mock_storage):
    mock_storage.should_fail = True
    response = client.post("/api/files/presign", json={"filename": "photo.jpg"})
    assert response.status_code == 500


def test_presign_targets_the_hot_tier(mock_storage):
    hot, cold = InMemoryStorage("hot"), InMemoryStorage("cold")
    tiered = TieredStorage(hot=hot, cold=cold)

    tiered.generate_presigned_upload("uploads/a/x.jpg", content_type="image/jpeg")

    assert len(hot.presigns) == 1
    assert cold.presigns == []


# --- policy construction --------------------------------------------------

def _presigning_storage(**kwargs):
    storage = S3Storage(
        endpoint_url="http://storage.internal:4566", access_key="k", secret_key="s",
        region="us-east-1", default_bucket="my-bucket", **kwargs,
    )
    storage._client = MagicMock()
    storage._client.generate_presigned_post.return_value = {
        "url": "http://storage.internal:4566/my-bucket", "fields": {},
    }
    return storage


def test_policy_pins_size_and_content_type():
    storage = _presigning_storage()

    storage.generate_presigned_upload(
        "uploads/a/x.jpg", content_type="image/jpeg", max_bytes=1024, expires_in=60,
    )

    kwargs = storage._client.generate_presigned_post.call_args.kwargs
    assert ["content-length-range", 0, 1024] in kwargs["Conditions"]
    assert {"Content-Type": "image/jpeg"} in kwargs["Conditions"]
    assert kwargs["Fields"]["Content-Type"] == "image/jpeg"
    assert kwargs["ExpiresIn"] == 60


def test_bucket_is_sent_as_a_form_field():
    """
    boto3 puts {"bucket": ...} in the policy conditions but omits the matching
    form field. Garage rejects that POST outright ("Key 'bucket' is required in
    policy"); AWS and LocalStack accept it either way. Verified against a real
    Garage node -- without this field the upload fails with 400 InvalidRequest.
    """
    storage = _presigning_storage()

    presigned = storage.generate_presigned_upload("uploads/a/x.jpg")

    assert presigned.fields["bucket"] == "my-bucket"


def test_bucket_field_from_the_backend_is_not_overwritten():
    storage = _presigning_storage()
    storage._client.generate_presigned_post.return_value = {
        "url": "http://storage.internal:4566/my-bucket",
        "fields": {"bucket": "signed-by-backend"},
    }

    presigned = storage.generate_presigned_upload("uploads/a/x.jpg")

    assert presigned.fields["bucket"] == "signed-by-backend"


def test_size_cap_applies_even_without_a_content_type():
    storage = _presigning_storage()

    storage.generate_presigned_upload("uploads/a/x.bin", content_type="", max_bytes=99)

    kwargs = storage._client.generate_presigned_post.call_args.kwargs
    assert kwargs["Conditions"] == [["content-length-range", 0, 99]]
    assert kwargs["Fields"] == {}   # no Content-Type pinned


def test_presign_signs_against_the_public_endpoint():
    """
    The browser cannot resolve the internal host, and the signature covers it,
    so the presign must be built against the public address.
    """
    storage = _presigning_storage(public_endpoint_url="https://cdn.example.com")

    assert storage.presign_client is not storage.client
    assert storage.presign_client.meta.endpoint_url == "https://cdn.example.com"


def test_presign_reuses_the_main_client_when_addresses_match():
    storage = _presigning_storage()
    assert storage.presign_client is storage.client
