"""Private admin-only timezone editing, with no member notifications."""

import discord

from services.presentation import message_payload
from services.timezone_search import CitySearchModal, owner_check
from services.user_settings import get_user_timezone


class MemberTimezoneSelect(discord.ui.UserSelect):
    def __init__(self, guild_id, admin_id):
        super().__init__(placeholder='Choose a member to set their timezone…', min_values=1, max_values=1)
        self.guild_id, self.admin_id = guild_id, admin_id

    async def callback(self, interaction):
        from services.server_setup import can_manage_server, build_server_setup_content, ServerSetupView
        if not await owner_check(interaction, self.admin_id):
            return
        target = self.values[0]

        async def authorize(current):
            if not can_manage_server(current, self.guild_id):
                await current.response.send_message(**message_payload('You need the Manage Server permission.'),
                                                     ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
                return False
            try:
                await current.guild.fetch_member(target.id)
            except discord.HTTPException:
                await current.response.send_message(**message_payload("I couldn't verify that member is still in this server."),
                                                     ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
                return False
            return True

        if not await authorize(interaction):
            return

        def render_setup(current):
            name = discord.utils.escape_mentions(discord.utils.escape_markdown(target.display_name))
            zone = get_user_timezone(target.id) or 'Not set'
            text = (build_server_setup_content(self.guild_id)
                    + f'\n\n### 🌍 Member Timezone\n**{name}:** {zone}\n'
                    'Included in match-time lists. This member receives no notification.')
            payload = message_payload(text, controls=ServerSetupView(self.guild_id))
            payload['view'].expire_with(current)
            return payload

        modal = CitySearchModal(self.admin_id, render_setup, target_user_id=target.id, authorize=authorize)
        modal.previous_card = getattr(interaction, 'extras', {}).get('expiring_message_card')
        await interaction.response.send_modal(modal)


class MemberTimezoneView(discord.ui.View):
    setup_controls = True

    def __init__(self, guild_id, admin_id):
        super().__init__(timeout=600)
        self.add_item(MemberTimezoneSelect(guild_id, admin_id))
