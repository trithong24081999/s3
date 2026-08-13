from datetime import datetime, timedelta, timezone

import pytest

from app.storage.interface import StorageError, StorageObjectNotFoundError
from app.storage.tiered import TieredStorage
from app.tiering import move_object, run
from tests.fakes import InMemoryStorage


def days_ago(n):
    return datetime.now(timezone.utc) - timedelta(days=n)


@pytest.fixture
def tiers():
    return InMemoryStorage("hot"), InMemoryStorage("cold")


# --- the mover ------------------------------------------------------------

def test_moves_only_objects_past_the_cutoff(tiers):
    hot, cold = tiers
    hot.put("uploads/a/old.jpg", b"old bytes", last_modified=days_ago(45))
    hot.put("uploads/a/fresh.jpg", b"fresh bytes", last_modified=days_ago(3))

    stats = run(hot, cold, prefix="uploads/", max_age_days=30)

    assert stats.scanned == 2
    assert stats.eligible == 1
    assert stats.moved == 1
    assert "uploads/a/old.jpg" not in hot.objects
    assert cold.download_file("uploads/a/old.jpg") == b"old bytes"
    assert hot.download_file("uploads/a/fresh.jpg") == b"fresh bytes"
    assert "uploads/a/fresh.jpg" not in cold.objects


def test_dry_run_changes_nothing(tiers):
    hot, cold = tiers
    hot.put("uploads/a/old.jpg", b"old bytes", last_modified=days_ago(45))

    stats = run(hot, cold, prefix="uploads/", max_age_days=30, dry_run=True)

    assert stats.eligible == 1
    assert stats.moved == 0
    assert "uploads/a/old.jpg" in hot.objects
    assert cold.objects == {}


def test_move_preserves_content_type(tiers):
    hot, cold = tiers
    hot.put("uploads/a/old.jpg", b"jpeg bytes", content_type="image/jpeg",
            last_modified=days_ago(45))

    run(hot, cold, prefix="uploads/", max_age_days=30)

    assert cold.head_file("uploads/a/old.jpg").content_type == "image/jpeg"


def test_prefix_scopes_the_sweep(tiers):
    hot, cold = tiers
    hot.put("uploads/a/old.jpg", b"x", last_modified=days_ago(45))
    hot.put("other/old.jpg", b"x", last_modified=days_ago(45))

    run(hot, cold, prefix="uploads/", max_age_days=30)

    assert "other/old.jpg" in hot.objects
    assert list(cold.objects) == ["uploads/a/old.jpg"]


def test_limit_stops_the_pass(tiers):
    hot, cold = tiers
    for i in range(5):
        hot.put(f"uploads/a/{i}.jpg", b"x", last_modified=days_ago(45))

    stats = run(hot, cold, prefix="uploads/", max_age_days=30, limit=2)

    assert stats.moved == 2
    assert len(hot.objects) == 3


def test_hot_original_survives_a_failed_copy(tiers):
    hot, cold = tiers
    hot.put("uploads/a/old.jpg", b"old bytes", last_modified=days_ago(45))
    cold.should_fail = True

    stats = run(hot, cold, prefix="uploads/", max_age_days=30)

    assert stats.failed == 1
    assert stats.moved == 0
    # The object must still be readable from somewhere.
    assert hot.download_file("uploads/a/old.jpg") == b"old bytes"


def test_truncated_cold_copy_is_redone_not_trusted(tiers):
    hot, cold = tiers
    hot.put("uploads/a/old.jpg", b"the full body", last_modified=days_ago(45))
    cold.put("uploads/a/old.jpg", b"trunc")  # leftover from a crashed run

    reused = move_object(hot, cold, "uploads/a/old.jpg", expected_size=len(b"the full body"))

    assert reused is False
    assert cold.download_file("uploads/a/old.jpg") == b"the full body"


def test_complete_cold_copy_resumes_at_the_delete(tiers):
    hot, cold = tiers
    hot.put("uploads/a/old.jpg", b"body", last_modified=days_ago(45))
    cold.put("uploads/a/old.jpg", b"body")

    reused = move_object(hot, cold, "uploads/a/old.jpg", expected_size=4)

    assert reused is True
    assert cold.uploads == []          # no re-transfer
    assert "uploads/a/old.jpg" not in hot.objects


def test_size_mismatch_after_copy_keeps_the_original(tiers):
    hot, cold = tiers
    hot.put("uploads/a/old.jpg", b"body", last_modified=days_ago(45))

    with pytest.raises(StorageError):
        # Claiming the wrong size makes the post-copy verification fail.
        move_object(hot, cold, "uploads/a/old.jpg", expected_size=999)

    assert "uploads/a/old.jpg" in hot.objects


# --- read-through ---------------------------------------------------------

def test_reads_fall_through_to_cold(tiers):
    hot, cold = tiers
    cold.put("uploads/a/archived.jpg", b"archived bytes")
    tiered = TieredStorage(hot=hot, cold=cold)

    assert tiered.download_file("uploads/a/archived.jpg") == b"archived bytes"
    assert tiered.head_file("uploads/a/archived.jpg").size == len(b"archived bytes")


def test_hot_wins_when_both_tiers_hold_the_key(tiers):
    hot, cold = tiers
    hot.put("uploads/a/pic.jpg", b"hot copy")
    cold.put("uploads/a/pic.jpg", b"stale cold copy")
    tiered = TieredStorage(hot=hot, cold=cold)

    assert tiered.download_file("uploads/a/pic.jpg") == b"hot copy"
    assert [i.key for i in tiered.list_objects("uploads/")] == ["uploads/a/pic.jpg"]


def test_missing_in_both_tiers_raises_not_found(tiers):
    hot, cold = tiers
    tiered = TieredStorage(hot=hot, cold=cold)

    with pytest.raises(StorageObjectNotFoundError):
        tiered.download_file("uploads/a/nope.jpg")


def test_uploads_go_to_the_hot_tier(tiers):
    import io
    hot, cold = tiers
    tiered = TieredStorage(hot=hot, cold=cold)

    tiered.upload_file(io.BytesIO(b"new"), "uploads/a/new.jpg", content_type="image/jpeg")

    assert hot.download_file("uploads/a/new.jpg") == b"new"
    assert cold.objects == {}


def test_delete_clears_both_tiers(tiers):
    hot, cold = tiers
    hot.put("uploads/a/pic.jpg", b"hot")
    cold.put("uploads/a/pic.jpg", b"cold")
    tiered = TieredStorage(hot=hot, cold=cold)

    tiered.delete_file("uploads/a/pic.jpg")

    assert hot.objects == {}
    assert cold.objects == {}


def test_resize_serves_an_archived_object(tiers):
    """End-to-end: an object that has aged into Garage still resizes."""
    import io
    from fastapi.testclient import TestClient
    from PIL import Image
    from app.main import app
    from app.api.files import get_storage_interface

    hot, cold = tiers
    buffer = io.BytesIO()
    Image.new("RGB", (400, 200), "green").save(buffer, format="PNG")
    cold.put("uploads/a/archived.png", buffer.getvalue(), content_type="image/png")

    app.dependency_overrides[get_storage_interface] = lambda: TieredStorage(hot, cold)
    try:
        response = TestClient(app).get(
            "/api/files/resize", params={"key": "uploads/a/archived.png", "width": 100}
        )
    finally:
        app.dependency_overrides = {}

    assert response.status_code == 200
    with Image.open(io.BytesIO(response.content)) as img:
        assert img.size == (100, 50)
