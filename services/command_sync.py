"""Register changed slash commands after connecting, without delaying readiness."""

import asyncio


def command_schema(payloads):
    def clean(value):
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()
                    if item is not None and item is not False and item != [] and item != {}}
        return value

    # Guild commands ignore DM/install contexts. Ignore Discord-generated IDs,
    # versions and deprecated fields, but compare all option constraints.
    fields = {'name', 'description', 'type', 'options', 'nsfw',
              'default_member_permissions', 'name_localizations', 'description_localizations'}
    result = []
    for payload in payloads:
        command = {key: value for key, value in payload.items() if key in fields}
        if command.get('default_member_permissions') is not None:
            command['default_member_permissions'] = int(command['default_member_permissions'])
        result.append(clean(command))
    return sorted(result, key=lambda item: (item.get('type', 1), item['name']))


async def sync_commands_after_ready(client, guild, logger):
    await client.wait_until_ready()
    desired = command_schema([command.to_dict(client.tree)
                              for command in client.tree.get_commands(guild=guild)])
    while not client.is_closed():
        try:
            logger.info('Checking slash command registration for guild %s', guild.id)
            registered = await asyncio.wait_for(
                client.http.get_guild_commands(client.application_id, guild.id), timeout=30)
            if command_schema(registered) == desired:
                logger.info('Slash commands unchanged; skipped registration for guild %s', guild.id)
                return
            synced = await asyncio.wait_for(client.tree.sync(guild=guild), timeout=60)
            logger.info('Synced %s command(s) to guild %s', len(synced), guild.id)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Slash command registration delayed; bot remains online. Retrying in 60s')
            await asyncio.sleep(60)
