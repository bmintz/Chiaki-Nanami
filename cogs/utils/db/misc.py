import json

import asyncpg

__all__ = ['create_pool']

async def _set_codec(conn):
    await conn.set_type_codec(
        'jsonb',
        schema='pg_catalog',
        encoder=json.dumps,
        decoder=json.loads,
        format='text'
    )


async def create_pool(dsn, *, init=None, **kwargs):
    if init is None:
        async def new_init(conn):
            await _set_codec(conn)
    else:
        async def new_init(conn):
            await _set_codec(conn)
            await init(conn)

    return await asyncpg.create_pool(dsn, init=new_init, **kwargs)
