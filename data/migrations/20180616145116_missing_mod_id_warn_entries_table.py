"""Created on 2018-06-16 14:51:16.075104 UTC

Add missing mod_id column in warn_entries table
"""


# Postgres 9.5 doesn't have ADD COLUMN IF NOT EXISTS...
upgrade_warn_entries = """
DO $$ 
    BEGIN
        ALTER TABLE warn_entries ADD COLUMN mod_id BIGINT;
    EXCEPTION
        WHEN duplicate_column THEN RAISE NOTICE 'column mod_id already exists in <table_name>.';
    END
$$;
"""