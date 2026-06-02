import pytest

from ticktask.core.idempotency import IdempotencyStore
from ticktask.core.errors import ValidationError


def test_idempotency_store_replays_matching_fingerprint(tmp_path) -> None:
    store = IdempotencyStore(tmp_path / "idempotency.json")
    result = {"id": "t1", "title": "Write"}

    assert store.get("task.create", "key-1", {"title": "Write"}) is None
    store.record("task.create", "key-1", {"title": "Write"}, result)

    replayed = store.get("task.create", "key-1", {"title": "Write"})
    assert replayed == result


def test_idempotency_store_rejects_same_key_with_different_fingerprint(tmp_path) -> None:
    store = IdempotencyStore(tmp_path / "idempotency.json")
    store.record("task.create", "key-1", {"title": "Write"}, {"id": "t1"})

    with pytest.raises(ValidationError) as exc:
        store.get("task.create", "key-1", {"title": "Different"})

    assert exc.value.code == "VALIDATION_ERROR"
    assert "different payload" in exc.value.message


def test_idempotency_store_reserves_pending_key_before_remote_write(tmp_path) -> None:
    store = IdempotencyStore(tmp_path / "idempotency.json")

    claim = store.reserve("task.create", "key-1", {"title": "Write"})

    assert claim.claimed is True
    assert claim.replayed is False
    assert claim.result is None


def test_idempotency_store_rejects_duplicate_pending_key(tmp_path) -> None:
    store = IdempotencyStore(tmp_path / "idempotency.json")
    store.reserve("task.create", "key-1", {"title": "Write"})

    with pytest.raises(ValidationError) as exc:
        store.reserve("task.create", "key-1", {"title": "Write"}, wait_timeout=0)

    assert "already in progress" in exc.value.message


def test_idempotency_store_replays_after_pending_key_is_recorded(tmp_path) -> None:
    store = IdempotencyStore(tmp_path / "idempotency.json")
    store.reserve("task.create", "key-1", {"title": "Write"})
    store.record("task.create", "key-1", {"title": "Write"}, {"id": "t1", "title": "Write"})

    claim = store.reserve("task.create", "key-1", {"title": "Write"}, wait_timeout=0)

    assert claim.claimed is False
    assert claim.replayed is True
    assert claim.result == {"id": "t1", "title": "Write"}


def test_idempotency_store_blocks_key_after_failed_remote_write(tmp_path) -> None:
    store = IdempotencyStore(tmp_path / "idempotency.json")
    store.reserve("task.create", "key-1", {"title": "Write"})
    store.mark_failed("task.create", "key-1", {"title": "Write"}, "HTTP request failed")

    with pytest.raises(ValidationError) as exc:
        store.reserve("task.create", "key-1", {"title": "Write"}, wait_timeout=0)

    assert "failed before its result was recorded" in exc.value.message
