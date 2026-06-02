from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ticktask.core.config import config_dir
from ticktask.core.errors import ValidationError

SYNC_STATE_FILENAME = "sync-state.json"


def sync_state_path() -> Path:
    return config_dir() / SYNC_STATE_FILENAME


class SyncStateStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or sync_state_path()

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "states": {}}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValidationError("Sync state file root must be a JSON object.")
        states = raw.get("states")
        if not isinstance(states, dict):
            states = {}
        return {"version": int(raw.get("version") or 1), "states": states}

    def save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def get(self, state_key: str) -> dict[str, Any] | None:
        key = self._validate_state_key(state_key)
        state = self.load()
        value = state["states"].get(key)
        return dict(value) if isinstance(value, dict) else None

    def mark(self, state_key: str, timestamp: str | None = None) -> dict[str, Any]:
        key = self._validate_state_key(state_key)
        value = self._validate_timestamp(timestamp or utc_now())
        state = self.load()
        state.setdefault("states", {})[key] = {"last_synced_at": value}
        self.save(state)
        return {"state_key": key, "last_synced_at": value, "state_path": str(self.path)}

    def as_dict(self) -> dict[str, Any]:
        state = self.load()
        return {"version": state["version"], "states": state["states"], "state_path": str(self.path)}

    @staticmethod
    def _validate_state_key(state_key: str) -> str:
        key = state_key.strip()
        if not key:
            raise ValidationError("Sync state key cannot be empty.")
        return key

    @staticmethod
    def _validate_timestamp(timestamp: str) -> str:
        value = timestamp.strip()
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("Sync timestamp must be ISO-8601.") from exc
        return value


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
