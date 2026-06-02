from __future__ import annotations

from pathlib import Path

import typer

from ticktask import __version__
from ticktask.cli import auth as auth_commands
from ticktask.cli import backup as backup_commands
from ticktask.cli import export as export_commands
from ticktask.cli import focus as focus_commands
from ticktask.cli import habit as habit_commands
from ticktask.cli import integration as integration_commands
from ticktask.cli import project as project_commands
from ticktask.cli import report as report_commands
from ticktask.cli import sync as sync_commands
from ticktask.cli import task as task_commands
from ticktask.cli.formatters import emit_error, emit_json, print_tasks
from ticktask.core.auth import AuthManager
from ticktask.core.config import ConfigStore
from ticktask.core.diagnostics import create_diagnostic_bundle
from ticktask.core.results import ok
from ticktask.core.services import TicktaskService

app = typer.Typer(help="Agent-friendly CLI for TickTick and Dida365.")
app.add_typer(auth_commands.app, name="auth")
app.add_typer(project_commands.app, name="project")
app.add_typer(task_commands.app, name="task")
app.add_typer(habit_commands.app, name="habit")
app.add_typer(focus_commands.app, name="focus")
app.add_typer(export_commands.app, name="export")
app.add_typer(backup_commands.app, name="backup")
app.add_typer(report_commands.app, name="report")
app.add_typer(sync_commands.app, name="sync")
app.add_typer(integration_commands.app, name="integration")
doctor_app = typer.Typer(help="Doctor and diagnostic commands.", invoke_without_command=True)
app.add_typer(doctor_app, name="doctor")


def version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=version_callback,
        help="Show version and exit.",
    ),
) -> None:
    return None


@doctor_app.callback()
def doctor(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Emit stable JSON."),
) -> None:
    if ctx.invoked_subcommand is not None:
        return None
    try:
        store = ConfigStore()
        status = AuthManager(store).status()
        data = {
            "version": __version__,
            "config_path": store.path_string(),
            "service": status.service,
            "base_url": status.base_url,
            "configured": status.configured,
            "authenticated": status.authenticated,
        }
        if json_output:
            emit_json(ok(data))
        else:
            typer.echo(f"ticktask {__version__}")
            typer.echo(f"config: {store.path_string()}")
            typer.echo(f"service: {status.service} ({status.base_url})")
            typer.echo(f"configured: {status.configured}")
            typer.echo(f"authenticated: {status.authenticated}")
    except Exception as exc:
        emit_error(exc, json_output)


@doctor_app.command("bundle")
def doctor_bundle(
    output_path: Path = typer.Option(
        Path("ticktask-diagnostics.zip"),
        "--output",
        help="Path for the redacted diagnostic ZIP bundle.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit stable JSON."),
) -> None:
    try:
        result = create_diagnostic_bundle(output_path=output_path)
        if json_output:
            emit_json(ok(result))
        else:
            typer.echo(f"redacted diagnostic bundle: {result['bundle_path']}")
            typer.echo("files: " + ", ".join(item["path"] for item in result["files"]))
    except Exception as exc:
        emit_error(exc, json_output)


config_app = typer.Typer(help="Config commands.")


@config_app.command("path")
def config_path() -> None:
    typer.echo(ConfigStore().path_string())


app.add_typer(config_app, name="config")


@app.command("today")
def today(
    json_output: bool = typer.Option(False, "--json", help="Emit stable JSON."),
) -> None:
    try:
        tasks = TicktaskService().list_tasks(status="open", today_only=True)
        if json_output:
            emit_json(ok(tasks, {"count": len(tasks)}))
        else:
            print_tasks(tasks)
    except Exception as exc:
        emit_error(exc, json_output)


@app.command("completed")
def completed(
    period: str | None = typer.Argument(None, help="today, yesterday, or week."),
    from_date: str | None = typer.Option(None, "--from", help="Start date, YYYY-MM-DD."),
    to_date: str | None = typer.Option(None, "--to", help="End date, YYYY-MM-DD."),
    project: str | None = typer.Option(None, "--project", help="Project name or ID."),
    json_output: bool = typer.Option(False, "--json", help="Emit stable JSON."),
) -> None:
    try:
        tasks = TicktaskService().completed_tasks(
            preset=period,
            start_date=from_date,
            end_date=to_date,
            project=project,
        )
        if json_output:
            emit_json(ok(tasks, {"count": len(tasks)}))
        else:
            print_tasks(tasks)
    except Exception as exc:
        emit_error(exc, json_output)


@app.command("add")
def add_alias(
    title: str,
    project: str | None = typer.Option(None, "--project", help="Project name or ID."),
    content: str | None = typer.Option(None, "--content", help="Task body/content."),
    due: str | None = typer.Option(None, "--due", help="Due date string accepted by the API."),
    priority: str = typer.Option("none", "--priority", help="none, low, medium, or high."),
    idempotency_key: str | None = typer.Option(
        None,
        "--idempotency-key",
        help="Agent-supplied key to safely replay task creation without duplicate API writes.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit stable JSON."),
) -> None:
    task_commands.add_task(
        title=title,
        project=project,
        content=content,
        due=due,
        priority=priority,
        idempotency_key=idempotency_key,
        json_output=json_output,
    )


@app.command("done")
def done_alias(
    task_id: str,
    project_id: str = typer.Option(..., "--project-id", help="Exact project ID."),
    yes: bool = typer.Option(False, "--yes", help="Confirm completion."),
    json_output: bool = typer.Option(False, "--json", help="Emit stable JSON."),
) -> None:
    task_commands.complete_task(task_id, project_id, yes, json_output)
