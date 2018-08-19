"""Created on 2018-07-27 09:49:43.190081 UTC

I have no idea how this happened, nor do I know how I managed
to change two columns and not know it until I recreated the database
from scratch.
"""


# Postgres doesn't have RENAME COLUMN IF EXISTS. Even though this is expensive
# it's the most reliable I can think of without relying on schema.
_rename_column = """
DO $$
    BEGIN
        ALTER TABLE {table} RENAME COLUMN {old_column} TO {new_column};
    EXCEPTION
        WHEN undefined_column THEN RAISE NOTICE 'column {old_column} was already renamed.';
    END
$$
"""

upgrade_schedule = _rename_column.format(table='schedule', old_column='time', new_column='created')
upgrade_tags = _rename_column.format(table='tags', old_column='guild_id', new_column='new_column')
upgrade_tags = f'ALTER TABLE tags ALTER COLUMN uses SET DEFAULT 0;\n{upgrade_tags}'
