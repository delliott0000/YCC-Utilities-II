from discord.ext import commands, tasks
from discord import (
    Message,
    Member,
    Embed,
    Color
)

from main import CustomBot

from core.context import CustomContext
from api.errors import TooManyRequests


class TokenHandler(commands.Cog):

    RATE_LIMITED = 'Failed to fetch MEE6 stats for {0} due to rate limits. It is possible that they\'re not ranked yet.'

    def __init__(self, bot: CustomBot):
        self.bot = bot
        self.recent_user_ids: list[int] = []

    def cog_load(self) -> None:
        self.update_user_tokens.start()

    def cog_unload(self) -> None:
        self.update_user_tokens.stop()

    @commands.Cog.listener()
    async def on_message(self, message: Message):
        if message.author.id not in self.recent_user_ids and not message.author.bot:
            self.recent_user_ids.append(message.author.id)

    @tasks.loop(minutes=15)
    async def update_user_tokens(self):
        for user_id in self.recent_user_ids:
            try:
                current_level = await self.bot.mee6.user_level(self.bot.guild_id, user_id)
            except TooManyRequests:
                break
            if current_level != (await self.bot.mongo_db.user_tokens_entry(user_id)).get('known_level'):
                await self.bot.mongo_db.update_user_level(user_id)
            self.recent_user_ids.remove(user_id)

    @commands.command(
        name='coins',
        aliases=['tokens'],
        description='Displays the member\'s current Café Coins count as well as their known MEE6 level.',
        extras={'requirement': 0}
    )
    async def coins(self, ctx: CustomContext, member: Member = None):
        async with ctx.typing():
            member = member or ctx.author
            if member.bot:
                raise Exception(f'{member.mention} is a bot.')
            try:
                data = await self.bot.mongo_db.user_tokens_entry(member.id)
            except TooManyRequests:
                raise Exception(self.RATE_LIMITED.format(member.mention))

            tokens_embed = Embed(color=Color.blue(), description=member.mention)

            tokens_embed.set_author(name='Café Coins', icon_url=self.bot.user.avatar or self.bot.user.default_avatar)
            tokens_embed.set_thumbnail(url=member.avatar or member.default_avatar)
            tokens_embed.set_footer(text='Earn more coins by levelling up!')

            tokens_embed.add_field(
                name='Total Coins:',
                value=f'> :coin: **`{data.get("tokens", 0):,}`**',
                inline=False)
            tokens_embed.add_field(
                name='MEE6 Level:',
                value=f'> <:chat_box:862204558780137482> **`{data.get("known_level", 0):,}`**',
                inline=False)

        await ctx.send(embed=tokens_embed)

    @commands.command(
        name='editcoins',
        aliases=[],
        description='Edits the Café Coins balance of a member. Balances cannot go below zero.',
        extras={'requirement': 3}
    )
    async def editcoins(self, ctx: CustomContext, member: Member, token_change: int):
        async with ctx.typing():
            if member.bot:
                raise Exception(f'{member.mention} is a bot.')
            try:
                result = await self.bot.mongo_db.edit_user_tokens(member.id, token_change)
            except TooManyRequests:
                raise Exception(self.RATE_LIMITED.format(member.mention))

            new_balance = result.get('tokens')

        await self.bot.good_embed(ctx, f'*Edited {member.mention}\'s balance to `{new_balance:,}` coins.*')


async def setup(bot: CustomBot):
    await bot.add_cog(TokenHandler(bot))
