from enum import IntEnum


class NbBetStatus(IntEnum):
    SCHEDULED = 1
    LIVE = 2
    FINISHED = 3
    MOVED = 4
    CANCELED = 5
    STOPPED = 6
    INTERRUPTED = 7
    WALKOVER = 8
    FINISHED_RETIRED = 9
    AWARDED = 10


# Статус уже не изменится: можно закрывать матч.
TERMINAL = frozenset({3, 5, 8, 9, 10})
# По этим матчам имеет смысл тянуть страницу и очки.
FETCH_DETAILS = frozenset({3, 9, 10})


def is_terminal(code: int | None) -> bool:
    return code in TERMINAL
