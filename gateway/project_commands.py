"""Gateway-facing first-class Project commands.

The CLI/Desktop profile-global active project is navigation state, not a safe
messaging workspace. This module binds a project to one durable gateway session
entry instead, using its small JSON metadata record and the existing
session-scoped cwd ContextVar.
"""

from __future__ import annotations

import shlex
import asyncio
import logging
from pathlib import Path
from typing import Any

from hermes_cli import projects_db as pdb

PROJECT_BINDING_METADATA_KEY = "project_binding"
logger = logging.getLogger(__name__)


async def _project_workspace_to_canonical_stores(runner, entry, cwd: str) -> None:
    """Project the binding into Hermes' existing tool and SessionDB cwd stores."""

    def _sync() -> None:
        from tools.terminal_tool import (
            clear_task_env_overrides,
            register_task_env_overrides,
        )

        if cwd:
            register_task_env_overrides(entry.session_id, {"cwd": cwd})
        else:
            clear_task_env_overrides(entry.session_id)

        # SessionEntry metadata remains the routing-index authority for the
        # gateway command. The canonical state.db cwd projection makes Desktop
        # grouping and every existing workspace consumer agree with it.
        try:
            db = runner.session_store._db
            if db is not None:
                db.update_session_cwd(
                    entry.session_id,
                    cwd,
                    replace_git_meta=True,
                )
        except Exception:
            logger.warning(
                "Could not project project workspace into SessionDB for %s",
                entry.session_id,
                exc_info=True,
            )

    await asyncio.to_thread(_sync)


def _primary_path(project) -> str:
    if project.primary_path:
        return str(project.primary_path)
    for folder in project.folders:
        if folder.is_primary:
            return str(folder.path)
    return str(project.folders[0].path) if project.folders else ""


def _resolve_project(conn, token: str):
    """Resolve a project by id, slug, or human name without fuzzy guessing."""
    candidate = (token or "").strip()
    if not candidate:
        return None
    projects = pdb.list_projects(conn, include_archived=False)
    for project in projects:
        if candidate in (project.id, project.slug) or candidate == project.name:
            return project
    folded = candidate.casefold()
    for project in projects:
        if folded in (project.slug.casefold(), project.name.casefold()):
            return project
    return None


def _binding_for(project, cwd: Path) -> dict[str, str]:
    return {
        "id": str(project.id),
        "slug": str(project.slug),
        "name": str(project.name),
        "cwd": str(cwd),
    }


def _format_binding(binding: dict[str, Any] | None) -> str:
    if not isinstance(binding, dict) or not binding.get("cwd"):
        return "This thread is not bound to a Hermes Project. Use `/project list` or `/project use <project>`."
    return (
        f"**Project:** {binding.get('name') or binding.get('slug')} "
        f"(`{binding.get('slug')}`)\n"
        f"**Workspace:** `{binding.get('cwd')}`"
    )


def _format_project_list(projects) -> str:
    if not projects:
        return (
            "No Projects are registered for this Hermes profile. Create one with "
            "`hermes project create <name> <folder> --primary <folder>`."
        )
    lines = ["**Projects available in this profile:**"]
    for project in projects:
        primary = _primary_path(project)
        suffix = f" — `{primary}`" if primary else " — no primary folder"
        lines.append(f"• **{project.name}** (`{project.slug}`){suffix}")
    lines.append("Use `/project use <slug>` to bind this thread.")
    return "\n".join(lines)


async def handle_project_command(runner, event) -> str:
    """Execute `/project list|status|use|clear` for one gateway session."""
    try:
        argv = shlex.split(event.get_command_args() or "")
    except ValueError as exc:
        return f"Invalid `/project` arguments: {exc}"

    action = argv[0].casefold() if argv else "status"
    profile_home = runner._resolve_profile_home_for_source(event.source)
    db_path = Path(profile_home) / "projects.db"

    if action in {"list", "ls"}:
        with pdb.connect_closing(db_path) as conn:
            return _format_project_list(pdb.list_projects(conn, include_archived=False))

    if action == "start":
        return (
            "Use Discord's native `/project` command with action **start** from a "
            "server channel to create a project-bound thread."
        )

    if action not in {"status", "use", "clear"}:
        return "Usage: `/project [list|status|use <project>|clear|start]`"

    entry = await runner.async_session_store.get_or_create_session(
        event.source, touch_activity=False
    )

    if action == "status":
        binding = entry.metadata.get(PROJECT_BINDING_METADATA_KEY)
        return _format_binding(binding)

    if action == "clear":
        current = entry.metadata.get(PROJECT_BINDING_METADATA_KEY)
        changed = current is not None
        if not await runner.async_session_store.set_session_metadata(
            entry.session_key, PROJECT_BINDING_METADATA_KEY, None
        ):
            return "Could not persist the project change; this thread was left unchanged."
        await _project_workspace_to_canonical_stores(runner, entry, "")
        if changed:
            runner._evict_cached_agent(entry.session_key)
        return "Project binding cleared. This thread now uses the gateway's default workspace."

    project_token = " ".join(argv[1:]).strip()
    if not project_token:
        return "Usage: `/project use <project-id|slug|name>`"

    with pdb.connect_closing(db_path) as conn:
        project = _resolve_project(conn, project_token)
    if project is None:
        return f"No Project matching `{project_token}` exists in this Hermes profile. Use `/project list`."

    raw_primary = _primary_path(project)
    if not raw_primary:
        return f"Project **{project.name}** has no primary folder; add one before binding it."
    primary = Path(raw_primary).expanduser().resolve(strict=False)
    if not primary.is_dir():
        return (
            f"Project **{project.name}** cannot be bound because its primary folder "
            f"does not exist: `{primary}`"
        )

    binding = _binding_for(project, primary)
    previous = entry.metadata.get(PROJECT_BINDING_METADATA_KEY)
    if not await runner.async_session_store.set_session_metadata(
        entry.session_key, PROJECT_BINDING_METADATA_KEY, binding
    ):
        return "Could not persist the project change; this thread was left unchanged."
    await _project_workspace_to_canonical_stores(runner, entry, str(primary))
    if previous != binding:
        runner._evict_cached_agent(entry.session_key)

    return f"✅ **Project bound:** {project.name} (`{project.slug}`)\n**Workspace:** `{primary}`"
