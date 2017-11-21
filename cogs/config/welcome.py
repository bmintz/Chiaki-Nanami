import collections
import discord
import enum
import functools

from discord.ext import commands
from datetime import datetime
from more_itertools import one

from ._initroot import InitRoot

from ..utils import time
from ..utils.formats import multi_replace
from ..utils.misc import nice_time, ordinal

__schema__ = """
    CREATE TABLE IF NOT EXISTS server_messages (
        guild_id BIGINT,
        is_welcome BOOLEAN,
        channel_id BIGINT NULL,
        message TEXT NULL,
        delete_after SMALLINT DEFAULT 0,
        enabled BOOLEAN DEFAULT FALSE
    );
"""

_DEFAULT_CHANNEL_CHANGE_URL = ('https://github.com/discordapp/discord-api-docs/blob/master/docs/'
                               'Change_Log.md#breaking-change-default-channels')


fields = 'guild_id is_welcome channel_id message delete_after enabled'.split()
ServerMessage = collections.namedtuple('ServerMessage', fields)
ServerMessage.__new__.__defaults__ = (None, ) * len(fields)
del fields

_server_message_check = functools.partial(commands.has_permissions, manage_guild=True)


class ServerMessageType(enum.Enum):
    leave = False
    welcome = True

    def __str__(self):
        return self.name

    @property
    def action(self):
        return _lookup[self][0]

    @property
    def past_tense(self):
        return _lookup[self][1]

    @property
    def command_name(self):
        return _lookup[self][2]

    @property
    def toggle_text(self):
        return _lookup[self][3]


_lookup = {
    ServerMessageType.leave: ('leaves', 'left', 'bye', 'mourn the loss of members ;-;'),
    ServerMessageType.welcome: ('joins', 'joined', 'welcome', 'welcome all new members to the server! ^o^')
}


def special_message(message):
    return message if '{user}' in message else f'{{user}}{message}'


class WelcomeMessages(InitRoot):
    """Commands related to welcome and leave messages."""
    # TODO: Put this in a config module.

    def __init__(self, bot):
        super().__init__(bot)

    # ------------ config helper functions --------------------

    async def _get_server_config(self, guild_id, thing, *, connection=None):
        connection = connection or self.bot.pool

        query = "SELECT * FROM server_messages WHERE guild_id = $1 AND is_welcome = $2"
        row = await connection.fetchrow(query, guild_id, thing.value)
        return ServerMessage(**row) if row else None

    async def _update_server_config(self, ctx, thing, **kwarg):
        column, value = one(kwarg.items())
        query = f"""INSERT INTO server_messages (guild_id, is_welcome, {column})
                    VALUES ($1, $2, $3)
                    ON CONFLICT (guild_id, is_welcome)
                    DO UPDATE SET {column} = $3
                """
        await ctx.db.execute(query, ctx.guild.id, thing.value, value)

    async def _show_server_config(self, ctx, thing):
        config = await self._get_server_config(ctx.guild.id, thing, connection=ctx.db)
        if not config:
            commands = sorted(ctx.command.commands, key=str)
            message = ("Um... you haven't even set this at all...\n"
                       f"Please use one of the {len(commands)} subcommands to get started.")

            embed = discord.Embed(colour=0xf44336, description=message)
            for c in commands:
                embed.add_field(name=f'{ctx.prefix} {c}', value=c.short_doc)

            return await ctx.send(embed=embed)

        colour, prefix = (0x4CAF50, 'en') if config.enabled else (0xf44336, 'dis')
        message = (f'**Message:**\n{config.message}'
                   if config.message else
                   f"You haven't even set the message...\nUse `{thing.command_name} message` to set it.")

        embed = (discord.Embed(colour=colour, description=message)
                 .set_author(name=f'{thing.name.title()} Status: {prefix}abled')
                 )

        ch_id = config.channel_id
        if ch_id == -1:
            ch_field = f"You haven't even set this! Use `{thing.command_name} channel your_channel` right now!"
        else:
            channel = ctx.bot.get_channel(ch_id)
            if channel:
                ch_field = channel.mention
            else:
                ch_field = (f"{config.channel_id} has been deleted, pls reset it with "
                            f"`{thing.command_name} channel your_channel`")

        embed.add_field(name='Channel', value=ch_field, inline=False)

        delete_after = (time.duration_units(config.delete_after)
                        if config.delete_after > 0 else
                        "Actually wait, I won't delete it at all...")

        embed.add_field(name='I will delete it after', value=delete_after, inline=False)

        await ctx.send(embed=embed)

    async def _toggle_config(self, ctx, do_thing, *, thing):
        if do_thing is None:
            await self._show_server_config(ctx, thing)
        else:
            await self._update_server_config(ctx, thing, enabled=do_thing)
            to_say = (f"Yay I will {thing.toggle_text}" if do_thing else
                      "Oki I'll just sit in my corner then :~")
            await ctx.send(to_say)

    async def _message_config(self, ctx, message, *, thing):
        if message:
            await self._update_server_config(ctx, thing, message=message)
            await ctx.send(f"{thing.name.title()} message has been set to *{message}*")
        else:
            config = await self._get_server_config(ctx.guild.id, thing, connection=ctx.db)
            to_say = (f"I will say {config.message} to the user."
                      if (config and config.message) else
                      "I won't say anything...")
            await ctx.send(to_say)

    async def _channel_config(self, ctx, channel, *, thing):
        if channel:
            await self._update_server_config(ctx, thing, channel_id=channel.id)
            await ctx.send(f'Ok, {channel.mention} it is then!')
        else:
            config = await self._get_server_config(ctx.guild.id, thing, connection=ctx.db)

            channel = self.bot.get_channel(getattr(config, 'channel_id', None))

            if channel:
                message = f"I'm gonna say the {thing} message in {channel.mention}"
            else:
                message = ("I don't have a channel at the moment, "
                           f"set one with `{ctx.prefix}{ctx.command} my_channel`")

            await ctx.send(message)

    async def _delete_after_config(self, ctx, duration, *, thing):
        if duration is None:
            config = await self._get_server_config(ctx.guild.id, thing, connection=ctx.db)
            duration = config.delete_after if config else 0
            message = (f"I won't delete the {thing} message." if duration < 0 else
                       f"I will delete the {thing} message after {time.duration_units(duration)}.")
            await ctx.send(message)
        else:
            await self._update_server_config(ctx, thing, delete_after=duration)
            message = (f"Ok, I'm deleting the {thing} message after {time.duration_units(duration)}"
                       if duration > 0 else
                       f"Ok, I won't delete the {thing} message.")

            await ctx.send(message)

    # --------------------- commands -----------------------

    def _do_command(*, thing):
        _toggle_help = f"""
        Sets whether or not I announce when someone {thing.action}s the server.

        Specifying with no arguments will toggle it.
        """

        _channel_help = f"""
            Sets the channel where I will {thing}.
            If no arguments are given, it shows the current channel.

            This **must** be specified due to the fact that default channels
            are no longer a thing. ([see here]({_DEFAULT_CHANNEL_CHANGE_URL}))

            If this isn't specified, or the channel was deleted, the message
            will not show.
            """

        _delete_after_help = f"""
            Sets the time it takes for {thing} messages to be auto-deleted.
            Passing it with no arguments will return the current duration.

            A number less than or equal 0 will disable automatic deletion.
            """

        _message_help = f"""
            Sets the bot's message when a member {thing.action}s this server.

            The following special formats can be in the message:
            `{{{{user}}}}`     = The member that {thing.past_tense}. If one isn't placed,
                                 it's placed at the beginning of the message.
            `{{{{uid}}}}`      = The ID of member that {thing.past_tense}.
            `{{{{server}}}}`   = The name of the server.
            `{{{{count}}}}`    = How many members are in the server now.
            `{{{{countord}}}}` = Like `{{{{count}}}}`, but as an ordinal,
                                 (e.g. instead of `5` it becomes `5th`.)
            `{{{{time}}}}`     = The date and time when the member {thing.past_tense}.
            """

        @commands.group(name=thing.command_name, help=_toggle_help, invoke_without_command=True)
        @_server_message_check()
        async def group(self, ctx, enable: bool=None):
            await self._toggle_config(ctx, enable, thing=thing)

        @group.command(name='message', help=_message_help)
        @_server_message_check()
        async def group_message(self, ctx, *, message: special_message):
            await self._message_config(ctx, message, thing=thing)

        @group.command(name='channel', help=_channel_help)
        @_server_message_check()
        async def group_channel(self, ctx, *, channel: discord.TextChannel):
            await self._channel_config(ctx, channel, thing=thing)

        @group.command(name='delete', help=_delete_after_help)
        @_server_message_check()
        async def group_delete(self, ctx, *, duration: int):
            await self._delete_after_config(ctx, duration, thing=thing)

        return group, group_message, group_channel, group_delete

    welcome, welcome_message, welcome_channel, welcome_delete = _do_command(
        thing=ServerMessageType.welcome,
    )

    bye, bye_message, bye_channel, bye_delete = _do_command(
        thing=ServerMessageType.leave,
    )

    # ----------------- events ------------------------

    async def _maybe_do_message(self, member, thing, time):
        guild = member.guild
        config = await self._get_server_config(guild.id, thing)

        if not (config and config.enabled):
            return

        channel_id = config.channel_id
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return

        message = config.message
        if not message:
            return

        member_count = guild.member_count

        replacements = {
            '{user}': member.mention,
            '{uid}': str(member.id),
            '{server}': str(guild),
            '{count}': str(member_count),
            '{countord}': ordinal(member_count),
            # TODO: Should I use %c...?
            '{time}': nice_time(time)
        }

        delete_after = config.delete_after
        if delete_after <= 0:
            delete_after = None

        # Not using str.format because that will raise KeyError on anything surrounded in {}
        message = multi_replace(message, replacements)
        await channel.send(message, delete_after=delete_after)

    async def on_member_join(self, member):
        await self._maybe_do_message(member, ServerMessageType.welcome, member.joined_at)

    # Hm, this needs less repetition
    # XXX: Lower the repetition
    async def on_member_remove(self, member):
        await self._maybe_do_message(member, ServerMessageType.leave, datetime.utcnow())


def setup(bot):
    bot.add_cog(WelcomeMessages(bot))
