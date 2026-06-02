from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from ticktask.core.auth import AuthManager
from ticktask.core.client import TicktaskClient
from ticktask.core.config import ProfileConfig
from ticktask.core.dates import parse_date_range
from ticktask.core.diagnostics import create_diagnostic_bundle
from ticktask.core.errors import (
    AmbiguousOperationError,
    ConfirmationRequiredError,
    NotFoundError,
    ValidationError,
)
from ticktask.core.exporters import serialize_focuses, serialize_tasks
from ticktask.core.idempotency import IdempotencyStore
from ticktask.core.models import PRIORITY_MAP, Focus, Habit, Project, Task
from ticktask.core.natural_query import compile_task_query
from ticktask.core.sync_state import SyncStateStore, utc_now
from ticktask.core.validation import (
    normalize_priority,
    normalize_project_kind,
    normalize_task_date,
    normalize_task_status,
    normalize_view_mode,
)


ClientFactory = Callable[[ProfileConfig], TicktaskClient]

REPEAT_PRESETS = {
    "daily": "RRULE:FREQ=DAILY",
    "weekly": "RRULE:FREQ=WEEKLY",
    "monthly": "RRULE:FREQ=MONTHLY",
    "yearly": "RRULE:FREQ=YEARLY",
}


def default_client_factory(profile: ProfileConfig) -> TicktaskClient:
    return TicktaskClient(profile.base_url, profile.access_token or "")


class TicktaskService:
    def __init__(
        self,
        auth: AuthManager | None = None,
        service: str | None = None,
        client_factory: ClientFactory | None = None,
        idempotency_store: IdempotencyStore | None = None,
    ) -> None:
        self.auth = auth or AuthManager()
        self.service = service
        self.client_factory = client_factory or default_client_factory
        self.idempotency_store = idempotency_store or IdempotencyStore(self.auth.store.path.parent / "idempotency.json")

    def _with_client(self) -> TicktaskClient:
        profile = self.auth.require_token(self.service)
        return self.client_factory(profile)

    def diagnostic_bundle(self, output_path: str | Path) -> dict[str, Any]:
        return create_diagnostic_bundle(output_path=output_path, store=self.auth.store)

    def list_projects(self) -> list[dict[str, Any]]:
        client = self._with_client()
        try:
            return [Project.from_api(item).to_dict() for item in client.list_projects()]
        finally:
            client.close()

    def get_project_data(self, project_id: str) -> dict[str, Any]:
        client = self._with_client()
        try:
            return client.project_data(project_id)
        finally:
            client.close()

    def create_project(
        self,
        name: str,
        color: str | None = None,
        sort_order: int | None = None,
        view_mode: str | None = None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        if not name.strip():
            raise ValidationError("Creating a project requires a non-empty name.")
        normalized_view_mode = normalize_view_mode(view_mode)
        normalized_kind = normalize_project_kind(kind)
        payload: dict[str, Any] = {"name": name}
        if color is not None:
            payload["color"] = color
        if sort_order is not None:
            payload["sortOrder"] = sort_order
        if normalized_view_mode is not None:
            payload["viewMode"] = normalized_view_mode
        if normalized_kind is not None:
            payload["kind"] = normalized_kind

        client = self._with_client()
        try:
            return Project.from_api(client.create_project(payload)).to_dict()
        finally:
            client.close()

    def update_project(
        self,
        project_id: str,
        name: str | None = None,
        color: str | None = None,
        sort_order: int | None = None,
        view_mode: str | None = None,
        kind: str | None = None,
        closed: bool | None = None,
    ) -> dict[str, Any]:
        if not project_id:
            raise AmbiguousOperationError("Updating a project requires `project_id`.")
        normalized_view_mode = normalize_view_mode(view_mode)
        normalized_kind = normalize_project_kind(kind)
        payload: dict[str, Any] = {"id": project_id}
        if name is not None:
            if not name.strip():
                raise ValidationError("Project name cannot be empty.")
            payload["name"] = name
        if color is not None:
            payload["color"] = color
        if sort_order is not None:
            payload["sortOrder"] = sort_order
        if normalized_view_mode is not None:
            payload["viewMode"] = normalized_view_mode
        if normalized_kind is not None:
            payload["kind"] = normalized_kind
        if closed is not None:
            payload["closed"] = closed
        if len(payload) == 1:
            raise ValidationError("Updating a project requires at least one changed field.")

        client = self._with_client()
        try:
            return Project.from_api(client.update_project(project_id, payload)).to_dict()
        finally:
            client.close()

    def delete_project(self, project_id: str, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            raise ConfirmationRequiredError("Deleting a project requires explicit confirmation.")
        if not project_id:
            raise AmbiguousOperationError("Deleting a project requires `project_id`.")
        client = self._with_client()
        try:
            result = client.delete_project(project_id)
            return {"project_id": project_id, "result": result}
        finally:
            client.close()

    def list_tasks(
        self,
        project: str | None = None,
        status: str = "open",
        today_only: bool = False,
        start_date: str | date | datetime | None = None,
        end_date: str | date | datetime | None = None,
        tag: str | None = None,
        filter_preset: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized_status = normalize_task_status(status) or "open"
        client = self._with_client()
        try:
            projects = [Project.from_api(item) for item in client.list_projects()]
            selected = self._select_projects(projects, project)
            tasks: list[Task] = []
            if normalized_status == "completed":
                completed_range = parse_date_range(
                    from_date=str(start_date) if start_date is not None else None,
                    to_date=str(end_date) if end_date is not None else None,
                )
                if project:
                    project_ids = [item.id for item in selected]
                    data = client.completed_tasks(
                        start_date=completed_range.start,
                        end_date=completed_range.end,
                        project_ids=project_ids,
                    )
                    project_id = project_ids[0] if len(project_ids) == 1 else None
                    for raw_task in self._extract_tasks(data):
                        tasks.append(Task.from_api(raw_task, project_id=project_id))
                else:
                    data = client.completed_tasks(
                        start_date=completed_range.start,
                        end_date=completed_range.end,
                    )
                    for raw_task in self._extract_tasks(data):
                        tasks.append(Task.from_api(raw_task))
            else:
                for item in selected:
                    data = client.project_data(item.id)
                    for raw_task in self._extract_tasks(data):
                        tasks.append(Task.from_api(raw_task, project_id=item.id))
            if normalized_status == "open":
                tasks = [task for task in tasks if not task.is_completed()]
            elif normalized_status == "completed":
                pass
            if today_only:
                today = date.today().isoformat()
                tasks = [task for task in tasks if task.due_date and task.due_date[:10] == today]
            tasks = self._apply_task_filters(tasks, tag=tag, filter_preset=filter_preset)
            return [task.to_dict() for task in tasks]
        finally:
            client.close()

    def filter_tasks(
        self,
        tag: str | None = None,
        project: str | None = None,
        status: str = "open",
        priority: str | None = None,
        start_date: str | date | datetime | None = None,
        end_date: str | date | datetime | None = None,
    ) -> list[dict[str, Any]]:
        normalized_status = normalize_task_status(status) or "open"
        normalized_priority = normalize_priority(priority)
        normalized_start_date = normalize_task_date(start_date) if start_date is not None else None
        normalized_end_date = normalize_task_date(end_date) if end_date is not None else None
        payload: dict[str, Any] = {}
        if tag is not None:
            normalized_tag = self._normalize_tag(tag)
            payload["tag"] = normalized_tag
        if normalized_status == "open":
            payload["status"] = 0
        elif normalized_status == "completed":
            payload["status"] = 2
        if normalized_priority is not None:
            payload["priority"] = [PRIORITY_MAP[normalized_priority]]
        if normalized_start_date is not None:
            payload["startDate"] = normalized_start_date
        if normalized_end_date is not None:
            payload["endDate"] = normalized_end_date

        client = self._with_client()
        try:
            if project:
                projects = [Project.from_api(item) for item in client.list_projects()]
                selected = self._select_one_project(projects, project)
                payload["projectIds"] = [selected.id]
            data = client.filter_tasks(payload)
            return [Task.from_api(raw_task).to_dict() for raw_task in self._extract_tasks(data)]
        finally:
            client.close()

    def completed_tasks(
        self,
        preset: str | None = None,
        start_date: str | date | datetime | None = None,
        end_date: str | date | datetime | None = None,
        project: str | None = None,
    ) -> list[dict[str, Any]]:
        range_ = parse_date_range(
            preset,
            str(start_date) if start_date is not None else None,
            str(end_date) if end_date is not None else None,
        )
        return self.list_tasks(
            project=project,
            status="completed",
            start_date=range_.start,
            end_date=range_.end,
        )

    def task_analytics(
        self,
        preset: str | None = None,
        start_date: str | date | datetime | None = None,
        end_date: str | date | datetime | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        range_ = parse_date_range(
            preset,
            str(start_date) if start_date is not None else None,
            str(end_date) if end_date is not None else None,
        )
        today = date.today().isoformat()
        client = self._with_client()
        try:
            projects = [Project.from_api(item) for item in client.list_projects()]
            selected = self._select_projects(projects, project)
            project_names = {item.id: item.name for item in selected}
            project_stats = {
                item.id: {
                    "project_id": item.id,
                    "project_name": item.name,
                    "open_count": 0,
                    "completed_count": 0,
                    "overdue_count": 0,
                }
                for item in selected
            }

            open_tasks: list[Task] = []
            for item in selected:
                data = client.project_data(item.id)
                for raw_task in self._extract_tasks(data):
                    task = Task.from_api(raw_task, project_id=item.id)
                    if task.is_completed():
                        continue
                    open_tasks.append(task)
                    stats = project_stats.setdefault(
                        task.project_id or item.id,
                        {
                            "project_id": task.project_id or item.id,
                            "project_name": project_names.get(task.project_id or item.id, task.project_id or item.id),
                            "open_count": 0,
                            "completed_count": 0,
                            "overdue_count": 0,
                        },
                    )
                    stats["open_count"] += 1
                    if self._is_overdue(task, today):
                        stats["overdue_count"] += 1

            completed_data = client.completed_tasks(
                start_date=range_.start,
                end_date=range_.end,
                project_ids=[item.id for item in selected] if project else None,
            )
            completed_tasks = [Task.from_api(raw_task) for raw_task in self._extract_tasks(completed_data)]
            if project:
                selected_ids = set(project_stats)
                completed_tasks = [task for task in completed_tasks if task.project_id in selected_ids]
            for task in completed_tasks:
                project_id = task.project_id or (selected[0].id if len(selected) == 1 else None)
                if not project_id:
                    continue
                stats = project_stats.setdefault(
                    project_id,
                    {
                        "project_id": project_id,
                        "project_name": project_names.get(project_id, project_id),
                        "open_count": 0,
                        "completed_count": 0,
                        "overdue_count": 0,
                    },
                )
                stats["completed_count"] += 1

            tag_counts: Counter[str] = Counter()
            priority_counts: Counter[str] = Counter()
            for task in open_tasks + completed_tasks:
                tag_counts.update(task.tags)
                priority_counts.update([self._priority_label(task.priority)])

            overdue_count = sum(1 for task in open_tasks if self._is_overdue(task, today))
            return {
                "period": {"preset": preset, "start": range_.start, "end": range_.end},
                "scope": {"project": project},
                "summary": {
                    "open_count": len(open_tasks),
                    "completed_count": len(completed_tasks),
                    "overdue_count": overdue_count,
                    "total_count": len(open_tasks) + len(completed_tasks),
                },
                "project_throughput": list(project_stats.values()),
                "tag_distribution": dict(sorted(tag_counts.items())),
                "priority_distribution": dict(sorted(priority_counts.items())),
            }
        finally:
            client.close()

    def progress_report(
        self,
        period: str | None = None,
        start_date: str | date | datetime | None = None,
        end_date: str | date | datetime | None = None,
        project: str | None = None,
        focus_type: int = 0,
    ) -> dict[str, Any]:
        range_ = parse_date_range(
            period,
            str(start_date) if start_date is not None else None,
            str(end_date) if end_date is not None else None,
        )
        analytics = self.task_analytics(
            preset=None,
            start_date=range_.start,
            end_date=range_.end,
            project=project,
        )
        summary = analytics["summary"]
        total_tasks = int(summary.get("total_count") or 0)
        completed_tasks = int(summary.get("completed_count") or 0)
        open_tasks = int(summary.get("open_count") or 0)
        overdue_tasks = int(summary.get("overdue_count") or 0)
        completion_rate = round(completed_tasks / total_tasks, 4) if total_tasks else 0
        overdue_rate = round(overdue_tasks / open_tasks, 4) if open_tasks else 0

        habits = self.list_habits()
        habit_ids = [habit["id"] for habit in habits if habit.get("id")]
        habit_history = self.habit_checkins(
            habit_ids=habit_ids,
            from_stamp=self._date_to_stamp(range_.start),
            to_stamp=self._date_to_stamp(range_.end),
        ) if habit_ids else []
        habit_checkin_count = sum(len(item.get("checkins") or []) for item in habit_history if isinstance(item, dict))
        total_checkins_all_time = sum(int(habit.get("total_checkins") or 0) for habit in habits)

        focuses = self.list_focuses(from_time=range_.start, to_time=range_.end, focus_type=focus_type)
        focus_seconds = sum(int(focus.get("duration") or 0) for focus in focuses)
        focus_minutes = round(focus_seconds / 60, 2)
        if focus_minutes == int(focus_minutes):
            focus_minutes = int(focus_minutes)

        return {
            "period": {"preset": period, "start": range_.start, "end": range_.end},
            "scope": {"project": project, "focus_type": focus_type},
            "tasks": {
                **summary,
                "completion_rate": completion_rate,
                "overdue_rate": overdue_rate,
                "project_throughput": analytics.get("project_throughput", []),
                "tag_distribution": analytics.get("tag_distribution", {}),
                "priority_distribution": analytics.get("priority_distribution", {}),
            },
            "habits": {
                "habit_count": len(habits),
                "checkin_count": habit_checkin_count,
                "total_checkins_all_time": total_checkins_all_time,
                "history_count": len(habit_history),
            },
            "focus": {
                "session_count": len(focuses),
                "duration_seconds": focus_seconds,
                "duration_minutes": focus_minutes,
            },
            "scorecard": {
                "completed_tasks": completed_tasks,
                "habit_checkins": habit_checkin_count,
                "focus_minutes": focus_minutes,
                "overdue_tasks": overdue_tasks,
            },
        }

    @staticmethod
    def _date_to_stamp(value: str) -> int:
        return int(value[:10].replace("-", ""))

    @staticmethod
    def _is_overdue(task: Task, today: str) -> bool:
        return bool(task.due_date and task.due_date[:10] < today and not task.is_completed())

    @staticmethod
    def _priority_label(priority: int | str | None) -> str:
        mapping = {0: "none", "0": "none", 1: "low", "1": "low", 3: "medium", "3": "medium", 5: "high", "5": "high"}
        return mapping.get(priority, str(priority or "none"))

    def search_tasks(self, query: str, status: str = "all") -> list[dict[str, Any]]:
        needle = query.casefold()
        tasks = [
            Task.from_api(task)
            for task in self.list_tasks(project=None, status=status)
            if needle in (task.get("title") or "").casefold()
            or needle in (task.get("content") or "").casefold()
            or needle in (task.get("id") or "").casefold()
        ]
        return [task.to_dict() for task in tasks]

    def query_tasks(self, query: str) -> dict[str, Any]:
        compiled = compile_task_query(query)
        tasks = self.list_tasks(
            project=compiled.get("project"),
            status=compiled.get("status", "open"),
            start_date=compiled.get("start_date"),
            end_date=compiled.get("end_date"),
            tag=compiled.get("tag"),
            filter_preset=compiled.get("filter_preset"),
        )
        search = compiled.get("search")
        if search:
            needle = str(search).casefold()
            tasks = [
                task
                for task in tasks
                if needle in (task.get("title") or "").casefold()
                or needle in (task.get("content") or "").casefold()
                or needle in (task.get("id") or "").casefold()
            ]
        return {"query": query, "compiled": compiled, "tasks": tasks}

    def create_task(
        self,
        title: str,
        project: str | None = None,
        content: str | None = None,
        due: str | None = None,
        priority: str = "none",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not title.strip():
            raise ValidationError("Creating a task requires a non-empty title.")
        normalized_due = normalize_task_date(due) if due else None
        normalized_priority = normalize_priority(priority) or "none"
        payload: dict[str, Any] = {"title": title}
        if content:
            payload["content"] = content
        if normalized_due:
            payload["dueDate"] = normalized_due
        if normalized_priority:
            payload["priority"] = PRIORITY_MAP[normalized_priority]

        client = self._with_client()
        try:
            if project:
                projects = [Project.from_api(item) for item in client.list_projects()]
                selected = self._select_one_project(projects, project)
                payload["projectId"] = selected.id
            claim = self.idempotency_store.reserve("task.create", idempotency_key, payload)
            if claim.replayed and claim.result is not None:
                task = dict(claim.result)
                task["_idempotency"] = {"key": idempotency_key, "replayed": True}
                return task
            try:
                task = Task.from_api(
                    client.create_task(payload),
                    payload.get("projectId"),
                ).to_dict()
            except Exception as exc:
                self.idempotency_store.mark_failed(
                    "task.create",
                    idempotency_key,
                    payload,
                    str(exc),
                )
                raise
            self.idempotency_store.record("task.create", idempotency_key, payload, task)
            if idempotency_key:
                task["_idempotency"] = {"key": idempotency_key, "replayed": False}
            return task
        finally:
            client.close()


    def add_task_tag(self, task_id: str, project_id: str, tag: str) -> dict[str, Any]:
        self._require_task_id(task_id, "Adding a task tag")
        if not project_id:
            raise AmbiguousOperationError("Adding a task tag requires `project_id`.")
        normalized_tag = self._normalize_tag(tag)
        client = self._with_client()
        try:
            task = client.get_task(project_id, task_id)
            tags = self._task_tags(task)
            if normalized_tag in tags:
                raise ValidationError(f"Task already has tag `{normalized_tag}`.")
            tags.append(normalized_tag)
            payload = self._tag_update_payload(task, task_id, project_id, tags)
            return Task.from_api(client.update_task(task_id, payload), project_id=project_id).to_dict()
        finally:
            client.close()

    def remove_task_tag(self, task_id: str, project_id: str, tag: str) -> dict[str, Any]:
        self._require_task_id(task_id, "Removing a task tag")
        if not project_id:
            raise AmbiguousOperationError("Removing a task tag requires `project_id`.")
        normalized_tag = self._normalize_tag(tag)
        client = self._with_client()
        try:
            task = client.get_task(project_id, task_id)
            tags = self._task_tags(task)
            if normalized_tag not in tags:
                raise ValidationError(f"Task does not have tag `{normalized_tag}`.")
            payload = self._tag_update_payload(
                task,
                task_id,
                project_id,
                [existing for existing in tags if existing != normalized_tag],
            )
            return Task.from_api(client.update_task(task_id, payload), project_id=project_id).to_dict()
        finally:
            client.close()


    def add_checklist_item(
        self,
        task_id: str,
        project_id: str,
        title: str,
        item_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_task_id(task_id, "Adding a checklist item")
        if not project_id:
            raise AmbiguousOperationError("Adding a checklist item requires `project_id`.")
        if not title.strip():
            raise ValidationError("Checklist item title cannot be empty.")
        client = self._with_client()
        try:
            task = client.get_task(project_id, task_id)
            items = self._task_items(task)
            next_sort_order = max([int(item.get("sortOrder") or 0) for item in items] or [0]) + 1
            items.append(
                {
                    "id": item_id or uuid4().hex,
                    "title": title,
                    "status": 0,
                    "sortOrder": next_sort_order,
                }
            )
            payload = self._checklist_update_payload(task, task_id, project_id, items)
            return Task.from_api(client.update_task(task_id, payload), project_id=project_id).to_dict()
        finally:
            client.close()

    def update_checklist_item(
        self,
        task_id: str,
        project_id: str,
        item_id: str,
        title: str | None = None,
        status: int | str | None = None,
    ) -> dict[str, Any]:
        self._require_task_id(task_id, "Updating a checklist item")
        if not project_id:
            raise AmbiguousOperationError("Updating a checklist item requires `project_id`.")
        if title is None and status is None:
            raise ValidationError("Updating a checklist item requires at least one changed field.")
        parsed_status = self._parse_checklist_status(status) if status is not None else None
        client = self._with_client()
        try:
            task = client.get_task(project_id, task_id)
            items = self._task_items(task)
            found = False
            for item in items:
                if str(item.get("id")) == item_id:
                    found = True
                    if title is not None:
                        if not title.strip():
                            raise ValidationError("Checklist item title cannot be empty.")
                        item["title"] = title
                    if parsed_status is not None:
                        item["status"] = parsed_status
                    break
            if not found:
                raise ValidationError(f"Checklist item `{item_id}` was not found.")
            payload = self._checklist_update_payload(task, task_id, project_id, items)
            return Task.from_api(client.update_task(task_id, payload), project_id=project_id).to_dict()
        finally:
            client.close()

    def complete_checklist_item(self, task_id: str, project_id: str, item_id: str) -> dict[str, Any]:
        return self.update_checklist_item(task_id, project_id, item_id, status=1)

    def delete_checklist_item(
        self,
        task_id: str,
        project_id: str,
        item_id: str,
        confirmed: bool,
    ) -> dict[str, Any]:
        if not confirmed:
            raise ConfirmationRequiredError("Deleting a checklist item requires explicit confirmation.")
        self._require_task_id(task_id, "Deleting a checklist item")
        if not project_id:
            raise AmbiguousOperationError("Deleting a checklist item requires `project_id`.")
        client = self._with_client()
        try:
            task = client.get_task(project_id, task_id)
            items = self._task_items(task)
            filtered = [item for item in items if str(item.get("id")) != item_id]
            if len(filtered) == len(items):
                raise ValidationError(f"Checklist item `{item_id}` was not found.")
            payload = self._checklist_update_payload(task, task_id, project_id, filtered)
            return Task.from_api(client.update_task(task_id, payload), project_id=project_id).to_dict()
        finally:
            client.close()


    @staticmethod
    def _normalize_tag(tag: str) -> str:
        normalized = tag.strip().lstrip("#")
        if not normalized:
            raise ValidationError("Tag cannot be empty.")
        return normalized

    @staticmethod
    def _task_tags(task: dict[str, Any]) -> list[str]:
        return [str(tag) for tag in (task.get("tags") or []) if tag is not None]

    @staticmethod
    def _tag_update_payload(
        task: dict[str, Any],
        task_id: str,
        project_id: str,
        tags: list[str],
    ) -> dict[str, Any]:
        payload = dict(task)
        payload["id"] = task_id
        payload["projectId"] = project_id
        payload["tags"] = tags
        return payload

    @staticmethod
    def _apply_task_filters(
        tasks: list[Task],
        tag: str | None = None,
        filter_preset: str | None = None,
    ) -> list[Task]:
        filtered = tasks
        if tag is not None:
            normalized_tag = TicktaskService._normalize_tag(tag)
            filtered = [task for task in filtered if normalized_tag in task.tags]
        if filter_preset is None:
            return filtered
        today = date.today().isoformat()
        if filter_preset == "today":
            return [task for task in filtered if task.due_date and task.due_date[:10] == today]
        if filter_preset == "overdue":
            return [task for task in filtered if task.due_date and task.due_date[:10] < today]
        if filter_preset == "upcoming":
            return [task for task in filtered if task.due_date and task.due_date[:10] > today]
        if filter_preset == "high-priority":
            return [task for task in filtered if task.priority in {5, "5", "high"}]
        if filter_preset == "no-date":
            return [task for task in filtered if not task.due_date]
        raise NotFoundError(
            f"Unknown smart filter `{filter_preset}`.",
            hint="Use one of: today, overdue, upcoming, high-priority, no-date.",
        )

    @staticmethod
    def _task_items(task: dict[str, Any]) -> list[dict[str, Any]]:
        return [dict(item) for item in task.get("items") or []]

    @staticmethod
    def _checklist_update_payload(
        task: dict[str, Any],
        task_id: str,
        project_id: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = dict(task)
        payload["id"] = task_id
        payload["projectId"] = project_id
        payload["kind"] = "CHECKLIST"
        payload["items"] = items
        return payload

    @staticmethod
    def _parse_checklist_status(status: int | str | None) -> int:
        if status in {1, "1", "completed", "complete", "done"}:
            return 1
        if status in {0, "0", "open", "normal", "todo"}:
            return 0
        raise ValidationError("Checklist item status must be open or completed.")

    def batch_complete_tasks(
        self,
        task_ids: list[str],
        project_id: str,
        dry_run: bool = True,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        items = self._batch_project_items(task_ids, project_id)
        self._validate_batch_execution("Completing tasks in batch", dry_run, confirmed)
        if dry_run:
            return self._batch_result("complete", True, items, [])
        client = self._with_client()
        try:
            results = [
                {**item, "result": client.complete_task(project_id, item["task_id"])}
                for item in items
            ]
            return self._batch_result("complete", False, items, results)
        finally:
            client.close()

    def batch_delete_tasks(
        self,
        task_ids: list[str],
        project_id: str,
        dry_run: bool = True,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        items = self._batch_project_items(task_ids, project_id)
        self._validate_batch_execution("Deleting tasks in batch", dry_run, confirmed)
        if dry_run:
            return self._batch_result("delete", True, items, [])
        client = self._with_client()
        try:
            results = [
                {**item, "result": client.delete_task(project_id, item["task_id"])}
                for item in items
            ]
            return self._batch_result("delete", False, items, results)
        finally:
            client.close()

    def batch_move_tasks(
        self,
        task_ids: list[str],
        from_project_id: str,
        to_project_id: str,
        dry_run: bool = True,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        task_ids = self._normalize_batch_task_ids(task_ids)
        if not from_project_id or not to_project_id:
            raise ValidationError("Batch move requires source and destination project IDs.")
        if from_project_id == to_project_id:
            raise ValidationError("Batch move source and destination projects must differ.")
        items = [
            {"task_id": task_id, "from_project_id": from_project_id, "to_project_id": to_project_id}
            for task_id in task_ids
        ]
        self._validate_batch_execution("Moving tasks in batch", dry_run, confirmed)
        if dry_run:
            return self._batch_result("move", True, items, [])
        client = self._with_client()
        try:
            results = [
                {
                    **item,
                    "result": client.move_task(item["task_id"], from_project_id, to_project_id),
                }
                for item in items
            ]
            return self._batch_result("move", False, items, results)
        finally:
            client.close()

    @staticmethod
    def _normalize_batch_task_ids(task_ids: list[str]) -> list[str]:
        normalized = [task_id.strip() for task_id in task_ids if task_id and task_id.strip()]
        if not normalized:
            raise ValidationError("Batch task operations require at least one task ID.")
        if len(set(normalized)) != len(normalized):
            raise ValidationError("Batch task operations require unique task IDs.")
        return normalized

    @staticmethod
    def _batch_project_items(task_ids: list[str], project_id: str) -> list[dict[str, str]]:
        normalized = TicktaskService._normalize_batch_task_ids(task_ids)
        if not project_id:
            raise ValidationError("Batch task operations require `project_id`.")
        return [{"task_id": task_id, "project_id": project_id} for task_id in normalized]

    @staticmethod
    def _validate_batch_execution(action: str, dry_run: bool, confirmed: bool) -> None:
        if not dry_run and not confirmed:
            raise ConfirmationRequiredError(f"{action} requires explicit confirmation.")

    @staticmethod
    def _batch_result(
        action: str,
        dry_run: bool,
        items: list[dict[str, Any]],
        results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "action": action,
            "dry_run": dry_run,
            "count": len(items),
            "items": items,
            "results": results,
        }

    def complete_task(self, task_id: str, project_id: str, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            raise ConfirmationRequiredError("Completing a task requires explicit confirmation.")
        self._require_task_id(task_id, "Completing a task")
        if not project_id:
            raise AmbiguousOperationError("Completing a task requires `project_id`.")
        client = self._with_client()
        try:
            result = client.complete_task(project_id, task_id)
            return {"task_id": task_id, "project_id": project_id, "result": result}
        finally:
            client.close()

    def get_task(self, task_id: str, project_id: str) -> dict[str, Any]:
        self._require_task_id(task_id, "Getting a task")
        if not project_id:
            raise AmbiguousOperationError("Getting a task requires `project_id`.")
        client = self._with_client()
        try:
            return Task.from_api(client.get_task(project_id, task_id), project_id=project_id).to_dict()
        finally:
            client.close()

    def update_task(
        self,
        task_id: str,
        project_id: str,
        title: str | None = None,
        content: str | None = None,
        due: str | None = None,
        priority: str | None = None,
    ) -> dict[str, Any]:
        self._require_task_id(task_id, "Updating a task")
        if not project_id:
            raise AmbiguousOperationError("Updating a task requires `project_id`.")
        normalized_due = normalize_task_date(due) if due is not None else None
        normalized_priority = normalize_priority(priority) if priority is not None else None
        payload: dict[str, Any] = {"id": task_id, "projectId": project_id}
        if title is not None:
            if not title.strip():
                raise ValidationError("Task title cannot be empty.")
            payload["title"] = title
        if content is not None:
            payload["content"] = content
        if normalized_due is not None:
            payload["dueDate"] = normalized_due
        if normalized_priority is not None:
            payload["priority"] = PRIORITY_MAP[normalized_priority]
        if len(payload) == 2:
            raise ValidationError("Updating a task requires at least one changed field.")

        client = self._with_client()
        try:
            return Task.from_api(client.update_task(task_id, payload), project_id=project_id).to_dict()
        finally:
            client.close()

    def set_task_reminders(self, task_id: str, project_id: str, reminders: list[str]) -> dict[str, Any]:
        self._require_task_id(task_id, "Setting task reminders")
        if not project_id:
            raise AmbiguousOperationError("Setting task reminders requires `project_id`.")
        normalized = [reminder.strip() for reminder in reminders if reminder and reminder.strip()]
        if not normalized:
            raise ValidationError("Setting task reminders requires at least one reminder value.")
        return self._update_existing_task_fields(task_id, project_id, {"reminders": normalized})

    def clear_task_reminders(self, task_id: str, project_id: str) -> dict[str, Any]:
        self._require_task_id(task_id, "Clearing task reminders")
        if not project_id:
            raise AmbiguousOperationError("Clearing task reminders requires `project_id`.")
        return self._update_existing_task_fields(task_id, project_id, {"reminders": []})

    def set_task_repeat(
        self,
        task_id: str,
        project_id: str,
        preset: str | None = None,
        rrule: str | None = None,
    ) -> dict[str, Any]:
        self._require_task_id(task_id, "Setting task repeat")
        if not project_id:
            raise AmbiguousOperationError("Setting task repeat requires `project_id`.")
        if bool(preset) == bool(rrule):
            raise ValidationError("Setting task repeat requires exactly one of `preset` or `rrule`.")
        repeat_flag = rrule.strip() if rrule else REPEAT_PRESETS.get(str(preset).casefold())
        if not repeat_flag:
            raise ValidationError(
                f"Unknown repeat preset `{preset}`.",
                hint="Use one of: daily, weekly, monthly, yearly, or pass a raw RRULE.",
            )
        if not repeat_flag.startswith("RRULE:"):
            repeat_flag = f"RRULE:{repeat_flag}"
        return self._update_existing_task_fields(task_id, project_id, {"repeatFlag": repeat_flag})

    def clear_task_repeat(self, task_id: str, project_id: str) -> dict[str, Any]:
        self._require_task_id(task_id, "Clearing task repeat")
        if not project_id:
            raise AmbiguousOperationError("Clearing task repeat requires `project_id`.")
        return self._update_existing_task_fields(task_id, project_id, {"repeatFlag": ""})

    def _update_existing_task_fields(
        self,
        task_id: str,
        project_id: str,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        client = self._with_client()
        try:
            task = client.get_task(project_id, task_id)
            payload = dict(task)
            payload["id"] = task_id
            payload["projectId"] = project_id
            payload.update(changes)
            return Task.from_api(client.update_task(task_id, payload), project_id=project_id).to_dict()
        finally:
            client.close()

    def delete_task(self, task_id: str, project_id: str, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            raise ConfirmationRequiredError("Deleting a task requires explicit confirmation.")
        self._require_task_id(task_id, "Deleting a task")
        if not project_id:
            raise AmbiguousOperationError("Deleting a task requires `project_id`.")
        client = self._with_client()
        try:
            result = client.delete_task(project_id, task_id)
            return {"task_id": task_id, "project_id": project_id, "result": result}
        finally:
            client.close()

    def move_task(self, task_id: str, from_project_id: str, to_project_id: str) -> dict[str, Any]:
        self._require_task_id(task_id, "Moving a task")
        if not from_project_id or not to_project_id:
            raise AmbiguousOperationError("Moving a task requires source and destination project IDs.")
        if from_project_id == to_project_id:
            raise ValidationError("Moving a task source and destination projects must differ.")
        client = self._with_client()
        try:
            result = client.move_task(task_id, from_project_id, to_project_id)
            return {
                "task_id": task_id,
                "from_project_id": from_project_id,
                "to_project_id": to_project_id,
                "result": result,
            }
        finally:
            client.close()


    def list_habits(self) -> list[dict[str, Any]]:
        client = self._with_client()
        try:
            return [Habit.from_api(item).to_dict() for item in self._extract_list(client.list_habits())]
        finally:
            client.close()

    def get_habit(self, habit_id: str) -> dict[str, Any]:
        if not habit_id:
            raise AmbiguousOperationError("Getting a habit requires `habit_id`.")
        client = self._with_client()
        try:
            return Habit.from_api(client.get_habit(habit_id)).to_dict()
        finally:
            client.close()

    def create_habit(
        self,
        name: str,
        goal: int | None = None,
        step: int | None = None,
        unit: str | None = None,
        repeat_rule: str | None = None,
    ) -> dict[str, Any]:
        if not name.strip():
            raise ValidationError("Creating a habit requires a non-empty name.")
        payload: dict[str, Any] = {"name": name}
        if goal is not None:
            payload["goal"] = goal
        if step is not None:
            payload["step"] = step
        if unit is not None:
            payload["unit"] = unit
        if repeat_rule is not None:
            payload["repeatRule"] = repeat_rule
        client = self._with_client()
        try:
            return Habit.from_api(client.create_habit(payload)).to_dict()
        finally:
            client.close()

    def update_habit(
        self,
        habit_id: str,
        name: str | None = None,
        goal: int | None = None,
        step: int | None = None,
        unit: str | None = None,
        repeat_rule: str | None = None,
    ) -> dict[str, Any]:
        if not habit_id:
            raise AmbiguousOperationError("Updating a habit requires `habit_id`.")
        payload: dict[str, Any] = {"id": habit_id}
        if name is not None:
            if not name.strip():
                raise ValidationError("Habit name cannot be empty.")
            payload["name"] = name
        if goal is not None:
            payload["goal"] = goal
        if step is not None:
            payload["step"] = step
        if unit is not None:
            payload["unit"] = unit
        if repeat_rule is not None:
            payload["repeatRule"] = repeat_rule
        if len(payload) == 1:
            raise ValidationError("Updating a habit requires at least one changed field.")
        client = self._with_client()
        try:
            return Habit.from_api(client.update_habit(habit_id, payload)).to_dict()
        finally:
            client.close()

    def checkin_habit(self, habit_id: str, stamp: int, value: int = 1) -> dict[str, Any]:
        if not habit_id:
            raise AmbiguousOperationError("Checking in a habit requires `habit_id`.")
        payload = {"stamp": int(stamp), "value": value}
        client = self._with_client()
        try:
            return client.checkin_habit(habit_id, payload)
        finally:
            client.close()

    def habit_checkins(self, habit_ids: list[str], from_stamp: int, to_stamp: int) -> list[dict[str, Any]]:
        if not habit_ids:
            raise ValidationError("Habit check-in history requires at least one habit ID.")
        client = self._with_client()
        try:
            return self._extract_list(client.habit_checkins(habit_ids, int(from_stamp), int(to_stamp)))
        finally:
            client.close()

    def get_focus(self, focus_id: str, focus_type: int = 0) -> dict[str, Any]:
        if not focus_id:
            raise AmbiguousOperationError("Getting a focus session requires `focus_id`.")
        client = self._with_client()
        try:
            return Focus.from_api(client.get_focus(focus_id, focus_type)).to_dict()
        finally:
            client.close()

    def list_focuses(self, from_time: str, to_time: str, focus_type: int = 0) -> list[dict[str, Any]]:
        self._validate_focus_range(from_time, to_time)
        client = self._with_client()
        try:
            return [Focus.from_api(item).to_dict() for item in self._extract_list(client.list_focuses(from_time, to_time, focus_type))]
        finally:
            client.close()

    def delete_focus(self, focus_id: str, focus_type: int = 0, confirmed: bool = False) -> dict[str, Any]:
        if not confirmed:
            raise ConfirmationRequiredError("Deleting a focus session requires explicit confirmation.")
        if not focus_id:
            raise AmbiguousOperationError("Deleting a focus session requires `focus_id`.")
        client = self._with_client()
        try:
            result = client.delete_focus(focus_id, focus_type)
            return {"focus_id": focus_id, "focus_type": focus_type, "result": result}
        finally:
            client.close()

    @staticmethod
    def _validate_focus_range(from_time: str, to_time: str) -> None:
        try:
            start = date.fromisoformat(from_time[:10])
            end = date.fromisoformat(to_time[:10])
        except ValueError as exc:
            raise ValidationError(
                "Invalid focus date range.",
                hint="Use ISO dates or datetimes beginning with YYYY-MM-DD.",
            ) from exc
        if start > end:
            raise ValidationError("--from must be on or before --to.")
        if (end - start).days > 30:
            raise ValidationError("Focus queries are limited to a maximum 30-day range.")

    @staticmethod
    def _extract_list(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("habits", "focuses", "checkins", "data", "items"):
                if isinstance(data.get(key), list):
                    return [item for item in data[key] if isinstance(item, dict)]
            return [data]
        return []

    @staticmethod
    def _require_task_id(task_id: str, action: str) -> None:
        if not task_id or not task_id.strip():
            raise AmbiguousOperationError(f"{action} requires `task_id`.")

    def export_tasks(
        self,
        output_format: str,
        project: str | None = None,
        status: str = "open",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> str:
        tasks = self._task_export_rows(project=project, status=status, start_date=start_date, end_date=end_date)
        return serialize_tasks(tasks, output_format)

    def backup_tasks(
        self,
        output_dir: str | Path,
        output_formats: list[str],
        backup_date: str | None = None,
        project: str | None = None,
        status: str = "all",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        formats = [item.casefold().strip() for item in output_formats if item and item.strip()]
        if not formats:
            raise ValidationError("Backing up tasks requires at least one output format.")
        if len(set(formats)) != len(formats):
            raise ValidationError("Backup output formats must be unique.")
        tasks = self._task_export_rows(project=project, status=status, start_date=start_date, end_date=end_date)
        backup_day = backup_date or date.today().isoformat()
        # Validate eagerly so backup paths are deterministic and sortable.
        date.fromisoformat(backup_day)
        root = Path(output_dir).expanduser()
        scope_slug = self._backup_slug(project or "all-projects")
        backup_dir = root / backup_day / scope_slug
        backup_dir.mkdir(parents=True, exist_ok=True)

        files: list[dict[str, Any]] = []
        for output_format in formats:
            extension = "md" if output_format == "markdown" else output_format
            filename = f"tasks.{extension}"
            path = backup_dir / filename
            content = serialize_tasks(tasks, output_format)
            if output_format == "markdown":
                title = f"# Ticktask backup — {backup_day}\n\n"
                details = [
                    f"- Project: {project or 'all'}",
                    f"- Status: {status}",
                    f"- Count: {len(tasks)}",
                ]
                if start_date:
                    details.append(f"- From: {start_date}")
                if end_date:
                    details.append(f"- To: {end_date}")
                content = title + "\n".join(details) + "\n\n" + content
            path.write_text(content, encoding="utf-8")
            files.append(
                {
                    "format": output_format,
                    "path": path.relative_to(root).as_posix(),
                    "bytes": path.stat().st_size,
                }
            )

        manifest = {
            "version": 1,
            "backup_date": backup_day,
            "project": project,
            "status": status,
            "start_date": start_date,
            "end_date": end_date,
            "count": len(tasks),
            "output_dir": str(root),
            "files": files,
        }
        manifest_path = root / backup_day / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        result = dict(manifest)
        result["manifest_path"] = manifest_path.relative_to(root).as_posix()
        return result

    def _task_export_rows(
        self,
        project: str | None = None,
        status: str = "open",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        if status == "completed":
            return self.completed_tasks(start_date=start_date, end_date=end_date, project=project)
        if status == "all":
            open_tasks = self.list_tasks(project=project, status="open")
            completed_tasks = self.completed_tasks(
                start_date=start_date,
                end_date=end_date,
                project=project,
            )
            tasks_by_key = {
                (task.get("project_id"), task.get("id")): task
                for task in [*open_tasks, *completed_tasks]
            }
            return list(tasks_by_key.values())
        if status != "open":
            raise NotFoundError(f"Unknown task status filter `{status}`.")
        return self.list_tasks(
            project=project,
            status=status,
            start_date=start_date,
            end_date=end_date,
        )

    @staticmethod
    def _backup_slug(value: str) -> str:
        slug = re.sub(r"[^\w]+", "-", value.casefold()).strip("-_")
        return slug or "all-projects"

    def sync_state(self) -> dict[str, Any]:
        return SyncStateStore().as_dict()

    def mark_sync_state(self, state_key: str, timestamp: str | None = None) -> dict[str, Any]:
        return SyncStateStore().mark(state_key, timestamp or utc_now())

    def sync_export_tasks(
        self,
        output_format: str,
        state_key: str,
        project: str | None = None,
        status: str = "all",
        since: str | None = None,
        save_state: bool = False,
    ) -> dict[str, Any]:
        store = SyncStateStore()
        stored = store.get(state_key)
        resolved_since = since or (stored or {}).get("last_synced_at")
        start_date = resolved_since[:10] if resolved_since else None
        content = self.export_tasks(
            output_format=output_format,
            project=project,
            status=status,
            start_date=start_date,
        )
        state = store.mark(state_key, utc_now()) if save_state else None
        return {
            "state_key": state_key,
            "since": resolved_since,
            "state_path": str(store.path),
            "content": content,
            "state": state,
        }

    def export_focuses(self, output_format: str, from_time: str, to_time: str, focus_type: int = 0) -> str:
        focuses = self.list_focuses(from_time=from_time, to_time=to_time, focus_type=focus_type)
        return serialize_focuses(focuses, output_format)

    @staticmethod
    def _extract_tasks(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            return []
        tasks: list[dict[str, Any]] = []
        for key in ("tasks", "task", "items"):
            values = data.get(key)
            if isinstance(values, list):
                tasks.extend(item for item in values if isinstance(item, dict))
        columns = data.get("columns")
        if isinstance(columns, list):
            for column in columns:
                if isinstance(column, dict) and isinstance(column.get("tasks"), list):
                    tasks.extend(item for item in column["tasks"] if isinstance(item, dict))
        return tasks

    @staticmethod
    def _select_projects(projects: list[Project], value: str | None) -> list[Project]:
        if not value:
            return projects
        return [TicktaskService._select_one_project(projects, value)]

    @staticmethod
    def _select_one_project(projects: list[Project], value: str) -> Project:
        exact = [item for item in projects if item.id == value or item.name == value]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise AmbiguousOperationError(f"Project `{value}` matches multiple projects.")
        fuzzy = [item for item in projects if value.casefold() in item.name.casefold()]
        if len(fuzzy) == 1:
            return fuzzy[0]
        if len(fuzzy) > 1:
            names = ", ".join(project.name for project in fuzzy)
            raise AmbiguousOperationError(
                f"Project `{value}` is ambiguous.",
                hint=f"Use a project ID. Matches: {names}.",
            )
        raise NotFoundError(f"Project `{value}` was not found.")
