"""Created on 2018-06-04 10:35:05.205458 UTC

Represent board and clues with text

This is here because my DB thingy doesn't support arrays yet. And
strings are slightly easier to reason about.
"""


from itertools import chain, count, takewhile

_flatten = chain.from_iterable

async def upgrade_saved_sudoku_games(connection):
    games = await connection.fetch('SELECT user_id, board, clues from saved_sudoku_games')
    new_games = [
        (
            user_id,
            ''.join(map(str, _flatten(board))),
            ''.join(map(str, _flatten(divmod(c, 9) for c in clues)))
        )
        for user_id, board, clues in games
    ]

    await connection.execute(
        'TRUNCATE TABLE saved_sudoku_games;\n'
        'ALTER TABLE saved_sudoku_games ALTER COLUMN board TYPE TEXT,\n'
        'ALTER COLUMN clues TYPE TEXT;'
    )

    columns = ('user_id', 'board', 'clues')
    await connection.copy_records_to_table('saved_sudoku_games', records=new_games, columns=columns)


def _sliced(seq, n):
    return takewhile(bool, (seq[i: i + n] for i in count(0, n)))

async def downgrade_saved_sudoku_games(connection):
    games = await connection.fetch('SELECT user_id, board, clues from saved_sudoku_games')
    new_games = [
        (
            user_id,
            [list(map(int, slice)) for slice in _sliced(board, 9)],
            [(int(x), int(y)) for x, y in _sliced(clues, 2)],
        )
        for user_id, board, clues in games
    ]

    await connection.execute(
        'TRUNCATE TABLE saved_sudoku_games;\n'
        'ALTER TABLE saved_sudoku_games ALTER COLUMN board TYPE SMALLINT[9][9],\n'
        'ALTER COLUMN clues TYPE SMALLINT[];'
    )

    columns = ('user_id', 'board', 'clues')
    await connection.copy_records_to_table('saved_sudoku_games', records=new_games, columns=columns)
