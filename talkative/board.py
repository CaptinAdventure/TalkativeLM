from functools import lru_cache

from board import Board, default_db_path


@lru_cache(maxsize=1)
def get_board():
    return Board(default_db_path())
