"""Run a person's SQL against the store, safely, for the explorer's SQL page.

No HTTP here, for the same reason `queries` has none: the guards are the part
worth testing, and they are testable without a socket.

**Read-only three times over.** The connection is opened `mode=ro`, so SQLite
refuses writes at the driver. An authorizer then allows only reading --
`SELECT`, table reads, functions, and the handful of schema pragmas a person
exploring a store actually wants -- which also shuts the doors `mode=ro` leaves
open: `ATTACH` of another file, `CREATE TEMP`, and pragmas that reconfigure the
connection. And `sqlite3` itself refuses a second statement after the first.

**A fresh connection per run.** Cheap for SQLite, and it means the timezone the
filter bar asked for can be registered for `local_date` without disturbing the
thread-local connections the cards share, and that a progress handler or
authorizer can never leak onto them.

**Bounded.** A progress handler stops a query after `TIMEOUT_SECONDS`, and at
most `MAX_ROWS` rows are shipped, with `truncated` saying so. A recursive CTE
without a stop condition is one typo away, and it should cost five seconds,
not a hung server thread.

**Parameters are the filter bar.** `:from` and `:to` arrive already converted
to the half-open UTC range the cards filter on, so a query that says
`created_at >= :from AND created_at < :to` selects exactly what a card counted.
"""
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ghstats.explorer import queries

MAX_ROWS = 5000
TIMEOUT_SECONDS = 5.0

# Longest SQL text accepted. Far beyond any query a person writes by hand; the
# bound is there so a request body cannot be arbitrarily large.
MAX_TEXT = 64 * 1024

# Named parameters every query may use, in the order the page lists them.
PARAMETERS = ('from', 'to', 'tz', 'repo', 'user', 'bots')

# Virtual-machine steps between progress-handler calls. Small enough that the
# timeout is honoured to within milliseconds, large enough to cost nothing.
_PROGRESS_STEPS = 1000

# Pragmas that only describe the schema. Everything else a pragma can do --
# `query_only = 0`, `journal_mode`, `user_version = 5` -- reconfigures the
# connection or the file, so the authorizer allows only these.
_SCHEMA_PRAGMAS = frozenset({
    'table_info', 'table_xinfo', 'table_list', 'index_list', 'index_info',
    'index_xinfo', 'foreign_key_list', 'function_list', 'collation_list',
})

_READING = frozenset({sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                      sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE})


class QueryError(Exception):
    """The query could not run. The message is meant for the person who wrote
    it, so it says what to change rather than where in this module it failed."""


class _Median:
    """`median(x)`: the middle value, or the mean of the middle pair.

    The same definition as `queries._median`, so a recipe that says
    `median(lines)` reproduces the card's number instead of approximating it.
    NULLs are skipped and an empty set is NULL, as for every SQL aggregate --
    the card's zero-for-nothing is a display choice a recipe can `COALESCE`.
    """

    def __init__(self):
        self.values: List[float] = []

    def step(self, value: Any) -> None:
        if value is not None:
            self.values.append(value)

    def finalize(self) -> Optional[float]:
        return queries._median(self.values) if self.values else None


def parameters(f: queries.Filters) -> Dict[str, Any]:
    """The values `:from`, `:to` and friends bind to for these filters."""
    start, end = f.window()
    return {'from': start, 'to': end, 'tz': f.tz, 'repo': f.repo,
            'user': f.user, 'bots': 1 if f.bots else 0}


def _authorize(action: int, arg1: Optional[str], arg2: Optional[str],
               database: Optional[str], trigger: Optional[str]) -> int:
    if action in _READING:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and arg1 in _SCHEMA_PRAGMAS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def open_connection(path: str, tz_name: str) -> sqlite3.Connection:
    """A read-only, reading-only connection with the explorer's functions."""
    uri = f'file:{Path(path).as_posix()}?mode=ro'
    conn = sqlite3.connect(uri, uri=True)
    queries.register_functions(conn, tz_name)
    conn.create_aggregate('median', 1, _Median)
    # Last, so registering the functions is not itself subject to it.
    conn.set_authorizer(_authorize)
    return conn


def _cell(value: Any) -> Any:
    """A value JSON can carry. Blobs travel as hex rather than a Python repr."""
    return value.hex() if isinstance(value, bytes) else value


def run(path: str, text: str, f: queries.Filters, *,
        max_rows: int = MAX_ROWS, timeout: float = TIMEOUT_SECONDS
        ) -> Dict[str, Any]:
    """Run one read-only statement and return its columns and rows.

    Raises:
        QueryError: for anything the person can fix -- a syntax error, a
            refused write, an unknown parameter, a query that ran too long.
    """
    if not text or not text.strip():
        raise QueryError('nothing to run')
    if len(text) > MAX_TEXT:
        raise QueryError(f'query is longer than {MAX_TEXT} characters')

    bound = parameters(f)
    conn = open_connection(path, f.tz)
    started = time.monotonic()
    deadline = started + timeout
    conn.set_progress_handler(lambda: time.monotonic() > deadline,
                              _PROGRESS_STEPS)
    try:
        cursor = conn.execute(text, bound)
        rows = cursor.fetchmany(max_rows + 1)
        columns = [d[0] for d in cursor.description or ()]
    except sqlite3.Error as exc:
        raise QueryError(_explain(exc, timeout)) from None
    finally:
        conn.close()

    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    return {
        'columns': columns,
        'rows': [[_cell(v) for v in row] for row in rows],
        'row_count': len(rows),
        'truncated': truncated,
        'elapsed_ms': round((time.monotonic() - started) * 1000),
        'parameters': bound,
    }


def _explain(exc: sqlite3.Error, timeout: float) -> str:
    """Turn a driver error into a sentence about the query."""
    message = str(exc)
    available = ', '.join(f':{name}' for name in PARAMETERS)
    if message == 'interrupted':
        return (f'stopped after {timeout:g} s -- narrow the window or add a '
                f'LIMIT')
    if message.startswith('not authorized'):
        return ('only reading is allowed here: SELECT and WITH, plus schema '
                'pragmas such as pragma_table_info')
    if message.startswith('You can only execute one statement'):
        return 'one statement at a time'
    if message.startswith('You did not supply a value for binding parameter'):
        name = message.rsplit(' ', 1)[-1].rstrip('.')
        return f'unknown parameter {name}; available: {available}'
    if 'has no name' in message:
        return f'use named parameters rather than ?: {available}'
    return message
