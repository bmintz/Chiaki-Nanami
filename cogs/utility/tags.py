import asyncpg
import discord
import itertools
import logging

from discord.ext import commands

from ..utils.examples import _get_static_example
from ..utils.paginator import ListPaginator
from ..utils import formats


__schema__ = """
    CREATE TABLE IF NOT EXISTS tags (
        name TEXT NOT NULL,
        content TEXT NOT NULL,
        is_alias BOOLEAN NOT NULL,
        -- metadata
        owner_id BIGINT NOT NULL,
        uses INTEGER NOT NULL DEFAULT 0,
        location_id BIGINT NOT NULL,
        created_at TIMESTAMP NOT NULL,
        PRIMARY KEY(name, location_id)
    );
    CREATE UNIQUE INDEX IF NOT EXISTS tags_uniq_idx ON tags (LOWER(name), location_id);
"""

tag_logger = logging.getLogger(__name__)


class TagError(commands.UserInputError):
    pass


class MemberTagPaginator(ListPaginator):
    def __init__(self, *args, member, **kwargs):
        super().__init__(*args, **kwargs)
        self.member = member

    def _create_embed(self, idx, page):
        header = f'Tags made by {self.member.display_name}'
        return (super()._create_embed(idx, page)
                       .set_author(name=header, icon_url=self.member.avatar_url)
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

    @staticmethod
    def random_example(ctx):
        ctx.__tag_example__ = example = _get_static_example('tag_examples')
        return example[0]


class TagContent(commands.clean_content):
    def random_example(ctx):
        return ctx.__tag_example__[1]


class Tags:
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
                   WHERE    location_id=$1 AND name % $2
                   ORDER BY similarity(name, $2) DESC
                   LIMIT 5;
                """
        try:
            results = await session.fetch(query, guild_id, name)
        except asyncpg.SyntaxOrAccessError:
            # % and similarity aren't supported, which means the owner didn't do
            # CREATE EXTENSION pg_trgm in their database
            tag_logger.error(
                f'pg_trgm extension not created, contact {self.bot.owner} '
                'to create it for the tags'
            )

        else:
            if results:
                # f-strings can't have backslashes in {}
                message += ' Did you mean...\n' + '\n'.join(r['name'] for r in results)

        return TagError(message)

    async def _get_tag(self, connection, name, guild_id):
        query = 'SELECT * FROM tags WHERE location_id = $1 AND lower(name) = $2'
        tag = await connection.fetchrow(query, guild_id, name)
        if tag is None:
            raise await self._disambiguate_error(connection, name, guild_id)

        return tag

    async def _get_original_tag(self, connection, name, guild_id):
        tag = await self._get_tag(connection, name, guild_id)
        if tag['is_alias']:
            return await self._get_tag(connection, tag['content'], guild_id)
        return tag

    @commands.group(invoke_without_command=True)
    async def tag(self, ctx, *, name: TagName):
        """Retrieves a tag, if one exists."""
        tag = await self._get_original_tag(ctx.db, name, ctx.guild.id)
        await ctx.send(tag['content'])

        query = 'UPDATE tags SET uses = uses + 1 WHERE name = $1 AND location_id = $2'
        await ctx.db.execute(query, tag['name'], ctx.guild.id)

    @tag.command(name='create', aliases=['add'])
    async def tag_create(self, ctx, name: TagName, *, content: TagContent):
        """Creates a new tag."""
        query = """INSERT INTO tags (is_alias, name, content, owner_id, location_id)
                   VALUES (FALSE, $1, $2, $3, $4)
                """

        try:
            await ctx.db.execute(query, name, content, ctx.author.id, ctx.guild.id)
        except asyncpg.UniqueViolationError as e:
            await ctx.send(f'Tag {name} already exists...')
        else:
            await ctx.send(f'Successfully created tag {name}! ^.^')

    @tag.command(name='edit')
    async def tag_edit(self, ctx, name: TagName, *, new_content: TagContent):
        """Edits a tag that *you* own.

        You can only edit actual tags. i.e. you can't edit aliases.
        """
        tag = await self._get_tag(ctx.db, name, ctx.guild.id)
        if tag['is_alias']:
            return await ctx.send("This tag is an alias. I can't edit it.")

        if tag['owner_id'] != ctx.author.id:
            return await ctx.send('This tag is not yours.')

        query = 'UPDATE tags SET content = $1 WHERE location_id = $2 AND name = $3;'
        await ctx.db.execute(query, new_content, ctx.guild.id, tag['name'])
        await ctx.send("Successfully edited the tag!")

    @tag.command(name='alias')
    async def tag_alias(self, ctx, alias: TagName, *, original: TagName):
        """Creats an alias of a tag.

        You own the alias. However, if the original tag gets deleted,
        so does your alias.

        You also can't edit the alias.
        """
        # Make sure the original tag exists.
        tag = await self._get_original_tag(ctx.db, original, ctx.guild.id)

        query = """INSERT INTO tags (is_alias, name, content, owner_id, location_id)
                   VALUES (TRUE, $1, $2, $3, $4)
                """

        try:
            await ctx.db.execute(query, alias, tag['name'], ctx.author.id, ctx.guild.id)
        except asyncpg.UniqueViolationError as e:
            return await ctx.send(f'Alias {alias} already exists...')
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
        tag = await self._get_tag(ctx.db, name, ctx.guild.id)
        if tag['owner_id'] != ctx.author.id and not is_mod:
            return await ctx.send("This tag is not yours.")

        query = """DELETE FROM tags
                   WHERE location_id = $1
                   AND ((is_alias AND LOWER(content) = $2) OR (LOWER(name) = $2))
                """

        await ctx.db.execute(query, ctx.guild.id, name)
        if not tag['is_alias']:
            await ctx.send(f"Tag {name} and all of its aliases have been deleted.")
        else:
            await ctx.send("Alias successfully deleted.")

    async def _get_tag_rank(self, connection, tag):
        query = """SELECT COUNT(*) FROM tags
                   WHERE location_id = $1
                   AND (uses, created_at) >= ($2, $3)
                """

        row = await connection.fetchrow(query, tag['location_id'], tag['uses'], tag['created_at'])
        return row[0]

    @tag.command(name='info')
    async def tag_info(self, ctx, *, tag: TagName):
        """Shows the info of a tag or alias."""
        # XXX: This takes roughly 8-16 ms. Not good, but to make my life
        #      simpler I'll ignore it for now until the bot gets really big
        #      and querying the tags starts becoming expensive.
        tag = await self._get_tag(ctx.db, tag, ctx.guild.id)
        rank = await self._get_tag_rank(ctx.db, tag)

        user = ctx.bot.get_user(tag['owner_id'])
        creator = user.mention if user else f'Unknown User (ID: {tag["owner_id"]})'
        icon_url = user.avatar_url if user else discord.Embed.Empty

        embed = (discord.Embed(colour=ctx.bot.colour, timestamp=tag['created_at'])
                 .set_author(name=tag['name'], icon_url=icon_url)
                 .add_field(name='Created by', value=creator)
                 .add_field(name='Used', value=f'{formats.pluralize(time=tag["uses"])}', inline=False)
                 .add_field(name='Rank', value=f'#{rank}', inline=False)
                 .set_footer(text='Created')
                 )

        if tag['is_alias']:
            embed.description = f'Original Tag: {tag["content"]}'

        await ctx.send(embed=embed)

    @tag.command(name='search')
    async def tag_search(self, ctx, *, name: commands.clean_content):
        """Searches and shows up to the 50 closest matches for a given name."""
        query = """SELECT   name
                   FROM     tags
                   WHERE    location_id=$1 AND name % $2
                   ORDER BY similarity(name, $2) DESC
                   LIMIT 5;
                """
        tags = [tag['name'] for tag in await ctx.db.fetch(query, ctx.guild.id, name)]
        entries = (
            itertools.starmap('{0}. {1}'.format, enumerate(tags, 1)) if tags else
            ['No results found... :(']
        )

        pages = ListPaginator(ctx, entries, title=f'Tags relating to {name}')
        await pages.interact()

    # XXX: too much repetition...
    @tag.command(name='list', aliases=['all'])
    async def tag_list(self, ctx):
        """Shows all the tags in the server."""
        query = 'SELECT name FROM tags WHERE location_id = $1 ORDER BY name'
        tags = [tag[0] for tag in await ctx.db.fetch(query, ctx.guild.id)]

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

        query = 'SELECT name FROM tags WHERE location_id = $1 AND owner_id = $2 ORDER BY name'
        tags = [tag[0] for tag in await ctx.db.fetch(query, ctx.guild.id, ctx.author.id)]

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
