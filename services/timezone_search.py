"""City search inside the personal setup card; no command arguments required."""

from datetime import UTC, datetime
from difflib import SequenceMatcher
import unicodedata
from zoneinfo import ZoneInfo, available_timezones

import discord

from services.presentation import message_payload
from services.timezones import TIMEZONE_REGIONS
from services.user_settings import set_user_timezone


def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text.casefold().replace('_', ' '))
                   if not unicodedata.combining(c)).strip()


def city_matches(query):
    query = normalize(query)
    if len(query) < 2:
        return []
    known = {zone: city for cities in TIMEZONE_REGIONS.values() for city, zone in cities}
    zones = {zone for zone in available_timezones() if zone.split('/')[0] in
             {'Africa', 'America', 'Antarctica', 'Arctic', 'Asia', 'Atlantic', 'Australia', 'Europe', 'Indian', 'Pacific'}}
    zones.update(known)
    ranked = []
    for zone in zones:
        city = known.get(zone, zone.rsplit('/', 1)[-1].replace('_', ' '))
        label, identity = normalize(city), normalize(zone)
        if query == label or query == identity:
            rank = 0
        elif label.startswith(query):
            rank = 1
        elif query in label:
            rank = 2
        elif query in identity:
            rank = 3
        else:
            similarity = SequenceMatcher(None, query, label).ratio()
            if similarity < .75:
                continue
            rank = 4 + 1 - similarity
        ranked.append((rank, city, zone))
    return [(city, zone) for _, city, zone in sorted(ranked)[:25]]


async def owner_check(interaction, user_id):
    if interaction.user.id == user_id:
        return True
    await interaction.response.send_message(**message_payload('Use `/mysetup` to change your own settings.'),
                                            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    return False


class CitySearchButton(discord.ui.Button):
    def __init__(self, user_id, setup_payload, *, row=0):
        super().__init__(label='Search city', emoji='🔍', style=discord.ButtonStyle.primary, row=row)
        self.user_id, self.setup_payload = user_id, setup_payload

    async def callback(self, interaction):
        if await owner_check(interaction, self.user_id):
            modal = CitySearchModal(self.user_id, self.setup_payload)
            modal.previous_card = getattr(interaction, 'extras', {}).get('expiring_message_card')
            await interaction.response.send_modal(modal)


class CitySearchModal(discord.ui.Modal, title='Find Your Timezone City'):
    city = discord.ui.TextInput(label='City', placeholder='Dubai, London, Berlin…', min_length=2, max_length=80)

    def __init__(self, user_id, setup_payload):
        super().__init__(timeout=600)
        self.user_id, self.setup_payload = user_id, setup_payload
        self.previous_card = None

    async def on_submit(self, interaction):
        if not await owner_check(interaction, self.user_id):
            return
        query = str(self.city)
        results = city_matches(query)
        if self.previous_card:
            interaction.extras['expiring_message_card'] = self.previous_card
        exact = [zone for city, zone in results if normalize(query) in
                 {normalize(zone), *(normalize(alias) for alias in city.split(' / '))}]
        selected = exact[0] if len(exact) == 1 else results[0][1] if len(results) == 1 else None
        if selected:
            await interaction.response.defer()
            set_user_timezone(self.user_id, selected)
            payload = self.setup_payload(interaction)
            payload['view'].expire_with(interaction)
            await interaction.edit_original_response(**payload, allowed_mentions=discord.AllowedMentions.none())
            return
        text = ('## Choose Your Timezone City\n\nSelect your city below.' if results else
                '## No Cities Found\n\nTry a nearby major city or a different spelling.')
        payload = message_payload(text, controls=CityMatchesView(self.user_id, self.setup_payload, results))
        payload['view'].expire_with(interaction)
        await interaction.response.edit_message(**payload, allowed_mentions=discord.AllowedMentions.none())


class CityResultSelect(discord.ui.Select):
    def __init__(self, user_id, setup_payload, results):
        options = []
        now = datetime.now(UTC)
        for city, zone in results:
            minutes = int(now.astimezone(ZoneInfo(zone)).utcoffset().total_seconds() // 60)
            sign = '+' if minutes >= 0 else '−'
            hours, mins = divmod(abs(minutes), 60)
            options.append(discord.SelectOption(label=f'{city} · UTC{sign}{hours:02d}:{mins:02d}'[:100],
                                                description=zone, value=zone))
        super().__init__(placeholder='Choose your city…', options=options, row=0)
        self.user_id, self.setup_payload = user_id, setup_payload
        self.allowed_zones = {zone for _, zone in results}

    async def callback(self, interaction):
        if not await owner_check(interaction, self.user_id):
            return
        if self.values[0] not in self.allowed_zones:
            return
        await interaction.response.defer()
        set_user_timezone(self.user_id, self.values[0])
        await interaction.edit_original_response(**self.setup_payload(interaction),
                                                 allowed_mentions=discord.AllowedMentions.none())


class CityMatchesView(discord.ui.View):
    setup_controls = True

    def __init__(self, user_id, setup_payload, results):
        super().__init__(timeout=600)
        self.user_id, self.setup_payload = user_id, setup_payload
        if results:
            self.add_item(CityResultSelect(user_id, setup_payload, results))
        self.add_item(CitySearchButton(user_id, setup_payload, row=1))

    @discord.ui.button(label='Back to setup', style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, button):
        if await owner_check(interaction, self.user_id):
            await interaction.response.edit_message(**self.setup_payload(interaction),
                                                    allowed_mentions=discord.AllowedMentions.none())
