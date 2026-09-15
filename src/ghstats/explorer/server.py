#!/usr/bin/env python3
"""Serve the explorer over loopback HTTP.

    ghstats-explore                          # http://127.0.0.1:8765
    ghstats-explore --timezone Europe/Berlin --open

A routing shell and nothing more: every response body comes from `queries`, so
the interesting logic is testable without a socket. Three properties are
deliberate.

**Read-only, enforced by SQLite.** The store is opened through a `mode=ro` URI,
so a bug in a handler cannot write to the file that took hours of API budget to
fill. `ghstats-sync` can run against the same store while this is serving --
that is what WAL is for.

**Loopback only.** There is no authentication because there is no remote
listener: the default bind is 127.0.0.1. `--host` can widen that, and says so.
The `Host` header is still checked, because a page in the user's browser can
resolve an attacker-controlled name to 127.0.0.1 and would otherwise reach this
server with the browser's blessing.

**One connection per thread.** `sqlite3` connections are not safe to share
across threads, and `ThreadingHTTPServer` hands each request to its own. A
thread-local connection is simpler than a pool and correct for a tool serving
one person.
"""
import argparse
import json
import os
import sqlite3
import sys
import threading
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from ghstats.explorer import queries, sql
from ghstats.store import sqlite as sqlite_store

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8765

STATIC = Path(__file__).resolve().parent / 'static'

# Extensions the static handler will serve, mapped to their content type.
# A whitelist rather than `mimetypes.guess_type`: this directory holds a fixed
# set of assets, and anything outside it is a bug or an attempt.
CONTENT_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.ico': 'image/x-icon',
}


class NotFound(Exception):
    """No such entity. Rendered as 404 with a JSON body."""


class BadRequest(Exception):
    """The request could not be understood. Rendered as 400."""


# -- store access ----------------------------------------------------------

class Store:
    """Thread-local read-only connections to one store.

    `register_functions` runs per connection, not once: `local_date` and
    friends are registered on a connection, so a new thread's connection would
    otherwise fail on the first query that groups by day.
    """

    def __init__(self, path: str, org: str, tz_name: str):
        self.path = path
        self.org = org
        self.tz_name = tz_name
        self._local = threading.local()

    def conn(self) -> sqlite3.Connection:
        existing = getattr(self._local, 'conn', None)
        if existing is not None:
            return existing
        # `mode=ro` refuses writes at the driver, and `immutable=0` keeps WAL
        # readers seeing a concurrent sync's commits.
        uri = f'file:{Path(self.path).as_posix()}?mode=ro'
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        queries.register_functions(conn, self.tz_name)
        self._local.conn = conn
        return conn


def detect_org(path: str) -> Optional[str]:
    """Read the organization out of the store when only one is present.

    Saves passing `--org` to a tool that reads a store holding exactly one, and
    refuses to guess when it holds several.
    """
    conn = sqlite3.connect(f'file:{Path(path).as_posix()}?mode=ro', uri=True)
    try:
        rows = [r[0] for r in conn.execute(
            'SELECT DISTINCT org FROM repos ORDER BY org')]
    finally:
        conn.close()
    return rows[0] if len(rows) == 1 else None


# -- request parsing -------------------------------------------------------

def _first(params: Dict[str, List[str]], name: str) -> Optional[str]:
    values = params.get(name)
    if not values:
        return None
    value = values[0].strip()
    return value or None


def _flag(params: Dict[str, List[str]], name: str) -> bool:
    value = _first(params, name)
    return value is not None and value.lower() not in ('0', 'false', 'no')


def _int(params: Dict[str, List[str]], name: str, default: int) -> int:
    value = _first(params, name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        raise BadRequest(f'{name} must be a whole number, got {value!r}')


def _date(params: Dict[str, List[str]], name: str) -> Optional[str]:
    value = _first(params, name)
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise BadRequest(f'{name} must be YYYY-MM-DD, got {value!r}')


def filters_from(params: Dict[str, List[str]], tz_name: str) -> queries.Filters:
    """Build a `Filters` from a query string.

    Unknown `kinds` values are dropped by `Filters` rather than rejected, so a
    stale bookmark degrades to the default instead of erroring.
    """
    kinds_raw = _first(params, 'kinds')
    kinds = tuple(k.strip() for k in kinds_raw.split(',')) if kinds_raw \
        else queries.KINDS
    return queries.Filters(
        frm=_date(params, 'from'),
        to=_date(params, 'to'),
        user=_first(params, 'user'),
        repo=_first(params, 'repo'),
        team=_first(params, 'team'),
        project=_first(params, 'project'),
        issue=_first(params, 'issue'),
        kinds=kinds,
        q=_first(params, 'q'),
        ai=_first(params, 'ai'),
        bots=_flag(params, 'bots'),
        tz=_first(params, 'tz') or tz_name,
        limit=_int(params, 'limit', queries.PAGE),
        offset=_int(params, 'offset', 0),
    )


# -- routing ---------------------------------------------------------------

Route = Callable[['Handler', List[str], Dict[str, List[str]]], Any]


def route_api(store: Store, path: str, params: Dict[str, List[str]]) -> Any:
    """Dispatch an `/api/...` path to a query function.

    Kept as an if-ladder over path segments rather than a regex table: there
    are eleven endpoints, the shapes are all `collection` or
    `collection/<name>`, and a table would be harder to read than the thing it
    replaces.
    """
    conn = store.conn()
    org = store.org
    segments = [unquote(s) for s in path.strip('/').split('/') if s]
    if not segments or segments[0] != 'api':
        raise NotFound(path)
    rest = segments[1:]
    if not rest:
        raise NotFound(path)

    head, name = rest[0], (rest[1] if len(rest) > 1 else None)
    f = filters_from(params, store.tz_name)

    if head == 'meta' and name is None:
        return queries.meta(conn, org, store.tz_name)

    if head == 'search' and name is None:
        return queries.search(conn, org, _first(params, 'q') or '',
                              limit=_int(params, 'limit', 8))

    if head == 'events' and name is None:
        return queries.events(conn, org, f)

    if head == 'users':
        if name is None:
            return queries.user_overview(conn, org, f)
        return queries.user_detail(conn, org, name, f)

    if head == 'repos':
        if name is None:
            return queries.repo_overview(conn, org, f)
        bundle = queries.repo_detail(conn, org, name, f)
        if not bundle['repo']['known']:
            raise NotFound(f'no repository {name!r} in {org}')
        return bundle

    if head == 'teams':
        if name is None:
            return queries.team_list(conn)
        bundle = queries.team_detail(conn, org, name, f)
        if not bundle['team']['known']:
            raise NotFound(f'no team {name!r}')
        return bundle

    if head == 'projects':
        if name is None:
            return {'projects': queries.project_list(conn, org, f)}
        return queries.project_detail(conn, org, name, f)

    if head == 'issues' and name:
        return queries.issue_detail(conn, org, name, f)

    if head == 'days' and name:
        try:
            day = date.fromisoformat(name).isoformat()
        except ValueError:
            raise BadRequest(f'day must be YYYY-MM-DD, got {name!r}')
        return queries.day_detail(conn, org, day, f)

    raise NotFound(path)


def route_api_post(store: Store, path: str, params: Dict[str, List[str]],
                   body: Any) -> Any:
    """Dispatch a POST. The SQL page is the only thing that sends one.

    POST rather than GET because a query does not fit comfortably in a URL and
    should not land in a log line. The filters still travel in the query
    string, parsed exactly as the cards' are.
    """
    if path.rstrip('/') != '/api/sql':
        raise NotFound(path)
    if not isinstance(body, dict) or not isinstance(body.get('sql'), str):
        raise BadRequest('expected a JSON object with a "sql" string')
    try:
        return sql.run(store.path, body['sql'],
                       filters_from(params, store.tz_name))
    except sql.QueryError as exc:
        raise BadRequest(str(exc))


class Handler(BaseHTTPRequestHandler):
    """One request. `store` and `allow_hosts` are set on the class by `serve`."""

    store: Store
    allow_hosts: Tuple[str, ...] = ()
    server_version = 'ghstats-explore'
    protocol_version = 'HTTP/1.1'

    # -- plumbing ----------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Quieter than the default, and on stderr so stdout stays the URL.

        The parameter is named for the base class, not for taste.
        """
        if os.environ.get('GHSTATS_EXPLORE_DEBUG'):
            sys.stderr.write(f'{self.address_string()} {format % args}\n')

    def _host_ok(self) -> bool:
        """Reject a request whose Host is not one we are willing to answer for.

        Without this, a page the user visits can point a hostname it controls at
        127.0.0.1 and read this server's responses -- DNS rebinding. The store
        holds the organization's whole commit history, so the check is cheap
        insurance rather than ceremony.
        """
        if not self.allow_hosts:
            return True
        host = (self.headers.get('Host') or '').split(':')[0].lower()
        return host in self.allow_hosts

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        # Nothing here is cacheable: the store changes under the server every
        # time a sync runs.
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, default=str).encode('utf-8')
        self._send(status, body, 'application/json; charset=utf-8')

    # -- verbs -------------------------------------------------------------

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._host_ok():
            self._json(400, {'error': 'unexpected Host header'})
            return

        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query, keep_blank_values=False)

        try:
            if path.startswith('/api/'):
                self._json(200, route_api(self.store, path, params))
            else:
                self._static(path)
        except NotFound as exc:
            self._json(404, {'error': str(exc)})
        except BadRequest as exc:
            self._json(400, {'error': str(exc)})
        except sqlite3.Error as exc:
            self._json(500, {'error': f'store error: {exc}'})
        except BrokenPipeError:
            pass                          # the browser navigated away mid-render
        except Exception as exc:          # noqa: BLE001 - a handler must not die
            self._json(500, {'error': f'{type(exc).__name__}: {exc}'})

    def do_POST(self) -> None:
        """Run a query from the SQL page.

        **Only `application/json`.** A page on any other site can make the
        browser POST `text/plain` or a form to 127.0.0.1 without asking first,
        and the Host check cannot tell -- the Host really is 127.0.0.1. A JSON
        body needs a CORS preflight, and nothing here answers one, so the
        browser never sends it. The runner is read-only regardless; this keeps
        a stranger's page from spending this machine's CPU on the store.
        """
        # Error paths below do not read the body, so the connection cannot be
        # reused for another request.
        self.close_connection = True
        if not self._host_ok():
            self._json(400, {'error': 'unexpected Host header'})
            return
        content_type = (self.headers.get('Content-Type') or '').split(';')[0]
        if content_type.strip().lower() != 'application/json':
            self._json(415, {'error': 'expected Content-Type: application/json'})
            return
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = -1
        if length < 0:
            self._json(400, {'error': 'bad Content-Length'})
            return
        if length > sql.MAX_TEXT:
            self._json(413, {'error': f'body is larger than {sql.MAX_TEXT} bytes'})
            return

        parsed = urlparse(self.path)
        params = parse_qs(parsed.query, keep_blank_values=False)
        try:
            try:
                body = json.loads(self.rfile.read(length) or b'null')
            except ValueError:
                raise BadRequest('body is not valid JSON')
            self._json(200, route_api_post(self.store, parsed.path, params, body))
        except NotFound as exc:
            self._json(404, {'error': str(exc)})
        except BadRequest as exc:
            self._json(400, {'error': str(exc)})
        except sqlite3.Error as exc:
            self._json(500, {'error': f'store error: {exc}'})
        except BrokenPipeError:
            pass
        except Exception as exc:          # noqa: BLE001 - a handler must not die
            self._json(500, {'error': f'{type(exc).__name__}: {exc}'})

    def _static(self, path: str) -> None:
        """Serve one file from `static/`, refusing anything outside it."""
        relative = 'index.html' if path in ('/', '') else path.lstrip('/')
        target = (STATIC / relative).resolve()
        # `resolve()` then containment check: the belt-and-braces against
        # `..` traversal, which no browser sends but a curl does.
        if not str(target).startswith(str(STATIC.resolve())) \
                or not target.is_file():
            raise NotFound(path)
        content_type = CONTENT_TYPES.get(target.suffix)
        if content_type is None:
            raise NotFound(path)
        self._send(200, target.read_bytes(), content_type)


def serve(db: str, org: str, tz_name: str, host: str, port: int,
          open_browser: bool = False) -> int:
    """Run until interrupted. Returns a process exit code."""
    store = Store(db, org, tz_name)
    try:
        conn = store.conn()
        version = conn.execute('PRAGMA user_version').fetchone()[0]
    except sqlite3.Error as exc:
        print(f'Error: could not open {db}: {exc}', file=sys.stderr)
        return 2

    if version != sqlite_store.SCHEMA_VERSION:
        # Schema 0 means there is no store here -- a first run, or the wrong
        # `--db`. Anything else is a real store this code has yet to migrate,
        # and only a writer can migrate it. `ghstats-reindex` opens the store
        # read-only-ish (`create=False`), so naming it here would send a new
        # user in a circle.
        if version == 0:
            print(f'Error: {db} holds no ghstats store yet.\n'
                  f'       Run `ghstats-sync --org <org> --from <YYYY-MM-DD>` '
                  f'first, or point --db at an existing store.',
                  file=sys.stderr)
        else:
            print(f'Error: {db} is at schema {version}, this code expects '
                  f'{sqlite_store.SCHEMA_VERSION}.\n'
                  f'       Run `ghstats-sync` to migrate it in place.',
                  file=sys.stderr)
        return 2

    missing = _missing_derived(conn)
    if missing:
        print(f'Note: {", ".join(missing)} is empty; run `ghstats-reindex` to '
              f'populate it.', file=sys.stderr)

    Handler.store = store
    # Only enforce the Host check when bound to loopback. A deliberate `--host
    # 0.0.0.0` means the user wants other names to reach it.
    Handler.allow_hosts = ('localhost', '127.0.0.1', '::1', '[::1]') \
        if host in ('127.0.0.1', 'localhost', '::1') else ()

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    url = f'http://{host}:{port}/'

    print(f'ghstats-explore  {org}  {db}')
    print(f'  timezone {tz_name}, schema {version}, read-only')
    if host not in ('127.0.0.1', 'localhost'):
        print(f'  WARNING: bound to {host}, reachable beyond this machine')
    print(f'\n  {url}\n')
    print('Ctrl-C to stop.')

    if open_browser:
        threading.Thread(target=lambda: webbrowser.open(url),
                         daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nstopped')
    finally:
        httpd.server_close()
    return 0


def _missing_derived(conn: sqlite3.Connection) -> List[str]:
    """Derived tables the explorer leans on that no reindex has filled yet.

    Reported rather than fixed: this process holds a read-only handle by design,
    and an explorer that silently served zero AI attribution and no issue links
    would look like an organization that uses neither.
    """
    names = []
    for table in ('issue_refs', 'bot_logins', 'commit_trailers'):
        if conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0:
            names.append(table)
    return names


def parse_arguments(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog='ghstats-explore',
        description='Serve an interactive explorer over the activity store.')
    parser.add_argument('--db', default=sqlite_store.DEFAULT_DB,
                        help=f'Store path (default: {sqlite_store.DEFAULT_DB})')
    parser.add_argument('--org',
                        help='Organization; inferred when the store holds one')
    parser.add_argument('--timezone', default='UTC',
                        help='Zone that day and hour grouping use (default UTC)')
    parser.add_argument('--host', default=DEFAULT_HOST,
                        help=f'Bind address (default: {DEFAULT_HOST})')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                        help=f'Bind port (default: {DEFAULT_PORT})')
    parser.add_argument('--open', action='store_true',
                        help='Open a browser once the server is up')
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point."""
    args = parse_arguments(argv)

    if not Path(args.db).exists():
        print(f'Error: {args.db} does not exist. Run `ghstats-sync` first.',
              file=sys.stderr)
        return 2

    org = args.org or detect_org(args.db)
    if not org:
        print('Error: the store holds more than one organization; '
              'pass --org.', file=sys.stderr)
        return 2

    try:
        return serve(args.db, org, args.timezone, args.host, args.port,
                     open_browser=args.open)
    except OSError as exc:
        print(f'Error: could not bind {args.host}:{args.port}: {exc}',
              file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
