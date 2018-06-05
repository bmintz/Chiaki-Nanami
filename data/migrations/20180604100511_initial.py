"""Created on 2018-06-04 10:05:11.153917 UTC

Initial migration.

This is only to mark the beginning for the migration scripts.
"""


__initial__ = True


upgrade_schedule = """
CREATE TABLE IF NOT EXISTS schedule (
id SERIAL PRIMARY KEY NOT NULL,
expires TIMESTAMP NOT NULL,
event TEXT NOT NULL,
time TIMESTAMP DEFAULT (now() at time zone 'utc') NOT NULL,
args_kwargs JSON DEFAULT ('{}'::jsonb) NOT NULL
);
CREATE INDEX IF NOT EXISTS schedule_expires_idx ON schedule (expires);
"""

downgrade_schedule = 'DROP TABLE schedule'


upgrade_commands = """
CREATE TABLE IF NOT EXISTS commands (
id BIGSERIAL PRIMARY KEY NOT NULL,
guild_id BIGINT NULL,
channel_id BIGINT NOT NULL,
author_id BIGINT NOT NULL,
used TIMESTAMP NOT NULL,
prefix TEXT NOT NULL,
command TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS commands_author_id_idx ON commands (author_id);
CREATE INDEX IF NOT EXISTS commands_command_idx ON commands (command);
CREATE INDEX IF NOT EXISTS commands_guild_id_idx ON commands (guild_id);
"""

downgrade_commands = 'DROP TABLE commands'


upgrade_server_messages = """
CREATE TABLE IF NOT EXISTS server_messages (
guild_id BIGINT NOT NULL,
is_welcome BOOLEAN NOT NULL,
channel_id BIGINT NOT NULL,
message TEXT NULL,
delete_after SMALLINT DEFAULT (0) NOT NULL,
enabled BOOLEAN DEFAULT FALSE NOT NULL
);
"""

downgrade_server_messages = 'DROP TABLE server_messages'


upgrade_permissions = """
CREATE TABLE IF NOT EXISTS permissions (
id SERIAL PRIMARY KEY NOT NULL,
guild_id BIGINT NOT NULL,
snowflake BIGINT NULL,
name TEXT NOT NULL,
whitelist BOOLEAN NOT NULL
);
CREATE INDEX IF NOT EXISTS permissions_guild_id_idx ON permissions (guild_id);
"""

downgrade_permissions = 'DROP TABLE permissions'


upgrade_plonks = """
CREATE TABLE IF NOT EXISTS plonks (
guild_id BIGINT NOT NULL,
entity_id BIGINT NOT NULL,
PRIMARY KEY(guild_id, entity_id)
);
CREATE INDEX IF NOT EXISTS plonks_idx ON plonks (guild_id, entity_id);
"""

downgrade_plonks = 'DROP TABLE plonks'


upgrade_command_aliases = """
CREATE TABLE IF NOT EXISTS command_aliases (
id SERIAL PRIMARY KEY NOT NULL,
guild_id BIGINT NOT NULL,
alias TEXT NOT NULL,
command TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS command_aliases_uniq_idx ON command_aliases (guild_id, alias);
"""

downgrade_command_aliases = 'DROP TABLE command_aliases'


upgrade_currency = """
CREATE TABLE IF NOT EXISTS currency (
user_id BIGINT PRIMARY KEY NOT NULL,
amount INTEGER NOT NULL
);
"""

downgrade_currency = 'DROP TABLE currency'


upgrade_givelog = """
CREATE TABLE IF NOT EXISTS givelog (
id SERIAL PRIMARY KEY NOT NULL,
giver BIGINT NOT NULL,
recipient BIGINT NOT NULL,
amount INTEGER NOT NULL,
time TIMESTAMP DEFAULT (now() at time zone 'utc') NOT NULL
);
"""

downgrade_givelog = 'DROP TABLE givelog'


upgrade_daily_cash_cooldowns = """
CREATE TABLE IF NOT EXISTS daily_cash_cooldowns (
user_id BIGINT PRIMARY KEY NOT NULL,
latest_time TIMESTAMP NOT NULL
);
"""

downgrade_daily_cash_cooldowns = 'DROP TABLE daily_cash_cooldowns'


upgrade_dailylog = """
CREATE TABLE IF NOT EXISTS dailylog (
id SERIAL PRIMARY KEY NOT NULL,
user_id BIGINT NOT NULL,
time TIMESTAMP NOT NULL,
amount INTEGER NOT NULL
);
"""

downgrade_dailylog = 'DROP TABLE dailylog'


upgrade_rigged_ships = """
CREATE TABLE IF NOT EXISTS rigged_ships (
id SERIAL PRIMARY KEY NOT NULL,
user_id BIGINT NOT NULL,
partner_id BIGINT NOT NULL,
score SMALLINT NOT NULL,
comment TEXT NULL,
guild_id BIGINT NOT NULL,
rigger_id BIGINT NOT NULL,
CHECK (user_id <= partner_id),
UNIQUE (guild_id, user_id, partner_id)
);
"""

downgrade_rigged_ships = 'DROP TABLE rigged_ships'


upgrade_hilo_games = """
CREATE TABLE IF NOT EXISTS hilo_games (
id SERIAL PRIMARY KEY NOT NULL,
guild_id BIGINT NOT NULL,
player_id BIGINT NOT NULL,
played_at TIMESTAMP NOT NULL,
points INTEGER NOT NULL
);
"""

downgrade_hilo_games = 'DROP TABLE hilo_games'


upgrade_racehorses = """
CREATE TABLE IF NOT EXISTS racehorses (
user_id BIGINT PRIMARY KEY NOT NULL,
emoji TEXT NOT NULL
);
"""

downgrade_racehorses = 'DROP TABLE racehorses'


upgrade_minesweeper_games = """
CREATE TABLE IF NOT EXISTS minesweeper_games (
id SERIAL PRIMARY KEY NOT NULL,
level SMALLINT NOT NULL,
won BOOLEAN NOT NULL,
guild_id BIGINT NOT NULL,
user_id BIGINT NOT NULL,
played_at TIMESTAMP NOT NULL,
time REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS minesweeper_games_time_idx ON minesweeper_games (time);
"""

downgrade_minesweeper_games = 'DROP TABLE minesweeper_games'


upgrade_saved_sudoku_games = """
CREATE TABLE IF NOT EXISTS saved_sudoku_games (
user_id BIGINT PRIMARY KEY,
board SMALLINT[9][9] NOT NULL,
clues SMALLINT[] NOT NULL
);
"""

downgrade_saved_sudoku_games = 'DROP TABLE saved_sudoku_games'


upgrade_warn_entries = """
CREATE TABLE IF NOT EXISTS warn_entries (
id SERIAL PRIMARY KEY NOT NULL,
guild_id BIGINT NOT NULL,
user_id BIGINT NOT NULL,
reason TEXT NOT NULL,
warned_at TIMESTAMP NOT NULL
);
"""

downgrade_warn_entries = 'DROP TABLE warn_entries'


upgrade_warn_timeouts = """
CREATE TABLE IF NOT EXISTS warn_timeouts (
guild_id BIGINT PRIMARY KEY NOT NULL,
timeout INTERVAL NOT NULL
);
"""

downgrade_warn_timeouts = 'DROP TABLE warn_timeouts'


upgrade_warn_punishments = """
CREATE TABLE IF NOT EXISTS warn_punishments (
guild_id BIGINT NOT NULL,
warns BIGINT NOT NULL,
text TEXT NOT NULL,
duration INTEGER DEFAULT (0) NOT NULL,
PRIMARY KEY(guild_id, warns)
);
"""

downgrade_warn_punishments = 'DROP TABLE warn_punishments'


upgrade_muted_roles = """
CREATE TABLE IF NOT EXISTS muted_roles (
guild_id BIGINT PRIMARY KEY NOT NULL,
role_id BIGINT NOT NULL
);
"""

downgrade_muted_roles = 'DROP TABLE muted_roles'


upgrade_modlog = """
CREATE TABLE IF NOT EXISTS modlog (
id SERIAL PRIMARY KEY NOT NULL,
channel_id BIGINT NOT NULL,
message_id BIGINT NOT NULL,
guild_id BIGINT NOT NULL,
action VARCHAR(16) NOT NULL,
mod_id BIGINT NOT NULL,
reason TEXT NOT NULL,
extra TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS modlog_guild_id_idx ON modlog (guild_id);
"""

downgrade_modlog = 'DROP TABLE modlog'


upgrade_modlog_targets = """
CREATE TABLE IF NOT EXISTS modlog_targets (
id SERIAL PRIMARY KEY NOT NULL,
entry_id INTEGER REFERENCES modlog (id) ON DELETE CASCADE ON UPDATE NO ACTION,
mod_id BIGINT NOT NULL
);
"""

downgrade_modlog_targets = 'DROP TABLE modlog_targets'


upgrade_modlog_config = """
CREATE TABLE IF NOT EXISTS modlog_config (
guild_id BIGINT PRIMARY KEY NOT NULL,
channel_id BIGINT DEFAULT (0) NOT NULL,
enabled BOOLEAN DEFAULT TRUE NOT NULL,
log_auto BOOLEAN DEFAULT TRUE NOT NULL,
dm_user BOOLEAN DEFAULT TRUE NOT NULL,
poll_audit_log BOOLEAN DEFAULT TRUE NOT NULL,
events INTEGER DEFAULT (383) NOT NULL
);
"""

downgrade_modlog_config = 'DROP TABLE modlog_config'


upgrade_blacklist = """
CREATE TABLE IF NOT EXISTS blacklist (
snowflake BIGINT PRIMARY KEY NOT NULL,
blacklisted_at TIMESTAMP NOT NULL,
reason TEXT NULL
);
"""

downgrade_blacklist = 'DROP TABLE blacklist'


upgrade_selfroles = """
CREATE TABLE IF NOT EXISTS selfroles (
id SERIAL PRIMARY KEY NOT NULL,
guild_id BIGINT NOT NULL,
role_id BIGINT UNIQUE NOT NULL
);
"""

downgrade_selfroles = 'DROP TABLE selfroles'


upgrade_autoroles = """
CREATE TABLE IF NOT EXISTS autoroles (
guild_id BIGINT PRIMARY KEY NOT NULL,
role_id BIGINT NOT NULL
);
"""

downgrade_autoroles = 'DROP TABLE autoroles'


upgrade_tags = """
CREATE TABLE IF NOT EXISTS tags (
name TEXT NOT NULL,
content TEXT NOT NULL,
is_alias BOOLEAN NOT NULL,
guild_id BIGINT NOT NULL,
uses INTEGER NOT NULL,
location_id BIGINT NOT NULL,
created_at TIMESTAMP NOT NULL,
PRIMARY KEY(name, location_id)
);
CREATE INDEX IF NOT EXISTS tags_uniq_idx ON tags (LOWER(name), location_id);
"""

downgrade_tags = 'DROP TABLE tags'
