import json

import pytest

from ticktask.core.auth import AuthManager
from ticktask.core.config import ConfigStore
from ticktask.core.errors import AmbiguousOperationError
from ticktask.core.errors import ConfirmationRequiredError
from ticktask.core.errors import ValidationError
from ticktask.core.services import TicktaskService


class FakeClient:
    def __init__(self):
        self.closed = False
        self.completed_calls = []
        self.updated_payloads = []
        self.filtered_payloads = []
        self.completed_task_calls = []
        self.deleted_task_calls = []
        self.moved_task_calls = []
        self.created_tasks = []
        self.created_projects = []

    def list_projects(self):
        return [{"id": "p1", "name": "Inbox"}]

    def project_data(self, project_id):
        return {
            "tasks": [
                {"id": "t1", "title": "Buy milk", "status": 0, "projectId": project_id},
                {"id": "t2", "title": "Done item", "status": 2, "projectId": project_id},
                {
                    "id": "t3",
                    "title": "Agent task",
                    "status": 0,
                    "projectId": project_id,
                    "tags": ["agent", "deep-work"],
                    "priority": 5,
                    "dueDate": "2026-01-01",
                },
                {
                    "id": "t4",
                    "title": "No date",
                    "status": 0,
                    "projectId": project_id,
                    "tags": ["agent"],
                },
            ]
        }

    def create_task(self, payload):
        self.created_tasks.append(payload)
        return {"id": "new", **payload}

    def complete_task(self, project_id, task_id):
        self.completed_task_calls.append({"project_id": project_id, "task_id": task_id})
        return {"completed": True, "projectId": project_id, "taskId": task_id}

    def get_task(self, project_id, task_id):
        return {
            "id": task_id,
            "title": "Loaded",
            "projectId": project_id,
            "tags": ["agent"],
            "kind": "CHECKLIST",
            "items": [
                {"id": "i1", "title": "Existing", "status": 0, "sortOrder": 1},
                {"id": "i2", "title": "Done", "status": 1, "sortOrder": 2},
            ],
        }

    def update_task(self, task_id, payload):
        self.updated_payloads.append(payload)
        return {"id": task_id, **payload}

    def delete_task(self, project_id, task_id):
        self.deleted_task_calls.append({"project_id": project_id, "task_id": task_id})
        return {"deleted": True, "projectId": project_id, "taskId": task_id}

    def move_task(self, task_id, from_project_id, to_project_id):
        self.moved_task_calls.append({"task_id": task_id, "from_project_id": from_project_id, "to_project_id": to_project_id})
        return {"moved": True, "taskId": task_id, "from": from_project_id, "to": to_project_id}

    def create_project(self, payload):
        self.created_projects.append(payload)
        return {"id": "new-project", **payload}

    def update_project(self, project_id, payload):
        return {"id": project_id, **payload}

    def delete_project(self, project_id):
        return {"deleted": True, "projectId": project_id}

    def completed_tasks(self, start_date=None, end_date=None, project_ids=None):
        self.completed_calls.append(
            {"start_date": start_date, "end_date": end_date, "project_ids": project_ids}
        )
        project_id = project_ids[0] if project_ids else "p1"
        return [{"id": "t2", "title": "Done item", "status": 2, "projectId": project_id}]

    def filter_tasks(self, payload):
        self.filtered_payloads.append(payload)
        return [
            {
                "id": "ft1",
                "title": "Filtered",
                "status": payload.get("status", 0),
                "projectId": (payload.get("projectIds") or ["p1"])[0],
                "tags": [payload.get("tag")],
            }
        ]

    def list_habits(self):
        return [
            {"id": "h1", "name": "Read", "status": 0, "totalCheckIns": 2},
            {"id": "h2", "name": "Write", "status": 0, "totalCheckIns": 5},
        ]

    def get_habit(self, habit_id):
        return {"id": habit_id, "name": "Read", "status": 0}

    def create_habit(self, payload):
        return {"id": "h-new", **payload}

    def update_habit(self, habit_id, payload):
        return {"id": habit_id, **payload}

    def checkin_habit(self, habit_id, payload):
        return {"habitId": habit_id, **payload}

    def habit_checkins(self, habit_ids, from_stamp, to_stamp):
        return [
            {"habitId": habit_ids[0], "from": from_stamp, "to": to_stamp, "checkins": [{"stamp": from_stamp}]},
            {"habitId": habit_ids[-1], "from": from_stamp, "to": to_stamp, "checkins": [{"stamp": to_stamp}]},
        ]

    def get_focus(self, focus_id, focus_type):
        return {"id": focus_id, "type": focus_type, "duration": 1500}

    def list_focuses(self, from_time, to_time, focus_type):
        return [{"id": "f1", "type": focus_type, "startTime": from_time, "endTime": to_time, "duration": 1500}]

    def delete_focus(self, focus_id, focus_type):
        return {"deleted": True, "focusId": focus_id, "type": focus_type}

    def close(self):
        self.closed = True


def service(tmp_path) -> TicktaskService:
    manager = AuthManager(ConfigStore(tmp_path / "config.json"))
    manager.init("ticktick", "client", "secret", "http://localhost", access_token="token")
    fake_client = FakeClient()
    svc = TicktaskService(auth=manager, client_factory=lambda _profile: fake_client)
    svc.client = fake_client  # type: ignore[attr-defined]
    return svc


def test_list_tasks_filters_open(tmp_path) -> None:
    tasks = service(tmp_path).list_tasks(status="open")
    assert [task["id"] for task in tasks] == ["t1", "t3", "t4"]


def test_search_tasks(tmp_path) -> None:
    tasks = service(tmp_path).search_tasks("milk")
    assert len(tasks) == 1
    assert tasks[0]["id"] == "t1"


def test_query_tasks_compiles_natural_query_and_filters_results(tmp_path) -> None:
    result = service(tmp_path).query_tasks('high priority #agent project:"Inbox" agent')

    assert result["compiled"] == {
        "status": "open",
        "filter_preset": "high-priority",
        "tag": "agent",
        "project": "Inbox",
        "search": "agent",
    }
    assert [task["id"] for task in result["tasks"]] == ["t3"]


def test_list_completed_uses_global_completed_query_without_project_ids(tmp_path) -> None:
    fake = FakeClient()
    manager = AuthManager(ConfigStore(tmp_path / "config.json"))
    manager.init("dida365", "client", "secret", "http://localhost", access_token="token")
    svc = TicktaskService(auth=manager, client_factory=lambda _profile: fake)

    tasks = svc.list_tasks(status="completed")

    assert tasks[0]["id"] == "t2"
    assert len(fake.completed_calls) == 1
    assert fake.completed_calls[0]["start_date"] == "1970-01-01"
    assert fake.completed_calls[0]["end_date"] is not None
    assert fake.completed_calls[0]["project_ids"] is None


def test_list_completed_with_project_passes_project_ids_and_dates(tmp_path) -> None:
    fake = FakeClient()
    manager = AuthManager(ConfigStore(tmp_path / "config.json"))
    manager.init("ticktick", "client", "secret", "http://localhost", access_token="token")
    svc = TicktaskService(auth=manager, client_factory=lambda _profile: fake)

    tasks = svc.list_tasks(
        project="Inbox",
        status="completed",
        start_date="2026-01-01",
        end_date="2026-01-31",
    )

    assert tasks[0]["id"] == "t2"
    assert fake.completed_calls == [
        {"start_date": "2026-01-01", "end_date": "2026-01-31", "project_ids": ["p1"]}
    ]


def test_task_analytics_summarizes_open_completed_projects_and_tags(tmp_path) -> None:
    fake = FakeClient()
    manager = AuthManager(ConfigStore(tmp_path / "config.json"))
    manager.init("dida365", "client", "secret", "http://localhost", access_token="token")
    svc = TicktaskService(auth=manager, client_factory=lambda _profile: fake)

    report = svc.task_analytics(preset="week")

    assert report["period"]["preset"] == "week"
    assert report["summary"]["open_count"] == 3
    assert report["summary"]["completed_count"] == 1
    assert report["summary"]["overdue_count"] == 1
    assert report["summary"]["total_count"] == 4
    assert report["project_throughput"] == [
        {"project_id": "p1", "project_name": "Inbox", "open_count": 3, "completed_count": 1, "overdue_count": 1}
    ]
    assert report["tag_distribution"] == {"agent": 2, "deep-work": 1}
    assert fake.completed_calls[0]["project_ids"] is None


def test_create_task_resolves_project(tmp_path) -> None:
    task = service(tmp_path).create_task("New task", project="Inbox", priority="high")
    assert task["id"] == "new"
    assert task["project_id"] == "p1"
    assert task["priority"] == 5


def test_create_task_idempotency_replays_without_duplicate_api_call(tmp_path) -> None:
    svc = service(tmp_path)

    first = svc.create_task("New task", project="Inbox", priority="high", idempotency_key="agent-key-1")
    second = svc.create_task("New task", project="Inbox", priority="high", idempotency_key="agent-key-1")

    assert first["id"] == "new"
    assert first["_idempotency"] == {"key": "agent-key-1", "replayed": False}
    assert second["id"] == "new"
    assert second["_idempotency"] == {"key": "agent-key-1", "replayed": True}
    assert len(svc.client.created_tasks) == 1  # type: ignore[attr-defined]


def test_create_task_idempotency_rejects_key_reuse_for_different_payload(tmp_path) -> None:
    svc = service(tmp_path)
    svc.create_task("New task", project="Inbox", priority="high", idempotency_key="agent-key-1")

    with pytest.raises(ValidationError) as exc:
        svc.create_task("Different task", project="Inbox", priority="high", idempotency_key="agent-key-1")

    assert "different payload" in exc.value.message


def test_create_task_idempotency_blocks_retry_after_unknown_write_failure(tmp_path) -> None:
    class FailingCreateClient(FakeClient):
        def create_task(self, payload):
            self.created_tasks.append(payload)
            raise RuntimeError("connection dropped")

    manager = AuthManager(ConfigStore(tmp_path / "config.json"))
    manager.init("ticktick", "client", "secret", "http://localhost", access_token="token")
    fake_client = FailingCreateClient()
    svc = TicktaskService(auth=manager, client_factory=lambda _profile: fake_client)

    with pytest.raises(RuntimeError):
        svc.create_task("New task", project="Inbox", priority="high", idempotency_key="agent-key-1")
    with pytest.raises(ValidationError) as exc:
        svc.create_task("New task", project="Inbox", priority="high", idempotency_key="agent-key-1")

    assert "failed before its result was recorded" in exc.value.message
    assert len(fake_client.created_tasks) == 1


def test_create_task_rejects_empty_title_before_api_call(tmp_path) -> None:
    svc = service(tmp_path)

    with pytest.raises(ValidationError) as exc:
        svc.create_task("   ")

    assert "non-empty title" in exc.value.message
    assert svc.client.created_tasks == []


def test_task_mutations_reject_empty_task_ids(tmp_path) -> None:
    svc = service(tmp_path)

    operations = [
        lambda: svc.complete_task("", "p1", confirmed=True),
        lambda: svc.get_task("", "p1"),
        lambda: svc.update_task("", "p1", title="Renamed"),
        lambda: svc.delete_task("", "p1", confirmed=True),
        lambda: svc.move_task("", "p1", "p2"),
        lambda: svc.set_task_reminders("", "p1", ["TRIGGER:PT10M"]),
        lambda: svc.clear_task_reminders("", "p1"),
        lambda: svc.set_task_repeat("", "p1", preset="weekly"),
        lambda: svc.clear_task_repeat("", "p1"),
        lambda: svc.add_task_tag("", "p1", "agent"),
        lambda: svc.remove_task_tag("", "p1", "agent"),
        lambda: svc.add_checklist_item("", "p1", "New item"),
        lambda: svc.update_checklist_item("", "p1", "i1", title="Renamed"),
        lambda: svc.delete_checklist_item("", "p1", "i1", confirmed=True),
    ]

    for operation in operations:
        with pytest.raises(AmbiguousOperationError):
            operation()


def test_update_task_rejects_empty_title(tmp_path) -> None:
    svc = service(tmp_path)

    with pytest.raises(ValidationError) as exc:
        svc.update_task("t1", "p1", title="   ")

    assert "Task title cannot be empty" in exc.value.message


def test_move_task_rejects_same_project(tmp_path) -> None:
    svc = service(tmp_path)

    with pytest.raises(ValidationError) as exc:
        svc.move_task("t1", "p1", "p1")

    assert "source and destination projects must differ" in exc.value.message


def test_complete_requires_confirmation(tmp_path) -> None:
    try:
        service(tmp_path).complete_task("t1", "p1", confirmed=False)
    except ConfirmationRequiredError as exc:
        assert exc.code == "CONFIRMATION_REQUIRED"
    else:
        raise AssertionError("expected ConfirmationRequiredError")


def test_task_crud_move_and_export(tmp_path) -> None:
    svc = service(tmp_path)
    assert svc.get_task("t1", "p1")["title"] == "Loaded"
    updated = svc.update_task("t1", "p1", title="Renamed", priority="medium")
    assert updated["title"] == "Renamed"
    assert updated["priority"] == 3
    deleted = svc.delete_task("t1", "p1", confirmed=True)
    assert deleted["result"]["deleted"] is True
    moved = svc.move_task("t1", "p1", "p2")
    assert moved["to_project_id"] == "p2"
    assert "Buy milk" in svc.export_tasks("markdown")


def test_export_focuses_serializes_focus_report(tmp_path) -> None:
    svc = service(tmp_path)
    content = svc.export_focuses("jsonl", from_time="2026-01-01", to_time="2026-01-30", focus_type=0)
    assert '"id": "f1"' in content
    assert '"duration_minutes": 25' in content


def test_update_requires_changed_field(tmp_path) -> None:
    try:
        service(tmp_path).update_task("t1", "p1")
    except ValidationError as exc:
        assert exc.code == "VALIDATION_ERROR"
    else:
        raise AssertionError("expected ValidationError")


def test_project_lifecycle_service_methods(tmp_path) -> None:
    svc = service(tmp_path)

    created = svc.create_project("Focus", color="#00aa00", view_mode="list", kind="TASK")
    assert created["id"] == "new-project"
    assert created["name"] == "Focus"
    assert created["raw"]["color"] == "#00aa00"

    updated = svc.update_project("p1", name="Renamed", color="#111111", closed=True)
    assert updated["id"] == "p1"
    assert updated["name"] == "Renamed"
    assert updated["raw"]["closed"] is True

    deleted = svc.delete_project("p1", confirmed=True)
    assert deleted == {"project_id": "p1", "result": {"deleted": True, "projectId": "p1"}}


def test_project_delete_requires_confirmation(tmp_path) -> None:
    try:
        service(tmp_path).delete_project("p1", confirmed=False)
    except ConfirmationRequiredError as exc:
        assert exc.code == "CONFIRMATION_REQUIRED"
    else:
        raise AssertionError("expected ConfirmationRequiredError")


def test_update_project_requires_changed_field(tmp_path) -> None:
    try:
        service(tmp_path).update_project("p1")
    except ValidationError as exc:
        assert exc.code == "VALIDATION_ERROR"
    else:
        raise AssertionError("expected ValidationError")


def test_checklist_item_crud_updates_task_items(tmp_path) -> None:
    fake = FakeClient()
    manager = AuthManager(ConfigStore(tmp_path / "config.json"))
    manager.init("ticktick", "client", "secret", "http://localhost", access_token="token")
    svc = TicktaskService(auth=manager, client_factory=lambda _profile: fake)

    added = svc.add_checklist_item("t1", "p1", "New item", item_id="i-new")
    assert added["kind"] == "CHECKLIST"
    assert added["items"][-1]["id"] == "i-new"
    assert fake.updated_payloads[-1]["items"][-1]["status"] == 0

    updated = svc.update_checklist_item("t1", "p1", "i1", title="Renamed", status=1)
    assert updated["items"][0]["title"] == "Renamed"
    assert updated["items"][0]["status"] == 1

    completed = svc.complete_checklist_item("t1", "p1", "i1")
    assert completed["items"][0]["status"] == 1

    deleted = svc.delete_checklist_item("t1", "p1", "i2", confirmed=True)
    assert [item["id"] for item in deleted["items"]] == ["i1"]


def test_checklist_item_delete_requires_confirmation(tmp_path) -> None:
    try:
        service(tmp_path).delete_checklist_item("t1", "p1", "i1", confirmed=False)
    except ConfirmationRequiredError as exc:
        assert exc.code == "CONFIRMATION_REQUIRED"
    else:
        raise AssertionError("expected ConfirmationRequiredError")


def test_checklist_item_update_requires_existing_item_and_change(tmp_path) -> None:
    svc = service(tmp_path)
    try:
        svc.update_checklist_item("t1", "p1", "missing", title="Nope")
    except ValidationError as exc:
        assert exc.code == "VALIDATION_ERROR"
    else:
        raise AssertionError("expected ValidationError")

    try:
        svc.update_checklist_item("t1", "p1", "i1")
    except ValidationError as exc:
        assert exc.code == "VALIDATION_ERROR"
    else:
        raise AssertionError("expected ValidationError")



def test_list_tasks_filters_by_tag_and_smart_filter(tmp_path) -> None:
    tagged = service(tmp_path).list_tasks(status="open", tag="deep-work")
    assert [task["id"] for task in tagged] == ["t3"]

    high_priority = service(tmp_path).list_tasks(status="open", filter_preset="high-priority")
    assert [task["id"] for task in high_priority] == ["t3"]

    no_date = service(tmp_path).list_tasks(status="open", filter_preset="no-date")
    assert [task["id"] for task in no_date] == ["t1", "t4"]


def test_filter_tasks_uses_open_api_filter_endpoint(tmp_path) -> None:
    fake = FakeClient()
    manager = AuthManager(ConfigStore(tmp_path / "config.json"))
    manager.init("ticktick", "client", "secret", "http://localhost", access_token="token")
    svc = TicktaskService(auth=manager, client_factory=lambda _profile: fake)

    tasks = svc.filter_tasks(tag="agent", project="Inbox", status="open", priority="high")

    assert tasks[0]["id"] == "ft1"
    assert fake.filtered_payloads == [
        {"tag": "agent", "status": 0, "projectIds": ["p1"], "priority": [5]}
    ]


def test_task_tag_mutation_helpers_preserve_existing_task_fields(tmp_path) -> None:
    fake = FakeClient()
    manager = AuthManager(ConfigStore(tmp_path / "config.json"))
    manager.init("ticktick", "client", "secret", "http://localhost", access_token="token")
    svc = TicktaskService(auth=manager, client_factory=lambda _profile: fake)

    added = svc.add_task_tag("t1", "p1", "deep-work")
    assert added["tags"] == ["agent", "deep-work"]
    assert fake.updated_payloads[-1]["tags"] == ["agent", "deep-work"]

    removed = svc.remove_task_tag("t1", "p1", "agent")
    assert removed["tags"] == []
    assert fake.updated_payloads[-1]["tags"] == []


def test_task_tag_mutation_validates_changes(tmp_path) -> None:
    svc = service(tmp_path)
    try:
        svc.add_task_tag("t1", "p1", "   ")
    except ValidationError as exc:
        assert exc.code == "VALIDATION_ERROR"
    else:
        raise AssertionError("expected ValidationError")

    try:
        svc.remove_task_tag("t1", "p1", "missing")
    except ValidationError as exc:
        assert exc.code == "VALIDATION_ERROR"
    else:
        raise AssertionError("expected ValidationError")



def test_habit_service_methods(tmp_path) -> None:
    svc = service(tmp_path)
    assert svc.list_habits()[0]["id"] == "h1"
    assert svc.get_habit("h1")["name"] == "Read"
    assert svc.create_habit("Write", goal=1, unit="time")["id"] == "h-new"
    updated = svc.update_habit("h1", name="Read more", goal=2)
    assert updated["name"] == "Read more"
    checkin = svc.checkin_habit("h1", stamp=20260101, value=1)
    assert checkin["habitId"] == "h1"
    history = svc.habit_checkins(["h1"], from_stamp=20260101, to_stamp=20260131)
    assert history[0]["habitId"] == "h1"


def test_habit_service_validates_inputs(tmp_path) -> None:
    svc = service(tmp_path)
    try:
        svc.create_habit("   ")
    except ValidationError as exc:
        assert exc.code == "VALIDATION_ERROR"
    else:
        raise AssertionError("expected ValidationError")

    try:
        svc.update_habit("h1")
    except ValidationError as exc:
        assert exc.code == "VALIDATION_ERROR"
    else:
        raise AssertionError("expected ValidationError")


def test_focus_service_methods_and_30_day_limit(tmp_path) -> None:
    svc = service(tmp_path)
    assert svc.get_focus("f1", focus_type=0)["id"] == "f1"
    focuses = svc.list_focuses("2026-01-01", "2026-01-30", focus_type=1)
    assert focuses[0]["focus_type"] == 1
    deleted = svc.delete_focus("f1", focus_type=0, confirmed=True)
    assert deleted["result"]["deleted"] is True

    try:
        svc.list_focuses("2026-01-01", "2026-02-15", focus_type=0)
    except ValidationError as exc:
        assert exc.code == "VALIDATION_ERROR"
    else:
        raise AssertionError("expected ValidationError")

    with pytest.raises(ValidationError) as invalid_date:
        svc.list_focuses("not-a-date", "2026-01-02", focus_type=0)
    assert "Invalid focus date" in invalid_date.value.message

    with pytest.raises(ValidationError) as reversed_range:
        svc.list_focuses("2026-01-03", "2026-01-02", focus_type=0)
    assert "--from must be on or before --to" in reversed_range.value.message


def test_focus_delete_requires_confirmation(tmp_path) -> None:
    try:
        service(tmp_path).delete_focus("f1", focus_type=0, confirmed=False)
    except ConfirmationRequiredError as exc:
        assert exc.code == "CONFIRMATION_REQUIRED"
    else:
        raise AssertionError("expected ConfirmationRequiredError")



def test_task_reminder_and_repeat_helpers_update_existing_task_payload(tmp_path) -> None:
    fake = FakeClient()
    manager = AuthManager(ConfigStore(tmp_path / "config.json"))
    manager.init("ticktick", "client", "secret", "http://localhost", access_token="token")
    svc = TicktaskService(auth=manager, client_factory=lambda _profile: fake)

    reminded = svc.set_task_reminders("t1", "p1", ["TRIGGER:PT10M", "2026-01-01T09:00:00+0000"])
    assert reminded["raw"]["reminders"] == ["TRIGGER:PT10M", "2026-01-01T09:00:00+0000"]
    assert fake.updated_payloads[-1]["projectId"] == "p1"
    assert fake.updated_payloads[-1]["reminders"] == ["TRIGGER:PT10M", "2026-01-01T09:00:00+0000"]

    repeated = svc.set_task_repeat("t1", "p1", preset="weekly")
    assert repeated["raw"]["repeatFlag"] == "RRULE:FREQ=WEEKLY"
    assert fake.updated_payloads[-1]["repeatFlag"] == "RRULE:FREQ=WEEKLY"

    cleared = svc.clear_task_repeat("t1", "p1")
    assert cleared["raw"]["repeatFlag"] == ""


def test_task_reminder_and_repeat_helpers_validate_inputs(tmp_path) -> None:
    svc = service(tmp_path)
    for call in (
        lambda: svc.set_task_reminders("t1", "p1", []),
        lambda: svc.set_task_repeat("t1", "p1"),
        lambda: svc.set_task_repeat("t1", "p1", preset="fortnightly"),
    ):
        try:
            call()
        except ValidationError as exc:
            assert exc.code == "VALIDATION_ERROR"
        else:
            raise AssertionError("expected ValidationError")



def test_batch_task_operations_dry_run_does_not_mutate(tmp_path) -> None:
    fake = FakeClient()
    manager = AuthManager(ConfigStore(tmp_path / "config.json"))
    manager.init("ticktick", "client", "secret", "http://localhost", access_token="token")
    svc = TicktaskService(auth=manager, client_factory=lambda _profile: fake)

    preview = svc.batch_complete_tasks(["t1", "t2"], project_id="p1", dry_run=True, confirmed=False)
    assert preview == {
        "action": "complete",
        "dry_run": True,
        "count": 2,
        "items": [
            {"task_id": "t1", "project_id": "p1"},
            {"task_id": "t2", "project_id": "p1"},
        ],
        "results": [],
    }
    assert fake.completed_task_calls == []

    move_preview = svc.batch_move_tasks(["t1"], from_project_id="p1", to_project_id="p2", dry_run=True, confirmed=False)
    assert move_preview["items"] == [{"task_id": "t1", "from_project_id": "p1", "to_project_id": "p2"}]
    assert fake.moved_task_calls == []


def test_batch_task_operations_require_confirmation_when_not_dry_run(tmp_path) -> None:
    svc = service(tmp_path)
    for call in (
        lambda: svc.batch_complete_tasks(["t1"], project_id="p1", dry_run=False, confirmed=False),
        lambda: svc.batch_delete_tasks(["t1"], project_id="p1", dry_run=False, confirmed=False),
        lambda: svc.batch_move_tasks(["t1"], from_project_id="p1", to_project_id="p2", dry_run=False, confirmed=False),
    ):
        try:
            call()
        except ConfirmationRequiredError as exc:
            assert exc.code == "CONFIRMATION_REQUIRED"
        else:
            raise AssertionError("expected ConfirmationRequiredError")


def test_batch_task_operations_execute_when_confirmed(tmp_path) -> None:
    fake = FakeClient()
    manager = AuthManager(ConfigStore(tmp_path / "config.json"))
    manager.init("ticktick", "client", "secret", "http://localhost", access_token="token")
    svc = TicktaskService(auth=manager, client_factory=lambda _profile: fake)

    completed = svc.batch_complete_tasks(["t1", "t2"], project_id="p1", dry_run=False, confirmed=True)
    assert completed["dry_run"] is False
    assert completed["count"] == 2
    assert [item["task_id"] for item in fake.completed_task_calls] == ["t1", "t2"]

    deleted = svc.batch_delete_tasks(["t3"], project_id="p1", dry_run=False, confirmed=True)
    assert deleted["results"][0]["result"]["deleted"] is True
    assert fake.deleted_task_calls == [{"project_id": "p1", "task_id": "t3"}]

    moved = svc.batch_move_tasks(["t4"], from_project_id="p1", to_project_id="p2", dry_run=False, confirmed=True)
    assert moved["results"][0]["result"]["moved"] is True
    assert fake.moved_task_calls == [{"task_id": "t4", "from_project_id": "p1", "to_project_id": "p2"}]


def test_batch_task_operations_validate_inputs(tmp_path) -> None:
    svc = service(tmp_path)
    for call in (
        lambda: svc.batch_complete_tasks([], project_id="p1"),
        lambda: svc.batch_delete_tasks(["t1"], project_id=""),
        lambda: svc.batch_move_tasks(["t1"], from_project_id="p1", to_project_id="p1"),
    ):
        try:
            call()
        except ValidationError as exc:
            assert exc.code == "VALIDATION_ERROR"
        else:
            raise AssertionError("expected ValidationError")


def test_backup_tasks_writes_project_date_exports_and_manifest(tmp_path) -> None:
    output_dir = tmp_path / "backups"
    result = service(tmp_path).backup_tasks(
        output_dir=output_dir,
        output_formats=["markdown", "jsonl", "csv"],
        backup_date="2026-05-17",
        project="Inbox",
        status="all",
    )

    assert result["backup_date"] == "2026-05-17"
    assert result["status"] == "all"
    assert result["project"] == "Inbox"
    assert result["count"] == 4
    assert result["output_dir"] == str(output_dir)
    paths = {item["format"]: output_dir / item["path"] for item in result["files"]}
    assert set(paths) == {"markdown", "jsonl", "csv"}
    assert paths["markdown"].read_text(encoding="utf-8").startswith("# Ticktask backup")
    assert '"title": "Buy milk"' in paths["jsonl"].read_text(encoding="utf-8")
    assert paths["csv"].read_text(encoding="utf-8").splitlines()[0] == "id,project_id,title,content,due_date,priority,status"

    manifest = json.loads((output_dir / result["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["backup_date"] == "2026-05-17"
    assert manifest["files"] == result["files"]


def test_backup_tasks_rejects_empty_formats(tmp_path) -> None:
    try:
        service(tmp_path).backup_tasks(output_dir=tmp_path, output_formats=[])
    except ValidationError as exc:
        assert exc.code == "VALIDATION_ERROR"
    else:
        raise AssertionError("expected ValidationError")


def test_progress_report_combines_tasks_habits_and_focus(tmp_path) -> None:
    report = service(tmp_path).progress_report(period="week", project="Inbox", focus_type=0)

    assert report["period"]["preset"] == "week"
    assert report["scope"]["project"] == "Inbox"
    assert report["tasks"]["open_count"] == 3
    assert report["tasks"]["completed_count"] == 1
    assert report["tasks"]["completion_rate"] == 0.25
    assert report["habits"]["habit_count"] == 2
    assert report["habits"]["checkin_count"] == 2
    assert report["habits"]["total_checkins_all_time"] == 7
    assert report["focus"]["session_count"] == 1
    assert report["focus"]["duration_minutes"] == 25
    assert report["scorecard"] == {
        "completed_tasks": 1,
        "habit_checkins": 2,
        "focus_minutes": 25,
        "overdue_tasks": 1,
    }

def test_create_task_normalizes_due_and_priority_alias(tmp_path) -> None:
    svc = service(tmp_path)

    task = svc.create_task("Relative date", project="Inbox", due="tomorrow", priority="p1")

    assert task["priority"] == 5
    created_payload = svc.client.created_tasks[-1]
    assert created_payload["dueDate"] != "tomorrow"
    assert created_payload["dueDate"].count("-") == 2
    assert created_payload["priority"] == 5


def test_create_project_validates_kind_and_view_mode_before_api_call(tmp_path) -> None:
    svc = service(tmp_path)

    with pytest.raises(ValidationError):
        svc.create_project("Bad", view_mode="board", kind="TASK")
    with pytest.raises(ValidationError):
        svc.create_project("Bad", view_mode="list", kind="folder")

    assert svc.client.created_projects == []


def test_filter_tasks_normalizes_status_priority_and_dates(tmp_path) -> None:
    svc = service(tmp_path)

    svc.filter_tasks(status="done", priority="p2", start_date="today", end_date="tomorrow")

    payload = svc.client.filtered_payloads[-1]
    assert payload["status"] == 2
    assert payload["priority"] == [3]
    assert payload["startDate"].count("-") == 2
    assert payload["endDate"].count("-") == 2
