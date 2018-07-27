"""Created on 2018-07-27 10:32:37.424361 UTC

Add default to tags.created_at

It turns out that I forgot to put a default on this for a while.
Not sure how that happened...
"""


upgrade_tags = "ALTER TABLE tags ALTER COLUMN created_at SET DEFAULT (now() at time zone 'utc')"
