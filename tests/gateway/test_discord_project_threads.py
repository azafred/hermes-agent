"""Discord project picker and project-bound thread creation tests."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from hermes_cli import projects_db as pdb


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.Interaction = object
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        autocomplete=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
        Group=MagicMock,
        Command=MagicMock,
    )
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules["discord"] = discord_mod
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


class FakeTree:
    def __init__(self):
        self.commands = {}

    def command(self, *, name, description):
        del description

        def decorator(fn):
            self.commands[name] = fn
            return fn

        return decorator

    def add_command(self, cmd):
        self.commands[cmd.name] = cmd

    def get_commands(self):
        return [SimpleNamespace(name=name) for name in self.commands]


_ensure_discord_mock()

from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


@pytest.fixture
def adapter():
    instance = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    instance._client = SimpleNamespace(
        tree=FakeTree(),
        get_channel=lambda _id: None,
        fetch_channel=AsyncMock(),
        user=SimpleNamespace(id=99999, name="HermesBot"),
    )
    instance._check_slash_authorization = AsyncMock(return_value=True)
    instance._evaluate_slash_authorization = MagicMock(return_value=(True, None))
    instance._threads.mark = lambda _thread_id: None
    return instance


def _interaction():
    channel = SimpleNamespace(
        id=100,
        name="hermes-work",
        parent_id=None,
        guild=SimpleNamespace(name="Techunix"),
    )
    return SimpleNamespace(
        channel=channel,
        channel_id=100,
        guild_id=200,
        guild=channel.guild,
        user=SimpleNamespace(id=42, name="Fred", display_name="Fred"),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_native_project_slash_source_preserves_guild_profile_routing(adapter):
    seen = []

    def _route(source):
        seen.append(source)
        return "work"

    adapter.gateway_runner = SimpleNamespace(_profile_name_for_source=_route)

    event = adapter._build_slash_event(_interaction(), "/project list")

    assert event.source.profile == "work"
    assert event.source.scope_id == "200"
    assert event.source.guild_id == "200"
    assert seen[0].scope_id == "200"


@pytest.mark.asyncio
async def test_project_autocomplete_fails_closed_before_catalog_lookup(adapter):
    adapter._evaluate_slash_authorization = MagicMock(
        return_value=(False, "not allowed")
    )
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=MagicMock(
            side_effect=AssertionError("unauthorized autocomplete reached project DB")
        )
    )

    choices = await adapter._project_autocomplete(_interaction(), "vault")

    assert choices == []


@pytest.mark.asyncio
async def test_project_start_checks_central_slash_access_before_thread_creation(adapter):
    interaction = _interaction()
    adapter.gateway_runner = SimpleNamespace(
        _check_slash_access=lambda _source, _command: "You are not allowed to run `/project`."
    )
    adapter._create_thread = AsyncMock()

    await adapter._handle_project_start_slash(
        interaction,
        project="vault-migrator",
        title="Denied",
        message="Do not run",
    )

    adapter._create_thread.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()
    assert "not allowed" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_project_start_binds_before_dispatching_starter(adapter):
    interaction = _interaction()
    adapter._create_thread = AsyncMock(
        return_value={"success": True, "thread_id": "300", "thread_name": "ACL investigation"}
    )
    order = []

    async def _bind(event):
        order.append(
            (
                "bind",
                event.text,
                event.source.thread_id,
                event.source.parent_chat_id,
                event.source.scope_id,
            )
        )
        return "✅ **Project bound:** Vault Migrator (`vault-migrator`)\n**Workspace:** `/repos/vault`"

    async def _starter(_interaction, thread_id, thread_name, text):
        order.append(("starter", text, thread_id, thread_name))

    adapter._message_handler = AsyncMock(side_effect=_bind)
    adapter._dispatch_thread_session = AsyncMock(side_effect=_starter)
    adapter.send = AsyncMock()

    await adapter._handle_project_start_slash(
        interaction,
        project="vault-migrator",
        title="ACL investigation",
        message="Inspect retry behavior",
    )

    assert order == [
        ("bind", "/project use vault-migrator", "300", "100", "200"),
        ("starter", "Inspect retry behavior", "300", "ACL investigation"),
    ]
    adapter.send.assert_awaited_once()
    assert "Project bound" in adapter.send.await_args.args[1]
    interaction.followup.send.assert_awaited_once()
    assert "<#300>" in interaction.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_project_start_does_not_run_starter_when_binding_fails(adapter):
    interaction = _interaction()
    adapter._create_thread = AsyncMock(
        return_value={"success": True, "thread_id": "300", "thread_name": "Missing"}
    )
    adapter._message_handler = AsyncMock(
        return_value="No Project matching `missing` exists in this Hermes profile."
    )
    adapter._dispatch_thread_session = AsyncMock()
    adapter.send = AsyncMock()

    await adapter._handle_project_start_slash(
        interaction,
        project="missing",
        title="Missing",
        message="Do not run",
    )

    adapter._dispatch_thread_session.assert_not_awaited()
    assert "could not be bound" in interaction.followup.send.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_project_autocomplete_reads_routed_profile_and_caps_choices(adapter, tmp_path):
    work_home = tmp_path / "profiles" / "work"
    work_home.mkdir(parents=True)
    with pdb.connect_closing(work_home / "projects.db") as conn:
        for index in range(30):
            repo = tmp_path / "repos" / f"vault-{index:02d}"
            repo.mkdir(parents=True)
            pdb.create_project(
                conn,
                name=f"Vault Project {index:02d}",
                slug=f"vault-{index:02d}",
                folders=[str(repo)],
                primary_path=str(repo),
            )

    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: work_home
    )
    interaction = _interaction()

    choices = await adapter._project_autocomplete(interaction, "vault")

    assert len(choices) == 25
    assert choices[0].value == "vault-00"
    assert choices[-1].value == "vault-24"
    assert all("Vault Project" in choice.name for choice in choices)
