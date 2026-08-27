"""Gateway `/project` command tests.

The command binds a first-class, profile-local Hermes Project to one messaging
session. It must never use the profile-global active project as a thread cwd.
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import (
    SessionEntry,
    SessionSource,
    SessionStore,
    build_session_key,
)
from hermes_cli import projects_db as pdb


@dataclass
class _Store:
    entries: dict[str, SessionEntry] = field(default_factory=dict)

    def get_or_create_session(self, source, **_kwargs):
        key = build_session_key(source, profile=source.profile)
        entry = self.entries.get(key)
        if entry is None:
            now = datetime.now()
            entry = SessionEntry(
                session_key=key,
                session_id=f"session-{len(self.entries) + 1}",
                created_at=now,
                updated_at=now,
                origin=source,
                platform=source.platform,
                chat_type=source.chat_type,
            )
            self.entries[key] = entry
        return entry

    def set_session_metadata(self, session_key, key, value):
        entry = self.entries.get(session_key)
        if entry is None:
            return False
        entry.metadata[key] = value
        return True


def _source(thread_id: str, *, profile: str = "work") -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id=thread_id,
        chat_type="thread",
        thread_id=thread_id,
        user_id="fred",
        user_name="Fred",
        profile=profile,
    )


def _event(text: str, thread_id: str = "thread-1", *, profile: str = "work"):
    return MessageEvent(
        text=text,
        message_type=MessageType.COMMAND,
        source=_source(thread_id, profile=profile),
        message_id="message-1",
    )


def _runner(profile_homes: dict[str, Path]):
    runner = object.__new__(GatewayRunner)
    runner.session_store = _Store()
    runner._resolve_profile_home_for_source = lambda source: profile_homes[source.profile]
    runner._evict_cached_agent = MagicMock()
    return runner


def _create_project(home: Path, name: str, path: Path, *, slug: str | None = None):
    path.mkdir(parents=True, exist_ok=True)
    with pdb.connect_closing(home / "projects.db") as conn:
        project_id = pdb.create_project(
            conn,
            name=name,
            slug=slug,
            folders=[str(path)],
            primary_path=str(path),
        )
        return pdb.get_project(conn, project_id)


@pytest.mark.asyncio
async def test_project_command_lists_profile_projects(tmp_path):
    work_home = tmp_path / "profiles" / "work"
    work_home.mkdir(parents=True)
    _create_project(work_home, "Vault Migrator", tmp_path / "repos" / "vault", slug="vault-migrator")
    _create_project(work_home, "Medusa API", tmp_path / "repos" / "medusa", slug="medusa-api")
    runner = _runner({"work": work_home})

    result = await runner._handle_project_command(_event("/project list"))

    assert "Vault Migrator" in result
    assert "`vault-migrator`" in result
    assert "Medusa API" in result


@pytest.mark.asyncio
async def test_project_use_persists_thread_binding_and_evicts_only_that_agent(tmp_path):
    work_home = tmp_path / "profiles" / "work"
    work_home.mkdir(parents=True)
    repo = tmp_path / "repos" / "vault"
    project = _create_project(work_home, "Vault Migrator", repo, slug="vault-migrator")
    runner = _runner({"work": work_home})
    source = _source("thread-1")
    expected_key = build_session_key(source, profile="work")

    result = await runner._handle_project_command(_event("/project use vault-migrator"))

    binding = runner.session_store.entries[expected_key].metadata["project_binding"]
    assert binding == {
        "id": project.id,
        "slug": "vault-migrator",
        "name": "Vault Migrator",
        "cwd": str(repo.resolve()),
    }
    runner._evict_cached_agent.assert_called_once_with(expected_key)
    assert "Project bound" in result
    assert "Vault Migrator" in result
    assert str(repo.resolve()) in result


@pytest.mark.asyncio
async def test_project_binding_drives_terminal_task_cwd_and_clear(tmp_path):
    from tools.terminal_tool import clear_task_env_overrides, get_session_cwd

    work_home = tmp_path / "profiles" / "work"
    work_home.mkdir(parents=True)
    repo = tmp_path / "repos" / "vault"
    _create_project(work_home, "Vault Migrator", repo, slug="vault-migrator")
    runner = _runner({"work": work_home})

    try:
        await runner._handle_project_command(_event("/project use vault-migrator"))
        entry = next(iter(runner.session_store.entries.values()))

        assert get_session_cwd(entry.session_id) == str(repo.resolve())

        await runner._handle_project_command(_event("/project clear"))
        assert get_session_cwd(entry.session_id) is None
    finally:
        for entry in runner.session_store.entries.values():
            clear_task_env_overrides(entry.session_id)


@pytest.mark.asyncio
async def test_project_use_fails_closed_when_primary_folder_disappeared(tmp_path):
    work_home = tmp_path / "profiles" / "work"
    work_home.mkdir(parents=True)
    missing = tmp_path / "repos" / "missing"
    project = _create_project(work_home, "Missing", missing, slug="missing")
    missing.rmdir()
    runner = _runner({"work": work_home})

    result = await runner._handle_project_command(_event(f"/project use {project.slug}"))

    assert "does not exist" in result
    assert all(
        "project_binding" not in entry.metadata
        for entry in runner.session_store.entries.values()
    )
    runner._evict_cached_agent.assert_not_called()


@pytest.mark.asyncio
async def test_project_status_and_clear_are_thread_scoped(tmp_path):
    work_home = tmp_path / "profiles" / "work"
    work_home.mkdir(parents=True)
    _create_project(work_home, "Vault Migrator", tmp_path / "repos" / "vault", slug="vault-migrator")
    _create_project(work_home, "Medusa API", tmp_path / "repos" / "medusa", slug="medusa-api")
    runner = _runner({"work": work_home})

    await runner._handle_project_command(_event("/project use vault-migrator", "thread-1"))
    await runner._handle_project_command(_event("/project use medusa-api", "thread-2"))

    first = await runner._handle_project_command(_event("/project status", "thread-1"))
    second = await runner._handle_project_command(_event("/project status", "thread-2"))
    cleared = await runner._handle_project_command(_event("/project clear", "thread-1"))
    first_after = await runner._handle_project_command(_event("/project status", "thread-1"))
    second_after = await runner._handle_project_command(_event("/project status", "thread-2"))

    assert "Vault Migrator" in first
    assert "Medusa API" in second
    assert "cleared" in cleared.lower()
    assert "not bound" in first_after.lower()
    assert "Medusa API" in second_after


@pytest.mark.asyncio
async def test_project_resolution_uses_the_routed_profiles_database(tmp_path):
    work_home = tmp_path / "profiles" / "work"
    personal_home = tmp_path / "profiles" / "personal"
    work_home.mkdir(parents=True)
    personal_home.mkdir(parents=True)
    _create_project(work_home, "Work Vault", tmp_path / "work" / "vault", slug="vault")
    _create_project(personal_home, "Personal Vault", tmp_path / "personal" / "vault", slug="vault")
    runner = _runner({"work": work_home, "personal": personal_home})

    work_result = await runner._handle_project_command(
        _event("/project use vault", "work-thread", profile="work")
    )
    personal_result = await runner._handle_project_command(
        _event("/project use vault", "personal-thread", profile="personal")
    )

    assert "Work Vault" in work_result
    assert "Personal Vault" in personal_result
    work_key = build_session_key(_source("work-thread", profile="work"), profile="work")
    personal_key = build_session_key(
        _source("personal-thread", profile="personal"), profile="personal"
    )
    assert runner.session_store.entries[work_key].metadata["project_binding"]["name"] == "Work Vault"
    assert runner.session_store.entries[personal_key].metadata["project_binding"]["name"] == "Personal Vault"


def test_project_is_registered_as_a_gateway_command():
    from hermes_cli.commands import resolve_command

    command = resolve_command("project")

    assert command is not None
    assert command.gateway_only is True
    assert set(command.subcommands) >= {"list", "status", "use", "clear", "start"}


def test_project_binding_survives_explicit_and_automatic_session_rotation(tmp_path):
    source = _source("thread-1")
    store = SessionStore(tmp_path / "sessions", GatewayConfig(platforms={}))
    try:
        first = store.get_or_create_session(source)
        binding = {
            "id": "p_1234",
            "slug": "vault-migrator",
            "name": "Vault Migrator",
            "cwd": str(tmp_path / "vault"),
        }
        assert store.set_session_metadata(first.session_key, "project_binding", binding)

        explicit = store.reset_session(first.session_key)
        assert explicit is not None
        assert explicit.metadata["project_binding"] == binding

        automatic = store.get_or_create_session(source, force_new=True)
        assert automatic.metadata["project_binding"] == binding
    finally:
        store.close_all_db_handles()
