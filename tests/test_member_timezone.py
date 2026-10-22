import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from services import user_settings
from services.member_timezone import MemberTimezoneSelect
from services.server_setup import ServerSetupView
from services.timezone_search import CityResultSelect
from test_voting import context


def admin_interaction(target):
    return SimpleNamespace(
        guild_id=123, guild=SimpleNamespace(fetch_member=AsyncMock(return_value=target)), extras={},
        user=SimpleNamespace(id=7, guild_permissions=SimpleNamespace(manage_guild=True, administrator=False)),
        response=SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


@pytest.mark.parametrize('query', ['Dubai', 'dub'])
def test_admin_saves_member_timezone_silently_and_includes_it_in_match_times(context, query):
    target = SimpleNamespace(id=55, display_name='Friend', send=AsyncMock())
    user_settings.set_user_timezone(7, 'Europe/Berlin')
    before = user_settings.get_notification_settings(55)
    followed = user_settings.get_followed_team_ids(55)

    async def run():
        interaction = admin_interaction(target)
        panel = ServerSetupView(123)
        await panel.set_member_timezone.callback(interaction)
        call = interaction.response.send_message.call_args
        assert call.kwargs['ephemeral']
        assert call.kwargs['allowed_mentions'].to_dict() == {'parse': []}
        member_card = call.kwargs['view']
        selector = next(item for item in member_card.walk_children() if isinstance(item, MemberTimezoneSelect))
        selector._values = [target]
        await selector.callback(interaction)
        modal = interaction.response.send_modal.call_args.args[0]
        modal.city._value = query
        await modal.on_submit(interaction)
        if query == 'dub':
            results = interaction.response.edit_message.call_args.kwargs['view']
            city = next(item for item in results.walk_children() if isinstance(item, CityResultSelect))
            city._values = ['Asia/Dubai']
            await city.callback(interaction)
        assert user_settings.get_user_timezone(55) == 'Asia/Dubai'
        assert user_settings.get_user_timezone(7) == 'Europe/Berlin'
        assert 'Asia/Dubai' in user_settings.get_registered_timezones()
        assert user_settings.get_notification_settings(55) == before
        assert user_settings.get_followed_team_ids(55) == followed
        assert not user_settings.has_completed_setup(123, 55)
        target.send.assert_not_awaited()
        assert 'Friend' in str(interaction.edit_original_response.call_args.kwargs['view'].to_components())
        assert interaction.edit_original_response.call_args.kwargs['allowed_mentions'].to_dict() == {'parse': []}
        from services.match_times import registered_match_times
        assert 'Dubai' in registered_match_times(context[1].data.matches[0].start_time)
        member_card.stop()
        panel.stop()

    asyncio.run(run())


@pytest.mark.parametrize('denial', ['permissions', 'guild', 'member_left'])
def test_admin_permissions_and_membership_are_rechecked_before_save(context, denial):
    target = SimpleNamespace(id=55, display_name='Friend', send=AsyncMock())

    async def run():
        interaction = admin_interaction(target)
        selector = MemberTimezoneSelect(123, 7)
        selector._values = [target]
        await selector.callback(interaction)
        modal = interaction.response.send_modal.call_args.args[0]
        modal.city._value = 'Dubai'
        if denial == 'permissions':
            interaction.user.guild_permissions.manage_guild = False
        elif denial == 'guild':
            interaction.guild_id = 999
        else:
            interaction.guild.fetch_member.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='gone'), 'gone')
        await modal.on_submit(interaction)
        assert user_settings.get_user_timezone(55) is None
        interaction.edit_original_response.assert_not_awaited()
        assert interaction.response.send_message.call_args.kwargs['ephemeral']
        target.send.assert_not_awaited()

    asyncio.run(run())


def test_member_editor_is_private_to_the_initiating_admin(context):
    async def run():
        interaction = admin_interaction(SimpleNamespace(id=55, display_name='Friend'))
        interaction.user.id = 8
        selector = MemberTimezoneSelect(123, 7)
        await selector.callback(interaction)
        interaction.response.send_modal.assert_not_awaited()
        assert interaction.response.send_message.call_args.kwargs['ephemeral']

    asyncio.run(run())
