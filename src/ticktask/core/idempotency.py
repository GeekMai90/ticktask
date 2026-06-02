from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import time
from typing import Any

from ticktask.core.config import config_dir
from ticktask.core.errors import ValidationError

try:  # pragma: no cover - platform dependent
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

try:  # pragma: no cover - platform dependent
    import msvcrt
except ImportError:  # pragma: no cover - POSIX fallback
    msvcrt = None  # type: ignore[assignment]


def idempotency_path() -> Path:
    return config_dir() / "idempotency.json"


@dataclass(frozen=True)
class IdempotencyClaim:
    claimed: bool
    replayed: bool
    result: dict[str, Any] | None = None


class IdempotencyStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path is not None else idempotency_path()

    def get(
        self,
        operation: str,
        key: str | None,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not key:
            return None
        with self._lock():
            data = self._load()
        entry = data.get(self._entry_key(operation, key))
        if entry is None:
            return None
        fingerprint = self._fingerprint(operation, payload)
        self._validate_fingerprint(key, entry, fingerprint)
        result = entry.get("result")
        return result if isinstance(result, dict) else None

    def reserve(
        self,
        operation: str,
        key: str | None,
        payload: dict[str, Any],
        *,
        wait_timeout: float = 30.0,
        poll_interval: float = 0.2,
    ) -> IdempotencyClaim:
        if not key:
            return IdempotencyClaim(claimed=True, replayed=False)
        entry_key = self._entry_key(operation, key)
        fingerprint = self._fingerprint(operation, payload)
        deadline = time.monotonic() + wait_timeout

        while True:
            with self._lock():
                data = self._load()
                entry = data.get(entry_key)
                if entry is None:
                    data[entry_key] = {
                        "operation": operation,
                        "key": key,
                        "fingerprint": fingerprint,
                        "status": "pending",
                        "created_at": self._now(),
                    }
                    self._save(data)
                    return IdempotencyClaim(claimed=True, replayed=False)

                self._validate_fingerprint(key, entry, fingerprint)
                status = entry.get("status") or (
                    "completed" if isinstance(entry.get("result"), dict) else "pending"
                )
                result = entry.get("result")
                if status == "completed" and isinstance(result, dict):
                    return IdempotencyClaim(claimed=False, replayed=True, result=result)
                if status == "failed":
                    raise ValidationError(
                        f"Idempotency key `{key}` failed before its result was recorded.",
                        hint=(
                            "Check remote tasks before retrying; use a new key only after "
                            "confirming no task was created."
                        ),
                    )

            if time.monotonic() >= deadline:
                raise ValidationError(
                    f"Idempotency key `{key}` is already in progress.",
                    hint=(
                        "Another task creation with this key is still pending; wait for it "
                        "to finish before retrying."
                    ),
                )
            time.sleep(poll_interval)

    def record(
        self,
        operation: str,
        key: str | None,
        payload: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        if not key:
            return
        fingerprint = self._fingerprint(operation, payload)
        with self._lock():
            data = self._load()
            entry = data.get(self._entry_key(operation, key))
            if entry is not None:
                self._validate_fingerprint(key, entry, fingerprint)
            data[self._entry_key(operation, key)] = {
                "operation": operation,
                "key": key,
                "fingerprint": fingerprint,
                "status": "completed",
                "result": result,
                "completed_at": self._now(),
            }
            self._save(data)

    def mark_failed(
        self,
        operation: str,
        key: str | None,
        payload: dict[str, Any],
        message: str,
    ) -> None:
        if not key:
            return
        fingerprint = self._fingerprint(operation, payload)
        with self._lock():
            data = self._load()
            entry = data.get(self._entry_key(operation, key))
            if entry is not None:
                self._validate_fingerprint(key, entry, fingerprint)
            data[self._entry_key(operation, key)] = {
                "operation": operation,
                "key": key,
                "fingerprint": fingerprint,
                "status": "failed",
                "error": message,
                "failed_at": self._now(),
            }
            self._save(data)

    @contextmanager
    def _lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a+", encoding="utf-8") as handle:
            self._lock_file(handle)
            try:
                yield
            finally:
                self._unlock_file(handle)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Idempotency store is not valid JSON: {self.path}") from exc
        if not isinstance(raw, dict):
            raise ValidationError(f"Idempotency store root must be a JSON object: {self.path}")
        return raw

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _entry_key(operation: str, key: str) -> str:
        return f"{operation}:{key}"

    @staticmethod
    def _fingerprint(operation: str, payload: dict[str, Any]) -> str:
        encoded = json.dumps(
            {"operation": operation, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_fingerprint(key: str, entry: dict[str, Any], fingerprint: str) -> None:
        if entry.get("fingerprint") != fingerprint:
            raise ValidationError(
                f"Idempotency key `{key}` was already used with a different payload.",
                hint="Use a new --idempotency-key when changing task creation arguments.",
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _lock_file(handle) -> None:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            return
        if msvcrt is not None:  # pragma: no cover - Windows fallback
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    @staticmethod
    def _unlock_file(handle) -> None:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return
        if msvcrt is not None:  # pragma: no cover - Windows fallback
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
