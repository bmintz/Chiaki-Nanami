import asyncpg
import asyncqlio
import datetime
import discord
import itertools
import logging

from discord.ext import commands

from .tables.base import TableBase
from .utils import formats
from .utils.paginator import ListPaginator

from core.cog import Cog

tag_logger = logging.getLogger(__name__)

class TagError(commands.UserInputError):
    pass


class Tag(TableBase, table_name='tags'):
    name = asyncqlio.Column(asyncqlio.String, index=True, primary_key=True)
    # If this is an alias, this will be repurposed to point to the original tag.
    content = asyncqlio.Column(asyncqlio.String, default='')

    is_alias = asyncqlio.Column(asyncqlio.Boolean)
    # TODO:
    embed = asyncqlio.Column(asyncqlio.String, default='')

    # Some important metadata.
    owner_id = asyncqlio.Column(asyncqlio.BigInt)
    uses = asyncqlio.Column(asyncqlio.Integer, default=0)
    location_id = asyncqlio.Column(asyncqlio.BigInt, index=True, primary_key=True)
    created_at = asyncqlio.Column(asyncqlio.Timestamp)


class MemberTagPaginator(ListPaginator):
    def __init__(self, *args, member, **kwargs):
        super().__init__(*args, **kwargs)
        self.member = member

    def _create_embed(self, idx, page):
        return (super()._create_embed(idx, page)
                       .set_author(name=f'Tags made by {self.member.display_name}', icon_url=self.member.avatar_url)
                )


class ServerTagPaginator(ListPaginator):
    def _create_embed(self, idx, page):
        guild = self.context.guild
        embed = super()._create_embed(idx, page).set_author(name=f'Tags in {guild}')
        if guild.icon:
            return embed.set_author(name=embed.author.name, icon_url=guild.icon_url)
        return embed


class TagName(commands.clean_content):
    async def convert(self, ctx, argument):
        converted = await super().convert(ctx, argument)
        lower = converted.lower()

        if len(lower) > 200:
            raise commands.BadArgument('Too long! It has to be less than 200 characters long.')

        first_word, _, _ = lower.partition(' ')

        # get tag command.
        root = ctx.bot.get_command('tag')
        if first_word in root.all_commands:
            raise commands.BadArgument('This tag name starts with a reserved word.')

        return lower


class Tags(Cog):
    """You're it."""
    def __init__(self, bot):
        self.bot = bot

    async def __error(self, ctx, error):
        print('error!', error)
        if isinstance(error, TagError):
            await ctx.send(error)

    async def _disambiguate_error(self, session, name, guild_id):
        # ~~thanks danno~~
        message = f'Tag "{name}" not found...'

        query = """SELECT   name
                   FROM     tags
                   WHERE    location_id={guild_id} AND name % {name}
                   ORDER BY similarity(name, {name}) DESC
                   LIMIT 5;
                """
        params = {'guild_id': guild_id, 'name': name}
        try:
            results = await (await session.cursor(query, params)).flatten()
        except asyncpg.SyntaxOrAccessError:
            # % and similarity aren't supported, which means the owner didn't do
            # CREATE EXTENSION pg_trgm in their database
            tag_logger.error('pg_trgm extension not created, contact %s to create it for the tags', self.bot.owner)
        else:
            if results:
                # f-strings can't have backslashes in {}
                message += ' Did you mean...\n' + '\n'.join(r['name'] for r in results)

        return TagError(message)

    async def _get_tag(self, session, name, guild_id):
        query = (session.select.from_(Tag)
                        .where((Tag.name == name)
                               & (Tag.location_id == guild_id)))

        tag = await query.first()
        if tag is None:
            raise await self._disambiguate_error(session, name, guild_id)

        return tag

    async def _get_original_tag(self, session, name, guild_id):
        tag = await self._get_tag(session, name, guild_id)
        if tag.is_alias:
            return await self._get_tag(session, tag.content, guild_id)
        return tag

    @commands.group(invoke_without_command=True)
    async def tag(self, ctx, *, name: TagName):
        """Retrieves a tag, if one exists."""
        tag = await self._get_original_tag(ctx.session, name, ctx.guild.id)
        await ctx.send(tag.content)

        await (ctx.session.update.table(Tag)
                          .where((Tag.name == name.lower()) & (Tag.location_id == ctx.guild.id))
                          .set(Tag.uses + 1)
               )

    @tag.command(name='create', aliases=['add'])
    async def tag_create(self, ctx, name: TagName, *, content):
        """Creates a new tag."""
        tag = Tag(
            name=name,
            content=content,
            is_alias=False,
            owner_id=ctx.author.id,
            location_id=ctx.guild.id,
            created_at=datetime.datetime.utcnow()
        )

        try:
            await ctx.session.add(tag)
        except asyncpg.UniqueViolationError as e:
            raise TagError(f'Tag {name} already exists...') from e
        else:
            await ctx.send(f'Successfully created tag {name}! ^.^')

    @tag.command(name='edit')
    async def tag_edit(self, ctx, name, *, new_content):
        """Edits a tag that *you* own.

        You can only edit actual tags. i.e. you can't edit aliases.
        """
        tag = await self._get_tag(ctx.session, name, ctx.guild.id)
        if tag.is_alias:
            return await ctx.send("This tag is an alias. I can't edit it.")

        tag.content = new_content
        await ctx.session.merge(tag)
        await ctx.send("Successfully edited the tag!")

    @tag.command(name='alias')
    async def tag_alias(self, ctx, alias: TagName, *, original: TagName):
        """Creats an alias of a tag.

        You own the alias. However, if the original tag gets deleted,
        so does your alias.

        You also can't edit the alias.
        """
        # Make sure the original tag exists.
        tag = await self._get_original_tag(ctx.session, original, ctx.guild.id)
        new_tag = Tag(
            name=alias.lower(),
            content=tag.name,
            is_alias=True,
            owner_id=ctx.author.id,
            location_id=ctx.guild.id,
            created_at=datetime.datetime.utcnow()
        )

        try:
            await ctx.session.add(new_tag)
        except asyncpg.UniqueViolationError as e:
            raise TagError(f'Alias {alias} already exists...') from e
        else:
            await ctx.send(f'Successfully created alias {alias} that points to {original}! ^.^')

    @tag.command(name='delete', aliases=['remove'])
    async def tag_delete(self, ctx, *, name: TagName):
        """Removes a tag or alias.

        Only the owner of the tag or alias can delete it.

        However, if you have Manage Server perms you can delete
        a tag *regardless* of whether or not it's yours.
        """

        is_mod = ctx.author.permissions_in(ctx.channel).manage_guild

        # idk how wasteful this is. Probably very.
        tag = await self._get_tag(ctx.session, name, ctx.guild.id)
        if tag.owner_id != ctx.author.id and not is_mod:
            return await ctx.send("This tag is not yours.")

        await ctx.session.remove(tag)
        if not tag.is_alias:
            # Slow path, we gotta delete all aliases.
            await ctx.session.delete(Tag).where((Tag.location_id == ctx.guild.id)
                                                & (Tag.is_alias == True)
                                                & (Tag.content == name.lower()))

            await ctx.send(f"Tag {name} and all of its aliases have been deleted.")
        else:
            await ctx.send("Alias successfully deleted.")

    async def _get_tag_rank(self, session, tag):
        query = """SELECT COUNT(*) FROM tags
                   WHERE location_id = {guild_id}
                   AND (uses, created_at) >= ({uses}, {created})
                """
        # XXX: Not sure if asyncqlio covers tuple comparisons.
        params = {'guild_id': tag.location_id, 'uses': tag.uses, 'created': tag.created_at}

        result = await session.cursor(query, params)
        return await result.fetch_row()

    @tag.command(name='info')
    async def tag_info(self, ctx, *, tag: TagName):
        """Shows the info of a tag or alias."""
        # XXX: This takes roughly 8-16 ms. Not good, but to make my life
        #      simpler I'll ignore it for now until the bot gets really big
        #      and querying the tags starts becoming expensive.
        tag = await self._get_tag(ctx.session, tag, ctx.guild.id)
        rank = await self._get_tag_rank(ctx.session, tag)

        user = ctx.bot.get_user(tag.owner_id)
        creator = user.mention if user else f'Unknown User (ID: {tag.owner_id})'
        icon_url = user.avatar_url if user else discord.Embed.Empty

        embed = (discord.Embed(colour=ctx.bot.colour, timestamp=tag.created_at)
                 .set_author(name=tag.name, icon_url=icon_url)
                 .add_field(name='Created by', value=creator)
                 .add_field(name='Used', value=f'{formats.pluralize(time=tag.uses)}', inline=False)
                 .add_field(name='Rank', value=f'#{rank["count"]}', inline=False)
                 .set_footer(text='Created')
                 )

        if tag.is_alias:
            embed.description = f'Original Tag: {tag.content}'

        await ctx.send(embed=embed)

    @tag.command(name='search')
    async def tag_search(self, ctx, *, name):
        """Searches and shows up to the 50 closest matches for a given name."""
        query = """SELECT   name
                   FROM     tags
                   WHERE    location_id={guild_id} AND name % {name}
                   ORDER BY similarity(name, {name}) DESC
                   LIMIT 5;
                """
        params = {'guild_id': ctx.guild.id, 'name': name}
        tags = [tag['name'] async for tag in await ctx.session.cursor(query, params)]
        entries = itertools.starmap('{0}. {1}'.format, enumerate(tags, 1)) if tags else ['No results found... :(']

        pages = ListPaginator(ctx, entries, title=f'Tags relating to {name}')
        await pages.interact()

    # XXX: too much repetition...
    @tag.command(name='list', aliases=['all'])
    async def tag_list(self, ctx):
        """Shows all the tags in the server."""
        query = ctx.session.select(Tag).where(Tag.location_id == ctx.guild.id).order_by(Tag.name)
        tags = [tag.name async for tag in await query.all()]

        entries = (
            itertools.starmap('{0}. {1}'.format, enumerate(tags, 1)) if tags else
            ('There are no tags. Use `{ctx.prefix}tag create` to fix that.', )
        )

        paginator = ServerTagPaginator(ctx, entries)
        await paginator.interact()

    @tag.command(name='from', aliases=['by'])
    async def tag_by(self, ctx, *, member: discord.Member = None):
        """Shows all the tags in the server."""
        member = member or ctx.author
        query = (ctx.session.select.from_(Tag)
                            .where((Tag.location_id == ctx.guild.id) & (Tag.owner_id == member.id))
                            .order_by(Tag.name)
                 )

        tags = [tag.name async for tag in await query.all()]

        entries = (
            itertools.starmap('{0}. {1}'.format, enumerate(tags, 1)) if tags else
            (f"{member} didn't make any tags yet. :(", )
        )
        paginator = MemberTagPaginator(ctx, entries, member=member)
        await paginator.interact()

    @commands.group(invoke_without_command=True)
    async def tags(self, ctx):
        """Alias for `{prefix}tag list`."""
        await ctx.invoke(self.tag_list)

    @tags.command(name='from', aliases=['by'])
    async def tags_from(self, ctx, *, member: discord.Member = None):
        """Alias for `{prefix}tag from/by`."""
        await ctx.invoke(self.tag_by, member=member)


def setup(bot):
    bot.add_cog(Tags(bot))
