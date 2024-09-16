from datetime import datetime, timedelta
from typing import Dict, Optional

from discord import Message
from discord.ext import commands

from main import CustomBot


class CustomSlowmode(commands.Cog):
    def __init__(self, bot: CustomBot) -> None:
        self.bot: CustomBot = bot
        self.slowmode_channels: Dict[int, int] = {
            1039886236602601512: 86400,  # channel_id: slowmode_duration_in_seconds
        }
        self.user_cooldowns: Dict[int, datetime] = {}

    @commands.Cog.listener(name="on_message")
    async def enforce_slowmode(self, message: Message) -> None:
        channel_id = message.channel.id
        author_id = message.author.id

        if channel_id not in self.slowmode_channels:
            return

        if message.author.bot:
            return

        clearance = await self.bot.member_clearance(message.author)
        if clearance:
            return

        slowmode_duration = self.slowmode_channels[channel_id]

        last_message_time = self.user_cooldowns.get(author_id)
        if last_message_time and (datetime.now(datetime.UTC) - last_message_time) < timedelta(
            seconds=slowmode_duration
        ):
            # User is on cooldown
            remaining_cooldown = (
                last_message_time + timedelta(seconds=slowmode_duration) - datetime.utcnow()
            ).total_seconds()
            await message.channel.send(
                f"{message.author.mention}, you are on cooldown. Please wait {int(remaining_cooldown)} seconds."
            )
            return

        self.user_cooldowns[author_id] = datetime.now(datetime.UTC)

async def setup(bot: CustomBot) -> None:
    await bot.add_cog(CustomSlowmode(bot))
