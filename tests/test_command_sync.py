import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from services.command_sync import command_schema, sync_commands_after_ready


def test_schema_ignores_discord_metadata_and_empty_defaults_but_keeps_constraints():
    local = {'name': 'vote', 'type': 1, 'description': 'Vote', 'nsfw': False,
             'dm_permission': False, 'default_member_permissions': 32,
             'options': [{'name': 'count', 'type': 4, 'description': 'Count', 'min_value': 0}]}
    remote = {**local, 'id': '1', 'application_id': '2', 'version': '3',
              'default_member_permissions': '32', 'name_localizations': {},
              'options': [{**local['options'][0], 'required': False, 'choices': [], 'max_value': None}]}
    assert command_schema([local]) == command_schema([remote])
    remote['options'][0]['min_value'] = 1
    assert command_schema([local]) != command_schema([remote])


def client_for(remote):
    payload = {'name': 'vote', 'type': 1, 'description': 'Vote', 'options': []}
    tree = SimpleNamespace(get_commands=Mock(return_value=[SimpleNamespace(to_dict=lambda _: payload)]),
                           sync=AsyncMock(return_value=[payload]))
    return SimpleNamespace(tree=tree, http=SimpleNamespace(get_guild_commands=AsyncMock(return_value=remote)),
                           application_id=123, wait_until_ready=AsyncMock(), is_closed=lambda: False)


def test_unchanged_commands_never_issue_bulk_registration():
    client = client_for([{'name': 'vote', 'type': 1, 'description': 'Vote'}])
    asyncio.run(sync_commands_after_ready(client, SimpleNamespace(id=456), Mock()))
    client.wait_until_ready.assert_awaited_once()
    client.tree.sync.assert_not_awaited()


def test_changed_commands_register_after_ready():
    client = client_for([])
    asyncio.run(sync_commands_after_ready(client, SimpleNamespace(id=456), Mock()))
    client.tree.sync.assert_awaited_once()


def test_pending_readiness_does_not_begin_registration_and_can_be_cancelled():
    async def scenario():
        client = client_for([])
        gate = asyncio.Event()
        client.wait_until_ready = gate.wait
        task = asyncio.create_task(sync_commands_after_ready(client, SimpleNamespace(id=456), Mock()))
        await asyncio.sleep(0)
        client.http.get_guild_commands.assert_not_awaited()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())
