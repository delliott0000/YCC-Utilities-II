from __future__ import annotations

import os
import asyncio
import logging
from typing import TYPE_CHECKING
from time import time
from datetime import timedelta
from traceback import format_exception as format_error

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

try:
    from discord.utils import MISSING, utcnow
    from discord.ext import commands, tasks
    from discord import (
        __version__ as __discord__,
        Intents,
        HTTPException,
        LoginFailure,
        PrivilegedIntentsRequired,
        Activity,
        ActivityType,
        User,
        Member,
        Embed,
        Color
    )

    # Local imports
    from resources import config
    from core.modlog import ModLogEntry
    from core.mongo import MongoDBClient
    from core.embed import EmbedField, CustomEmbed
    from core.errors import DurationError, ModLogNotFound
    from core.context import CustomContext, enforce_clearance
    from core.help import CustomHelpCommand
    from core.metadata import MetaData
    from components.traceback import TracebackView
    from components.roles import RoleView

except ModuleNotFoundError as unknown_import:
    logging.fatal(f'Missing required dependencies - {unknown_import}.')
    raise SystemExit()

if TYPE_CHECKING:
    from discord import Guild, Message
    from discord.abc import Messageable
    from discord.ui import View


class CustomBot(commands.Bot):

    _duration_mapping = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800, 'y': 31536000}

    def __init__(self):

        self.start_time = time()

        intents = Intents.all()
        intents.typing = intents.presences = False

        super().__init__(
            command_prefix=config.PREFIX,
            intents=intents,
            owner_ids=config.OWNERS,
            help_command=CustomHelpCommand(),
            case_insensitive=True,
            max_messages=10000
        )

        self.guild_id: int = config.GUILD_ID
        self.guild: Guild | None = None
        self.mongo_db: MongoDBClient | None = None
        self.metadata: MetaData | None = None
        self.bans: list[int] = []
        self.perm_duration: int = 2 ** 32 - 1

        self.add_check(enforce_clearance, call_once=True)

        self.loops: tuple[tasks.Loop, ...] = (self.modlogs_tasks, self.init_status)
        self.extension_folders: tuple[str, ...] = ('./ext', './events')

    def convert_duration(self, duration: str, allow_any_duration: bool = False) -> timedelta:
        try:
            td = timedelta(seconds=int(duration[:-1]) * self._duration_mapping[duration[-1:].lower()])
        except (KeyError, ValueError):
            raise DurationError()
        if td.total_seconds() < 60 and allow_any_duration is False:
            raise DurationError()
        return td

    def clearance_to_str(self, clearance: int) -> str:
        if clearance <= 0:
            return '**`Member`**'
        elif clearance >= 9:
            return '**`Owner`**'
        rs = ('helper', 'tmod', 'rmod', 'smod', 'hmod', 'senior', 'bot', 'admin')
        clearance_map = {rs.index(r) + 1: self.metadata.get(f'{r}_role') for r in rs}
        clearance_str = clearance_map.get(clearance)
        return f'<@&{clearance_str}>' if clearance_str else f'**`None`**'

    @staticmethod
    def _new_embed(**kwargs) -> CustomEmbed:
        return CustomEmbed(
            title=kwargs.get('title'),
            description=kwargs.get('description'),
            color=kwargs.get('color', Color.blue()),
            timestamp=kwargs.get('timestamp')
        )

    def fields_to_embeds(self, fields: list[EmbedField], **kwargs) -> list[CustomEmbed]:
        embed_list = [self._new_embed(**kwargs)]

        for field in fields:
            if len(embed_list[-1].fields) > kwargs.get('field_limit', 5) - 1 or len(embed_list[-1]) + len(field) > 6000:
                embed_list.append(self._new_embed(**kwargs))
            embed_list[-1].append_field(field)

        author_name = kwargs.get('author_name')
        author_icon = kwargs.get('author_icon')
        for embed in embed_list:
            if author_name and author_icon:
                embed.set_author(name=author_name, icon_url=author_icon)
            embed.set_footer(text=f'Page {embed_list.index(embed) + 1} of {len(embed_list)}')

        return embed_list

    @staticmethod
    async def basic_embed(destination: Messageable, message: str, color: Color, view: View = MISSING) -> Message:
        embed = Embed(description=message, color=color)
        return await destination.send(embed=embed, view=view)

    async def neutral_embed(self, destination: Messageable, message: str, view: View = MISSING) -> Message:
        return await self.basic_embed(destination, message, Color.blue(), view=view)

    async def good_embed(self, destination: Messageable, message: str, view: View = MISSING) -> Message:
        return await self.basic_embed(destination, message, Color.green(), view=view)

    async def bad_embed(self, destination: Messageable, message: str, view: View = MISSING) -> Message:
        return await self.basic_embed(destination, message, Color.red(), view=view)

    async def user_to_member(self, user: User, raise_exception: bool = False) -> Member | None:
        try:
            return self.guild.get_member(user.id) or await self.guild.fetch_member(user.id)
        except HTTPException as error:
            if raise_exception is True:
                raise error

    async def member_clearance(self, member: User | Member) -> int:
        if member.id in self.owner_ids or member == self.guild.owner:
            return 9
        elif isinstance(member, User):
            try:
                member = self.guild.get_member(member.id) or await self.guild.fetch_member(member.id)
            except HTTPException:
                return 0

        roles = [role.id for role in member.roles]
        data = self.metadata

        return \
            8 if data.get('admin_role') in roles else \
            7 if data.get('bot_role') in roles else \
            6 if data.get('senior_role') in roles else \
            5 if data.get('hmod_role') in roles else \
            4 if data.get('smod_role') in roles else \
            3 if data.get('rmod_role') in roles else \
            2 if data.get('tmod_role') in roles else \
            1 if data.get('helper_role') in roles else 0

    async def check_target_member(self, member: User | Member) -> None:
        if await self.member_clearance(member) > 0:
            raise commands.CheckFailure('The target of this moderation is protected.')

    async def command_names(self) -> list[str]:
        faqs = [entry.get('shortcut', '').lower() for entry in await self.mongo_db.fetch_commands('faq')]
        customs = [entry.get('shortcut', '').lower() for entry in await self.mongo_db.fetch_commands('custom')]
        aliases = [alias.lower() for command in self.commands for alias in command.aliases if command.aliases]
        regular = [command.name.lower() for command in self.commands]
        return faqs + customs + regular + aliases

    async def on_command_error(self, ctx: CustomContext, error: commands.CommandError) -> None:
        reset_cooldown = True

        if isinstance(error, (commands.CheckFailure, commands.CommandNotFound)):
            return

        elif isinstance(error, commands.MissingRequiredArgument):
            await self.send_command_help(ctx, ctx.command)
            return ctx.command.reset_cooldown(ctx)

        elif isinstance(error, commands.CommandOnCooldown):
            message = f'Wait `{round(error.retry_after)}s` before doing that again.'
            reset_cooldown = False

        elif isinstance(error, commands.BotMissingPermissions):
            message = f'Bot missing required permissions: `{", ".join(error.missing_permissions).replace("_", " ")}`.'

        elif isinstance(error, (commands.UserNotFound, commands.MemberNotFound,
                                commands.ChannelNotFound, commands.RoleNotFound)):
            message = f'{error.__class__.__name__[:-8]} not found.'

        elif isinstance(error, commands.BadArgument):
            message = 'Incorrect argument type(s).'

        elif isinstance(error, commands.CommandInvokeError):
            message = str(error.original)

        else:
            message = f'An unexpected error occurred: {error}'

        if reset_cooldown is True:
            ctx.command.reset_cooldown(ctx)

        message = await self.bad_embed(ctx, f'❌ {message}')

        traceback = ''.join(format_error(type(error), error, error.__traceback__))
        if len(traceback) > 1960:
            traceback = '\n'.join(traceback[-1960:].split('\n')[1:])
            note = ' (Last 2,000)'
        else:
            note = ''
        traceback = f'**Traceback{note}:**\n```\n{traceback}\n```'

        await message.edit(view=TracebackView(self, message, traceback))

    async def send_command_help(self, ctx: CustomContext, command: commands.Command) -> None:
        req = command.extras.get('requirement', 0)
        if await self.member_clearance(ctx.author) < req:
            return

        description, aliases, name, params = command.description, command.aliases, command.name, command.clean_params
        params_str = " ".join([f'{"opt" if not params[param].required else ""}<{param}>' for param in params])

        command_help_embed = Embed(
            color=Color.blue(),
            title=f'{self.command_prefix}{name} Command',
            description=description + f' Requires {self.clearance_to_str(req)} **`[{req}]`** or higher.')
        command_help_embed.set_author(name='Help Menu', icon_url=self.user.avatar or self.user.default_avatar)
        command_help_embed.set_footer(text=f'Use {self.command_prefix}help to view all commands.')

        command_help_embed.add_field(name='Usage:',
                                     value=f'`{self.command_prefix}{name} {params_str}`',
                                     inline=False)
        command_help_embed.add_field(name='Aliases:',
                                     value=f'`{", ".join(aliases)}`' if aliases else '`None`',
                                     inline=False)
        await ctx.send(embed=command_help_embed)

    async def on_message(self, message: Message) -> None:
        if not message.guild or message.guild.id != self.guild_id or message.author.bot:
            return

        ctx = await self.get_context(message, cls=CustomContext)
        await self.invoke(ctx)

    async def on_member_join(self, member: Member) -> None:
        if member.guild != self.guild:
            return

        try:
            member_modlogs = await self.mongo_db.search_modlog(user_id=member.id, active=True, deleted=False)
        except ModLogNotFound:
            return

        for modlog in member_modlogs:
            if modlog.expired is True:
                continue

            try:

                if modlog.type == 'mute':
                    duration = timedelta(seconds=modlog.until - utcnow().timestamp())
                    await member.timeout(duration)

                elif modlog.type == 'channel_ban':
                    channel = self.get_channel(modlog.channel_id) or await self.fetch_channel(modlog.channel_id)
                    await channel.set_permissions(member, view_channel=False)

            except HTTPException as error:
                logging.error(f'Failed to enforce case {modlog.id} on member re-join - {error}')

    @tasks.loop(minutes=1)
    async def modlogs_tasks(self) -> None:
        await self.wait_until_ready()

        self.guild = self.get_guild(self.guild_id) or self.guild

        try:
            expired_logs = await self.mongo_db.search_modlog(active=True, deleted=False)
        except ModLogNotFound:
            return

        for modlog in expired_logs:
            if modlog.expired is False:
                continue

            try:
                user = self.get_user(modlog.user_id) or await self.fetch_user(modlog.user_id)

                if modlog.type == 'ban':
                    await self.guild.unban(user)

                elif modlog.type == 'channel_ban':
                    channel = self.get_channel(modlog.channel_id) or await self.fetch_channel(modlog.channel_id)
                    member = await self.user_to_member(user, raise_exception=True)
                    await channel.set_permissions(member, view_channel=None)

            except Exception as error:
                logging.error(f'Failed to resolve modlog case {modlog.id} - {error}')

            await self.mongo_db.update_modlog(_case_id=modlog.id, active=False)

    @tasks.loop(count=1)
    async def init_status(self) -> None:
        await self.wait_until_ready()
        await self.change_presence(activity=Activity(type=ActivityType.listening, name=self.metadata.activity))

    async def setup_hook(self) -> None:
        logging.info(f'Logging in as {self.user.name} (ID: {self.user.id})...')

        try:
            owners = [await self.fetch_user(_id) for _id in self.owner_ids]
            self.guild = await self.fetch_guild(self.guild_id)
            logging.info(f'Owner(s): {", ".join([owner.name for owner in owners])}')
            logging.info(f'Guild: {self.guild.name}')

        except HTTPException:
            logging.fatal('Invalid IDs passed. Please check your config.py file is correct.')
            raise SystemExit()

        logging.info('Fetching guild bans, this may take a while...')
        self.bans = [entry.user.id async for entry in self.guild.bans(limit=None)]

        self.metadata = await self.mongo_db.get_metadata()

        for loop in self.loops:
            loop.add_exception_type(Exception)
            loop.start()

        async for view in self.mongo_db.get_views():
            roles = [self.guild.get_role(role_id) for role_id in view.get('role_ids', [])]
            message_id = view.get('message_id')
            if None in roles:
                logging.warning(f'Cannot add persistent view to message (ID: {message_id}) due to unknown role IDs')
                continue
            self.add_view(RoleView(roles), message_id=message_id)

    def run_bot(self) -> None:

        async def _run_bot():
            async with self, MongoDBClient(self, config.MONGO) as self.mongo_db:
                for folder in self.extension_folders:
                    for file in os.listdir(folder):
                        if file.endswith('.py'):
                            extension = f'{folder[2:]}.{file[:-3]}'
                            try:
                                await self.load_extension(extension)
                            except (commands.ExtensionFailed, commands.NoEntryPointError) as extension_error:
                                logging.error(f'Extension {extension} could not be loaded: {extension_error}')
                try:
                    await self.start(config.TOKEN)
                except LoginFailure:
                    logging.fatal('Invalid token passed.')
                except PrivilegedIntentsRequired:
                    logging.fatal('Intents are being requested that have not been enabled in the developer portal.')

        async def _cleanup():
            self.modlogs_tasks.cancel()
            self.init_status.cancel()

        # noinspection PyUnresolvedReferences
        with asyncio.Runner() as runner:
            try:
                runner.run(_run_bot())
            except (KeyboardInterrupt, SystemExit):
                logging.info('Received signal to terminate bot and event loop.')
            finally:
                logging.info('Cleaning up tasks and connections...')
                runner.run(_cleanup())
                logging.info('Done. Have a nice day!')


if __name__ == '__main__':

    if __discord__ == '2.5.2':
        bot = CustomBot()
        bot.run_bot()

    else:
        logging.fatal('The incorrect version of discord.py has been installed.')
        logging.fatal('Current Version: {}'.format(__discord__))
        logging.fatal('Required: 2.5.2')
