import inspect
import itertools
import json
import operator
import pathlib
from datetime import datetime

from cogs.utils.db import all_tables

_DEFAULT_DIR = pathlib.Path('data', 'migrations')
_REVISIONS_FILE_NAME = '.revisions'

def _format_timestamp(timestamp):
    # strftime isn't guaranteed to pad datetime.min with zeros.
    # This breaks strptime which requires zero-padded years and can also mess
    # up comparisons.
    return timestamp.strftime('%Y%m%d%H%M%S').zfill(14)

_MIN_TIMESTAMP = _format_timestamp(datetime.min)
_MAX_TIMESTAMP = _format_timestamp(datetime.max)


def _file_version(name):
    # This works for our case lol
    return name[:14]

def _get_revisions(directory):
    directory = pathlib.Path(directory)
    file = directory / _REVISIONS_FILE_NAME

    try:
        return json.loads(file.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}

def _write_revisions(revisions, directory):
    directory = pathlib.Path(directory)
    file = directory / _REVISIONS_FILE_NAME

    with file.open('w', encoding='utf-8') as f:
        json.dump(revisions, f, indent=4)


def _get_migrations(directory=_DEFAULT_DIR, *, downgrade=False):
    if downgrade:
        action, cmp = 'downgrade', operator.le
    else:
        action, cmp = 'upgrade', operator.gt

    revisions = _get_revisions(directory)
    revisions = {t.__tablename__: revisions.get(t.__tablename__, _MIN_TIMESTAMP) for t in all_tables()}

    for script in sorted(directory.glob('*.py'), reverse=downgrade):
        version = _file_version(script.stem)

        namespace = {'__name__': f'migration_{script.stem}'}
        to_compile = compile(script.read_text(), script.name, 'exec')
        exec(to_compile, namespace)

        for name, value in namespace.items():
            action_, _, table = name.partition('_')
            if action_ == action and table in revisions and cmp(version, revisions[table]):
                yield version, table, script.stem, value


async def _apply_migration(to_execute, *, connection):
    if callable(to_execute):
        await to_execute(connection)
    else:
        await connection.execute(to_execute)

def _get_source(obj):
    try:
        return inspect.getsource(obj)
    except:
        # whatevs
        return obj


async def migrate(version=None, *, connection, downgrade=False, directory=_DEFAULT_DIR, verbose=False):
    if version is None:
        version = _MIN_TIMESTAMP if downgrade else _MAX_TIMESTAMP
    elif isinstance(version, datetime):
        version = _format_timestamp(version)

    if downgrade:
        cmp = operator.ge
    else:
        cmp = operator.le

    revisions = _get_revisions(directory)

    table_key = operator.itemgetter(1)
    migrations = _get_migrations(directory, downgrade=downgrade)
    table_migrations = sorted(
        itertools.takewhile(lambda p: cmp(p[0], version), migrations),
        key=table_key
    )

    async with connection.transaction():
        for table_name, migrations in itertools.groupby(table_migrations, table_key):
            last_version = None
            if downgrade:
                # The last script isn't supposed to be executed in a
                # downgrade.
                *migrations, last_version = migrations
                last_version = last_version[0]
            for version, table, file, step in migrations:
                if verbose:
                    print('applying', table, 'from', file, ':')
                    print(_get_source(step))

                try:
                    await _apply_migration(step, connection=connection)
                except:
                    action = 'upgrade' if downgrade else 'downgrade'
                    print('Error from', f'{action}_{table}', 'in', file)
                    raise

            if last_version is not None:
                version = last_version

            revisions[table_name] = version

        _write_revisions(revisions, directory)

def _last_migration(directory):
    return max(_file_version(path.stem) for path in directory.glob('*.py'))

async def init(*, connection, directory=_DEFAULT_DIR, verbose=False):
    # We can safely use the latest migration because when we initially create
    # all the tables we use the current schema which has the migrations already
    # applied.
    revision = _last_migration(directory)
    if verbose:
        print('writing newest revision', revision, 'to each table')

    directory = pathlib.Path(directory)
    file = directory / _REVISIONS_FILE_NAME
    if file.exists():
        raise RuntimeError('cannot initialize the database more than once')

    revisions = {}
    async with connection.transaction():
        for table in all_tables():
            print('creating table', table.__tablename__)
            sql = table.create_sql()
            if verbose:
                print('schema:')
                print(sql)

            await connection.execute(sql)
            revisions[table.__tablename__] = revision

        _write_revisions(revisions, directory)
