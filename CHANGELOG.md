## 1.4.0 - Unreleased
### Added
- Draws are now possible in chess by either threefold repetition, insufficient 
  material, and the 50-move rule.
- You can now use `->info channel` on categories.
- Bad arguments show better error messages.
- `->changelog` - See what changed in the new versions of Chiaki.
- `->shards` - See how many shards Chiaki has, and what shard your server is on.

### Changed
- Commands are now case-insensitive.
- Quotes are no longer necessary for `->info channel` on channels with more than
  one word.

### Removed
- Removed `->commits` as it wasn't really useful for the end user.


## 1.3.1 - 2018-07-18
### Fixed
- Fix Chiaki freezing after starting multiple `->hilo` games.
- Fix the correct answer showing as a list in `->trivia diepio`.
- Fix a typo in the deletion message of `->cleanup`.
- Fix a missing f-string in `->tags` causing the prefix to show as "{ctx.prefix}"
  when there are no tags in the server.


## 1.3.0 - 2018-06-02
### Added
- `->reactiontest` - Test how good your reaction times are with this command!
- `->dots-boxes` - Connect the dots and see who can get the most boxes.
- `->checkers` - Hop, jump and king your way to victory!
- `->prefix set` - Is `->` clashing with your beloved Mantaro? Here's an easier
  way to change the prefix!
- `->chess` - That's right, the classic game of Chess has come to Chiaki!
- *Rocketeer* has been added to the list of possible diep.io tanks.
- `->info server` now shows if a server is partnered or verified.
- Reaction-less sessions and paginators are now a thing! If you don't want
  Chiaki to add reactions, you now have a fallback!

### Changed
- `->enable`, `->disable`, and `->undo` are now able to disable commands or
  categories (eg `->enable 8ball` is now possible).
- Cleaned up `->about`.
- Cleaned up `->info server`.
- `->trivia diepio` and `->trivia pokemon` now require exact spelling. This is
  because people would create shortened spellings and weird typos would pass as
  being "ok".

### Fixed
- `->unban` now has it's own example reasons, rather than the default reasons
  used in other punishments.
- `->warnpunish` now creates correct examples. Before it used to generate
  examples such as `->warnpunish 70 ban 15minutes`. Now duration is only added
  for mute and tempban, and the number of warns is lowered to 3-5.
