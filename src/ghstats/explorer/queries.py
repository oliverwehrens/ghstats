"""Slices of the activity store, as plain functions over a connection.

No HTTP here. `server` is a routing shell over this module, which means every
view the explorer offers can be exercised from a test or a REPL without a
socket -- and that the SQL is reviewable in one place instead of scattered
through request handlers.

**The event stream is the spine.** Every entry point (user, repository, team,
issue, day) is a set of aggregates plus a link into `events`, which is one
filterable query over commits, pull requests, merges and reviews. Adding an
entry point therefore means adding a summary and a filter, never a new way to
list activity. That is deliberate: the drill-down is where the actual answer
lives, and there should be exactly one implementation of it.

**Local dates, UTC storage.** The store holds UTC at whole-second precision.
"What changed on Tuesday" is a question about a local day, so a window arrives
as local dates and is converted to a UTC half-open instant range *before* it
reaches SQL (`window_utc`). Filtering on an expression over `committed_date`
would work and would also throw away the indexes on it; converting the
boundary keeps the comparison indexable. Grouping for histograms uses the
registered `local_date` / `local_hour` functions, which are correct across DST
because they defer to `zoneinfo` per row rather than adding a fixed offset.
"""
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:                                     # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:                      # pragma: no cover - 3.8 fallback
    ZoneInfo = None                      # type: ignore

from ghstats.store.sqlite import UNSYNCABLE_REPOS

# The four things that can appear in the stream. A pull request contributes two
# events, not one: it was opened on one day and merged on another, and "what
# changed on Tuesday" usually means the merge while "what did she work on"
# usually means the opening. Collapsing them onto `created_at` answers the
# first question wrongly and silently.
KINDS = ('commit', 'pull', 'merge', 'review')

DEFAULT_TZ = 'UTC'

# A page of events. Large enough that a quiet week arrives whole, small enough
# that a busy repository's year does not.
PAGE = 100


# -- timezone plumbing -----------------------------------------------------

def _zone(name: str):
    """Resolve a timezone name, falling back to UTC rather than failing.

    A bad `--timezone` should not make the explorer refuse to start; it should
    render UTC and be visibly wrong in the header, which is recoverable.
    """
    if not name or ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def register_functions(conn: sqlite3.Connection, tz_name: str = DEFAULT_TZ) -> None:
    """Install `local_date` and `local_hour` on the connection.

    Grouping by local day cannot be done by adding a fixed offset: the window
    this store covers spans four DST transitions, so an offset that is right in
    January is an hour wrong in July, and activity lands on the wrong day
    either side of 23:00. Deferring to `zoneinfo` per row costs about a
    microsecond per call and is simply correct.
    """
    zone = _zone(tz_name)

    def local_date(value: Optional[str]) -> Optional[str]:
        moment = _parse(value)
        return moment.astimezone(zone).date().isoformat() if moment else None

    def local_hour(value: Optional[str]) -> Optional[int]:
        moment = _parse(value)
        return moment.astimezone(zone).hour if moment else None

    def local_dow(value: Optional[str]) -> Optional[str]:
        moment = _parse(value)
        return moment.astimezone(zone).strftime('%A') if moment else None

    conn.create_function('local_date', 1, local_date, deterministic=True)
    conn.create_function('local_hour', 1, local_hour, deterministic=True)
    conn.create_function('local_dow', 1, local_dow, deterministic=True)


def _parse(value: Optional[str]) -> Optional[datetime]:
    """Parse a stored timestamp. Tolerates both `Z` and `+00:00` spellings."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None


def window_utc(frm: Optional[str], to: Optional[str], tz_name: str = DEFAULT_TZ
               ) -> Tuple[Optional[str], Optional[str]]:
    """Convert an inclusive range of local dates to a half-open UTC range.

    `to` is inclusive on the way in and exclusive on the way out, because a
    caller asking for `2026-08-17..2026-08-17` means that whole day. Comparing
    against `<= '2026-08-17'` instead would keep only the midnight second.

    Args:
        frm: Local date `YYYY-MM-DD`, or None for open-ended.
        to: Local date `YYYY-MM-DD`, inclusive, or None.
        tz_name: IANA zone the dates are expressed in.

    Returns:
        `(start, end)` as stored-format UTC strings; either may be None.
    """
    zone = _zone(tz_name)
    start = end = None
    if frm:
        day = date.fromisoformat(frm)
        start = _stamp(datetime.combine(day, time.min, tzinfo=zone))
    if to:
        day = date.fromisoformat(to) + timedelta(days=1)
        end = _stamp(datetime.combine(day, time.min, tzinfo=zone))
    return start, end


def _stamp(moment: datetime) -> str:
    """Render an instant the way the store spells them: UTC, seconds, `Z`."""
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# -- filters ---------------------------------------------------------------

class Filters:
    """What to include in a slice.

    One object for every view, because every view narrows the same event stream
    along the same axes. A view that needed its own filter vocabulary would be
    a sign it belongs somewhere else.
    """

    __slots__ = ('frm', 'to', 'user', 'repo', 'team', 'project', 'issue',
                 'kinds', 'q', 'ai', 'bots', 'tz', 'limit', 'offset')

    def __init__(self, *, frm=None, to=None, user=None, repo=None, team=None,
                 project=None, issue=None, kinds=KINDS, q=None, ai=None,
                 bots=False, tz=DEFAULT_TZ, limit=PAGE, offset=0):
        self.frm = frm
        self.to = to
        self.user = user
        self.repo = repo
        self.team = team
        self.project = project
        self.issue = issue
        self.kinds = tuple(k for k in kinds if k in KINDS) or KINDS
        self.q = q
        self.ai = ai            # None | 'only' | 'none' | a tool name
        self.bots = bots
        self.tz = tz
        self.limit = max(1, min(int(limit), 1000))
        self.offset = max(0, int(offset))

    def window(self) -> Tuple[Optional[str], Optional[str]]:
        return window_utc(self.frm, self.to, self.tz)

    def describe(self) -> Dict[str, Any]:
        return {name: getattr(self, name) for name in self.__slots__}


def _bot_clause(column: str, include_bots: bool) -> str:
    """SQL excluding automation logins, or nothing if bots are wanted.

    Defers to `bot_logins`, which `ghstats-reindex` builds. Two shapes that look
    equivalent are not:

    - `LIKE '%bot%'` would classify a person whose login merely contains the
      substring as an automation -- one such account was among the most
      prolific committers in the org this was built against.
    - `NOT LIKE '%[bot]'` catches bot *commits* and no bot *pull requests*,
      because GraphQL spells the same account `renovate[bot]` on a commit and
      `renovate` on a PR. Renovate alone opened more PRs than any human there.

    Left as a correlated subquery rather than an expanded parameter list so the
    filter cannot drift from what the reindex decided.
    """
    if include_bots:
        return ''
    return (f' AND ({column} IS NULL OR {column} NOT IN '
            '(SELECT login FROM bot_logins))')


def _bot_params(include_bots: bool) -> List[str]:
    """No parameters: `_bot_clause` is self-contained. Kept so call sites read
    symmetrically and adding a parameterised variant later touches one place."""
    return []


# Each kind maps to the table it lives in, the column that dates it, and the
# column naming the actor. Keeping this as data rather than four near-identical
# query builders is what stops the filters drifting apart between kinds.
_SOURCES = {
    'commit': {
        'table': 'commits c JOIN repos r ON r.id = c.repo_id',
        'at': 'c.committed_date',
        'actor': 'c.author_login',
        'repo_id': 'c.repo_id',
        'ref': 'c.oid',
        'ref_kind': 'commit',
    },
    'pull': {
        'table': 'pulls p JOIN repos r ON r.id = p.repo_id',
        'at': 'p.created_at',
        'actor': 'p.author_login',
        'repo_id': 'p.repo_id',
        'ref': 'CAST(p.number AS TEXT)',
        'ref_kind': 'pull',
    },
    'merge': {
        'table': 'pulls p JOIN repos r ON r.id = p.repo_id',
        'at': 'p.merged_at',
        'actor': 'p.author_login',
        'repo_id': 'p.repo_id',
        'ref': 'CAST(p.number AS TEXT)',
        'ref_kind': 'pull',
        'extra': 'p.merged_at IS NOT NULL',
    },
    'review': {
        'table': ('reviews v JOIN repos r ON r.id = v.repo_id '
                  'LEFT JOIN pulls p ON p.repo_id = v.repo_id '
                  'AND p.number = v.pull_number'),
        'at': 'v.submitted_at',
        'actor': 'v.author_login',
        'repo_id': 'v.repo_id',
        'ref': 'CAST(v.pull_number AS TEXT)',
        'ref_kind': 'pull',
    },
}


def _where(kind: str, f: Filters, org: str) -> Tuple[str, List[Any]]:
    """Build the WHERE clause for one kind, and its parameters."""
    src = _SOURCES[kind]
    at, actor = src['at'], src['actor']
    clauses = ['r.org = ?']
    params: List[Any] = [org]

    if src.get('extra'):
        clauses.append(src['extra'])
    else:
        clauses.append(f'{at} IS NOT NULL')

    start, end = f.window()
    if start:
        clauses.append(f'{at} >= ?')
        params.append(start)
    if end:
        clauses.append(f'{at} < ?')
        params.append(end)

    if f.user:
        clauses.append(f'{actor} = ?')
        params.append(f.user)
    if f.repo:
        clauses.append('r.name = ?')
        params.append(f.repo)
    if f.team:
        clauses.append(
            f'{actor} IN (SELECT login FROM team_members '
            'WHERE team_slug = ? AND active = 1)')
        params.append(f.team)

    if f.project or f.issue:
        # For a review, `ref` is its pull request's number, so a review of a PR
        # that names the issue is included. Deliberate: reviewing the change
        # that implements an issue is work on that issue. It does mean an issue's
        # event count exceeds its *reference* count, which is the number
        # `_issue_breakdown` reports -- two different questions.
        column = 'project' if f.project else 'issue_key'
        clauses.append(
            'EXISTS (SELECT 1 FROM issue_refs i '
            f"WHERE i.kind = ? AND i.repo_id = {src['repo_id']} "
            f"AND i.ref = {src['ref']} AND i.{column} = ?)")
        params.extend([src['ref_kind'], f.project or f.issue])

    if f.ai and kind == 'commit':
        if f.ai == 'none':
            clauses.append('NOT EXISTS (SELECT 1 FROM v_commit_ai a '
                           'WHERE a.repo_id = c.repo_id AND a.oid = c.oid)')
        else:
            clause = ('EXISTS (SELECT 1 FROM v_commit_ai a '
                      'WHERE a.repo_id = c.repo_id AND a.oid = c.oid')
            if f.ai != 'only':
                clause += ' AND a.tool = ?'
                params.append(f.ai)
            clauses.append(clause + ')')
    elif f.ai in ('only',) and kind != 'commit':
        # Only commits carry co-author trailers, so an AI filter cannot
        # meaningfully include other kinds. Excluding them is honest; silently
        # returning them as though they qualified is not.
        clauses.append('0')

    if f.q:
        needle = f'%{f.q}%'
        if kind == 'commit':
            clauses.append('(c.message LIKE ? OR c.oid LIKE ?)')
            params.extend([needle, f'{f.q}%'])
        elif kind == 'review':
            clauses.append('(p.title LIKE ? OR v.body LIKE ?)')
            params.extend([needle, needle])
        else:
            clauses.append('p.title LIKE ?')
            params.append(needle)

    bots = _bot_clause(actor, f.bots)
    where = ' AND '.join(clauses)
    if bots:
        where += bots
        params.extend(_bot_params(f.bots))
    return where, params


def _event_select(kind: str) -> str:
    """The column list for one kind, aligned across the union."""
    if kind == 'commit':
        return """
        SELECT 'commit' AS kind, c.committed_date AS at, c.author_login AS actor,
               r.name AS repo, c.oid AS ref, c.message AS body,
               c.additions AS additions, c.deletions AS deletions,
               NULL AS state, c.author_name AS actor_name"""
    if kind == 'pull':
        return """
        SELECT 'pull' AS kind, p.created_at AS at, p.author_login AS actor,
               r.name AS repo, CAST(p.number AS TEXT) AS ref, p.title AS body,
               0 AS additions, 0 AS deletions,
               p.state AS state, NULL AS actor_name"""
    if kind == 'merge':
        return """
        SELECT 'merge' AS kind, p.merged_at AS at, p.author_login AS actor,
               r.name AS repo, CAST(p.number AS TEXT) AS ref, p.title AS body,
               0 AS additions, 0 AS deletions,
               p.state AS state, NULL AS actor_name"""
    return """
        SELECT 'review' AS kind, v.submitted_at AS at, v.author_login AS actor,
               r.name AS repo, CAST(v.pull_number AS TEXT) AS ref,
               COALESCE(p.title, '') AS body,
               0 AS additions, 0 AS deletions,
               v.state AS state, NULL AS actor_name"""


def _union(f: Filters, org: str) -> Tuple[str, List[Any]]:
    """Assemble the filtered union across the requested kinds."""
    parts, params = [], []
    for kind in f.kinds:
        where, kind_params = _where(kind, f, org)
        parts.append(f'{_event_select(kind)}\n        FROM {_SOURCES[kind]["table"]}\n'
                     f'        WHERE {where}')
        params.extend(kind_params)
    return '\nUNION ALL\n'.join(parts), params


# -- the event stream ------------------------------------------------------

def events(conn: sqlite3.Connection, org: str, f: Filters) -> Dict[str, Any]:
    """One page of activity, newest first, with issue keys and AI tools attached.

    The `total` is counted separately from the page so the UI can say "showing
    100 of 4,312" instead of implying the page is the answer. That distinction
    is the whole reason this is a server and not a pre-rendered file.
    """
    union, params = _union(f, org)

    total = conn.execute(
        f'SELECT COUNT(*) FROM ({union})', params).fetchone()[0]

    rows = conn.execute(
        f'SELECT * FROM ({union}) ORDER BY at DESC, kind, repo, ref '
        'LIMIT ? OFFSET ?', [*params, f.limit, f.offset]).fetchall()

    out = [_event_row(r, org) for r in rows]
    _attach_issues(conn, out)
    _attach_tools(conn, out)
    return {'total': total, 'returned': len(out), 'offset': f.offset,
            'events': out}


def _event_row(row: sqlite3.Row, org: str) -> Dict[str, Any]:
    """Shape one union row into the event contract the UI consumes."""
    body = row['body'] or ''
    subject, _, rest = body.partition('\n')
    kind, repo, ref = row['kind'], row['repo'], row['ref']

    if kind == 'commit':
        url = f'https://github.com/{org}/{repo}/commit/{ref}'
        label = ref[:8]
    else:
        # Reviews link to their pull request: `reviews.id` is a GraphQL node id
        # (`PRR_kwDO...`), not the numeric id an anchor would need.
        url = f'https://github.com/{org}/{repo}/pull/{ref}'
        label = f'#{ref}'

    return {
        'kind': kind,
        'at': row['at'],
        'actor': row['actor'],
        'actor_name': row['actor_name'],
        'repo': repo,
        'ref': ref,
        'label': label,
        'subject': subject.strip() or '(no message)',
        'body': rest.strip()[:2000],
        'additions': row['additions'] or 0,
        'deletions': row['deletions'] or 0,
        'state': row['state'],
        'url': url,
        'issues': [],
        'tools': [],
    }


def _attach_issues(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> None:
    """Fill in issue keys for a page of events, in one query per kind.

    Per-event lookups would be 100 round trips for a 100-row page. The join key
    differs by kind, which is why `issue_refs.kind` is matched explicitly.
    """
    if not rows:
        return
    wanted = {('commit' if r['kind'] == 'commit' else 'pull',
               r['repo'], r['ref']) for r in rows}
    by_key: Dict[Tuple[str, str, str], List[str]] = {}
    for ref_kind in {k for k, _, _ in wanted}:
        refs = sorted({(repo, ref) for k, repo, ref in wanted if k == ref_kind})
        for chunk in _chunks(refs, 400):
            names = ','.join('(?,?)' for _ in chunk)
            flat = [v for pair in chunk for v in pair]
            sql = ('SELECT r.name AS repo, i.ref, i.issue_key '
                   'FROM issue_refs i JOIN repos r ON r.id = i.repo_id '
                   'WHERE i.kind = ? AND (r.name, i.ref) IN '
                   f'(VALUES {names})')
            for row in conn.execute(sql, [ref_kind, *flat]):
                by_key.setdefault(
                    (ref_kind, row['repo'], row['ref']), []).append(
                        row['issue_key'])

    for row in rows:
        ref_kind = 'commit' if row['kind'] == 'commit' else 'pull'
        row['issues'] = sorted(
            by_key.get((ref_kind, row['repo'], row['ref']), []))


def _attach_tools(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> None:
    """Fill in AI tool names for the commit events on a page."""
    commits = [r for r in rows if r['kind'] == 'commit']
    if not commits:
        return
    refs = sorted({(r['repo'], r['ref']) for r in commits})
    found: Dict[Tuple[str, str], List[str]] = {}
    for chunk in _chunks(refs, 400):
        names = ','.join('(?,?)' for _ in chunk)
        flat = [v for pair in chunk for v in pair]
        sql = ('SELECT r.name AS repo, a.oid, a.tool FROM v_commit_ai a '
               'JOIN repos r ON r.id = a.repo_id '
               f'WHERE (r.name, a.oid) IN (VALUES {names})')
        for row in conn.execute(sql, flat):
            found.setdefault((row['repo'], row['oid']), []).append(row['tool'])
    for row in commits:
        row['tools'] = sorted(set(found.get((row['repo'], row['ref']), [])))


def _chunks(items: Sequence[Any], size: int) -> List[Sequence[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


# -- shared aggregates -----------------------------------------------------

def _totals(conn: sqlite3.Connection, org: str, f: Filters) -> Dict[str, Any]:
    """Headline counts for a slice, one query per kind.

    Counted per kind rather than off the union so that lines changed -- which
    only commits carry -- is not summed over rows that always contribute zero.
    """
    out = {'commits': 0, 'pulls': 0, 'merges': 0, 'reviews': 0,
           'lines_added': 0, 'lines_removed': 0, 'repos': 0, 'people': 0}

    for kind in KINDS:
        narrowed = _with(f, kinds=(kind,))
        where, params = _where(kind, narrowed, org)
        table = _SOURCES[kind]['table']
        if kind == 'commit':
            row = conn.execute(
                'SELECT COUNT(*) n, COALESCE(SUM(c.additions),0) a, '
                f'COALESCE(SUM(c.deletions),0) d FROM {table} WHERE {where}',
                params).fetchone()
            out['commits'] = row['n']
            out['lines_added'] = row['a']
            out['lines_removed'] = row['d']
        else:
            n = conn.execute(
                f'SELECT COUNT(*) FROM {table} WHERE {where}', params
            ).fetchone()[0]
            out[{'pull': 'pulls', 'merge': 'merges',
                 'review': 'reviews'}[kind]] = n

    out['lines_net'] = out['lines_added'] - out['lines_removed']
    union, params = _union(f, org)
    row = conn.execute(
        'SELECT COUNT(DISTINCT repo) r, COUNT(DISTINCT actor) p '
        f'FROM ({union})', params).fetchone()
    out['repos'] = row['r']
    out['people'] = row['p']
    return out


def _with(f: Filters, **overrides) -> Filters:
    """A copy of `f` with some fields replaced."""
    fields = f.describe()
    fields.update(overrides)
    return Filters(**fields)


def _span(f: Filters, present: List[str]) -> List[str]:
    """Every local day a daily series should carry, oldest first.

    The requested window when it has ends, and the span of the data when it
    does not: "All" must not invent the eighteen months before the first
    commit. The tail is clamped to today, because a window may reach into the
    future and a run of empty days after the last one that could exist is not a
    gap in the work, it is just the calendar.
    """
    if not present:
        return []
    first = f.frm or present[0]
    last = f.to or present[-1]
    today = datetime.now(_zone(f.tz)).date().isoformat()
    # `max` against the data: a committer with a wrong clock can date work
    # after today, and dropping it here would leave a hole the query filled.
    last = min(last, max(today, present[-1]))
    if first > last:
        return []
    out, cursor, end = [], date.fromisoformat(first), date.fromisoformat(last)
    while cursor <= end:
        out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


def _by_day(conn: sqlite3.Connection, org: str, f: Filters) -> List[Dict[str, Any]]:
    """Event counts per local day, oldest first, with per-kind breakdown.

    **Quiet days are in here as zeroes.** A series of only the days that had
    something on them draws a fortnight of silence as no gap at all: the bars
    either side end up adjacent, the axis quietly relabels itself, and a
    stop-start month reads as a steady one. Weekends are the everyday case --
    without the zeroes a working week and a seven-day grind look identical.

    An empty slice still returns nothing rather than a flat row of zeroes, so
    "no activity in this window" stays a sentence instead of an empty chart.
    """
    union, params = _union(f, org)
    rows = conn.execute(f"""
        SELECT local_date(at) AS day, kind, COUNT(*) AS n,
               COALESCE(SUM(additions),0) AS added,
               COALESCE(SUM(deletions),0) AS removed
        FROM ({union})
        GROUP BY day, kind ORDER BY day""", params).fetchall()

    def blank(day: str) -> Dict[str, Any]:
        return {'day': day, 'total': 0, 'added': 0, 'removed': 0,
                **{k: 0 for k in KINDS}}

    days: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        day = days.setdefault(row['day'], blank(row['day']))
        day[row['kind']] = row['n']
        day['total'] += row['n']
        day['added'] += row['added']
        day['removed'] += row['removed']

    for day in _span(f, sorted(days)):
        days.setdefault(day, blank(day))
    return [days[k] for k in sorted(days)]


def _grouped(conn: sqlite3.Connection, org: str, f: Filters, column: str,
             limit: int = 25) -> List[Dict[str, Any]]:
    """Top `column` values in a slice by event count."""
    union, params = _union(f, org)
    rows = conn.execute(f"""
        SELECT {column} AS name, COUNT(*) AS total,
               SUM(kind = 'commit') AS commits,
               SUM(kind = 'pull') AS pulls,
               SUM(kind = 'merge') AS merges,
               SUM(kind = 'review') AS reviews,
               COALESCE(SUM(additions),0) AS added,
               COALESCE(SUM(deletions),0) AS removed
        FROM ({union})
        WHERE {column} IS NOT NULL
        GROUP BY name ORDER BY total DESC LIMIT ?""",
        [*params, limit]).fetchall()
    return [dict(r) for r in rows]


DAYS_OF_WEEK = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday',
                'Saturday', 'Sunday')


def _rhythm(conn: sqlite3.Connection, org: str, f: Filters) -> Dict[str, Any]:
    """When in the week and the day the work happens, in local time.

    Split by kind rather than summed. A single total series says "the day peaks
    at 10:00" and hides the thing worth knowing -- that commits peak in the
    morning and reviews in the afternoon, because a review waits on someone
    else's morning. The client stacks or groups the kinds it was asked for; the
    query has no opinion about that and returns all four, zero-filled.

    Zero-filling here rather than in the client: an hour with no activity is a
    real zero in a 24-point series, and a chart that has to invent the gaps is
    a chart that will one day invent them differently from its neighbour.
    """
    union, params = _union(f, org)
    hours = conn.execute(
        f'SELECT local_hour(at) AS h, kind, COUNT(*) n FROM ({union}) '
        'GROUP BY h, kind', params).fetchall()
    dows = conn.execute(
        f'SELECT local_dow(at) AS d, kind, COUNT(*) n FROM ({union}) '
        'GROUP BY d, kind', params).fetchall()

    by_hour = {k: {str(h): 0 for h in range(24)} for k in KINDS}
    by_dow = {k: {d: 0 for d in DAYS_OF_WEEK} for k in KINDS}
    for row in hours:
        if row['h'] is not None:
            by_hour[row['kind']][str(row['h'])] = row['n']
    for row in dows:
        if row['d']:
            by_dow[row['kind']][row['d']] = row['n']
    return {
        'by_hour': by_hour,
        'by_day_of_week': by_dow,
        'days_of_week': list(DAYS_OF_WEEK),
    }


def _issue_breakdown(conn: sqlite3.Connection, org: str, f: Filters,
                     limit: int = 20) -> Dict[str, Any]:
    """Which Jira projects and issues a slice touched.

    Counted over `issue_refs` joined back to the same filters, so "this person's
    work on a project" means their commits and PRs referencing it -- not every
    reference the project ever collected.

    **Reviews are excluded here and included in the event stream.** This counts
    references -- text naming a key -- and a review names nothing; a review is
    reached only through the pull request it is on. So `refs` is smaller than the
    event total for the same issue, and both are right: this is "how often was
    this key written", the stream is "what work touched it".
    """
    projects: Dict[str, Dict[str, Any]] = {}
    issues: Dict[str, Dict[str, Any]] = {}

    for kind in f.kinds:
        if kind == 'review':
            continue                      # reviews carry no key of their own
        src = _SOURCES[kind]
        where, params = _where(kind, _with(f, kinds=(kind,)), org)
        rows = conn.execute(f"""
            SELECT i.project, i.issue_key, COUNT(*) AS n
            FROM {src['table']}
            JOIN issue_refs i ON i.kind = ?
              AND i.repo_id = {src['repo_id']} AND i.ref = {src['ref']}
            WHERE {where}
            GROUP BY i.project, i.issue_key""",
            [src['ref_kind'], *params]).fetchall()
        for row in rows:
            p = projects.setdefault(
                row['project'], {'project': row['project'], 'refs': 0,
                                 'issues': set()})
            p['refs'] += row['n']
            p['issues'].add(row['issue_key'])
            i = issues.setdefault(
                row['issue_key'], {'issue': row['issue_key'],
                                   'project': row['project'], 'refs': 0})
            i['refs'] += row['n']

    ranked_projects = sorted(
        ({'project': p['project'], 'refs': p['refs'],
          'issues': len(p['issues'])} for p in projects.values()),
        key=lambda d: -d['refs'])
    ranked_issues = sorted(issues.values(), key=lambda d: -d['refs'])
    return {'projects': ranked_projects[:limit],
            'issues': ranked_issues[:limit]}


def _tool_breakdown(conn: sqlite3.Connection, org: str, f: Filters
                    ) -> Dict[str, Any]:
    """AI-assisted commit share for a slice.

    `COUNT(DISTINCT ...)` on the commit, not on the trailer: a session spanning
    two models leaves two trailers on one commit, so counting rows overstates
    assisted commits by about a third on this data.
    """
    if 'commit' not in f.kinds:
        return {'assisted': 0, 'total': 0, 'by_tool': []}
    where, params = _where('commit', _with(f, kinds=('commit',)), org)
    table = _SOURCES['commit']['table']

    total = conn.execute(
        f'SELECT COUNT(*) FROM {table} WHERE {where}', params).fetchone()[0]
    rows = conn.execute(f"""
        SELECT a.tool, COUNT(DISTINCT c.repo_id || ':' || c.oid) AS commits
        FROM {table}
        JOIN v_commit_ai a ON a.repo_id = c.repo_id AND a.oid = c.oid
        WHERE {where}
        GROUP BY a.tool ORDER BY commits DESC""", params).fetchall()
    assisted = conn.execute(f"""
        SELECT COUNT(DISTINCT c.repo_id || ':' || c.oid)
        FROM {table}
        JOIN v_commit_ai a ON a.repo_id = c.repo_id AND a.oid = c.oid
        WHERE {where}""", params).fetchone()[0]
    return {'assisted': assisted, 'total': total,
            'by_tool': [dict(r) for r in rows]}


def _slice(conn: sqlite3.Connection, org: str, f: Filters, *,
           group: Optional[str] = None) -> Dict[str, Any]:
    """The standard bundle every entry point returns.

    Totals, a daily series, rhythm, issue and tool breakdowns, and a first page
    of events. Views differ in what they group by and what they add on top, not
    in how they compute the shared parts.
    """
    bundle = {
        'filters': f.describe(),
        'totals': _totals(conn, org, f),
        'by_day': _by_day(conn, org, f),
        'rhythm': _rhythm(conn, org, f),
        'issues': _issue_breakdown(conn, org, f),
        'ai': _tool_breakdown(conn, org, f),
        'events': events(conn, org, f),
    }
    if group:
        bundle['by_repo'] = _grouped(conn, org, f, 'repo')
        bundle['by_actor'] = _grouped(conn, org, f, 'actor')
    return bundle


# -- entry points ----------------------------------------------------------

def meta(conn: sqlite3.Connection, org: str, tz_name: str = DEFAULT_TZ
         ) -> Dict[str, Any]:
    """What the store holds, and how stale it is.

    Served on load so the UI can show freshness up front. A stalled sync looks
    exactly like a quiet fortnight otherwise -- the same reason the static
    report opens with a banner.
    """
    row = conn.execute("""
        SELECT MAX(c.covered_from) AS covered_from,
               MIN(c.covered_to) AS covered_to
        FROM coverage c JOIN repos r ON r.id = c.repo_id
        WHERE r.org = ?""", (org,)).fetchone()
    run = conn.execute(
        'SELECT * FROM sync_runs WHERE complete = 1 '
        'ORDER BY started_at DESC LIMIT 1').fetchone()

    counts = {
        'repos': conn.execute(
            'SELECT COUNT(*) FROM repos WHERE org = ?', (org,)).fetchone()[0],
        'members': conn.execute(
            'SELECT COUNT(*) FROM members WHERE active = 1').fetchone()[0],
        'teams': conn.execute(
            'SELECT COUNT(*) FROM teams WHERE active = 1').fetchone()[0],
        'commits': conn.execute('SELECT COUNT(*) FROM commits').fetchone()[0],
        'pulls': conn.execute('SELECT COUNT(*) FROM pulls').fetchone()[0],
        'reviews': conn.execute('SELECT COUNT(*) FROM reviews').fetchone()[0],
        'issues': conn.execute(
            'SELECT COUNT(DISTINCT issue_key) FROM issue_refs').fetchone()[0],
    }

    covered_to = _parse(row['covered_to']) if row else None
    stale_hours = None
    if covered_to:
        stale_hours = round(
            (datetime.now(timezone.utc) - covered_to).total_seconds() / 3600, 1)

    return {
        'org': org,
        'timezone': tz_name,
        'covered_from': row['covered_from'] if row else None,
        'covered_to': row['covered_to'] if row else None,
        'stale_hours': stale_hours,
        'counts': counts,
        'unsyncable': sorted(UNSYNCABLE_REPOS),
        'last_sync': {
            'at': run['finished_at'] or run['started_at'],
            'repos': run['repos_synced'],
            'failed': run['repos_failed'],
            'seconds': run['seconds'],
        } if run else None,
        'kinds': list(KINDS),
        'teams_present': counts['teams'] > 0,
        'issues_present': counts['issues'] > 0,
    }


def search(conn: sqlite3.Connection, org: str, term: str, limit: int = 8
           ) -> Dict[str, Any]:
    """Resolve a typed string to entry points across every dimension.

    One box, because someone with a name, a repository and a ticket key in hand
    should not have to know which tab each belongs to.
    """
    term = (term or '').strip()
    if not term:
        return {'users': [], 'repos': [], 'teams': [], 'issues': [],
                'projects': []}
    like = f'%{term}%'

    users = [r['login'] for r in conn.execute(
        'SELECT login FROM members WHERE active = 1 AND login LIKE ? '
        'ORDER BY (login = ?) DESC, LENGTH(login), login LIMIT ?',
        (like, term, limit))]
    repos = [r['name'] for r in conn.execute(
        'SELECT name FROM repos WHERE org = ? AND name LIKE ? '
        'ORDER BY (name = ?) DESC, LENGTH(name), name LIMIT ?',
        (org, like, term, limit))]
    teams = [dict(r) for r in conn.execute(
        'SELECT slug, name FROM teams WHERE active = 1 '
        'AND (slug LIKE ? OR name LIKE ?) ORDER BY LENGTH(slug), slug LIMIT ?',
        (like, like, limit))]
    issues = [r['issue_key'] for r in conn.execute(
        'SELECT issue_key, COUNT(*) n FROM issue_refs WHERE issue_key LIKE ? '
        'GROUP BY issue_key ORDER BY (issue_key = ?) DESC, n DESC LIMIT ?',
        (like.upper(), term.upper(), limit))]
    projects = [r['project'] for r in conn.execute(
        'SELECT project, COUNT(*) n FROM issue_refs WHERE project LIKE ? '
        'GROUP BY project ORDER BY n DESC LIMIT ?', (like.upper(), limit))]

    return {'users': users, 'repos': repos, 'teams': teams,
            'issues': issues, 'projects': projects}


def user_list(conn: sqlite3.Connection, org: str, f: Filters
              ) -> List[Dict[str, Any]]:
    """Everyone active in the window, busiest first, with their teams."""
    rows = _grouped(conn, org, _with(f, user=None), 'actor', limit=500)
    teams: Dict[str, List[str]] = {}
    for row in conn.execute(
            'SELECT login, team_slug FROM team_members WHERE active = 1'):
        teams.setdefault(row['login'], []).append(row['team_slug'])
    for row in rows:
        row['login'] = row.pop('name')
        row['teams'] = sorted(teams.get(row['login'], []))
    return rows


def user_overview(conn: sqlite3.Connection, org: str, f: Filters
                  ) -> Dict[str, Any]:
    """The People entry point: the roster, and the shape of the whole window.

    The same daily series, rhythm and totals a person's own page shows, with
    the person filter dropped -- so "when does this org work" and "when does
    she work" are the same two charts read at two scales, and a personal
    pattern can be judged against the one it sits inside.

    No event page: the entry point is a directory, and the stream underneath it
    would be the unfiltered firehose. Pick someone first.
    """
    wide = _with(f, user=None)
    return {
        'users': user_list(conn, org, wide),
        'filters': wide.describe(),
        'totals': _totals(conn, org, wide),
        'by_day': _by_day(conn, org, wide),
        'rhythm': _rhythm(conn, org, wide),
    }


def user_detail(conn: sqlite3.Connection, org: str, login: str, f: Filters
                ) -> Dict[str, Any]:
    """What one person did: the bundle, plus who they worked alongside."""
    f = _with(f, user=login)
    bundle = _slice(conn, org, f)
    bundle['user'] = {
        'login': login,
        'teams': [dict(r) for r in conn.execute(
            'SELECT t.slug, t.name, m.role FROM team_members m '
            'JOIN teams t ON t.slug = m.team_slug '
            'WHERE m.login = ? AND m.active = 1 ORDER BY t.name', (login,))],
        'member': conn.execute(
            'SELECT active FROM members WHERE login = ?', (login,)).fetchone()
        is not None,
        'names': [r['author_name'] for r in conn.execute(
            'SELECT DISTINCT author_name FROM commits '
            'WHERE author_login = ? AND author_name IS NOT NULL LIMIT 5',
            (login,))],
    }
    bundle['by_repo'] = _grouped(conn, org, f, 'repo')
    bundle['reviewers'] = _reviewers_of(conn, org, login, f)
    bundle['reviewed'] = _reviewed_by(conn, org, login, f)
    return bundle


def _reviewers_of(conn: sqlite3.Connection, org: str, login: str, f: Filters
                  ) -> List[Dict[str, Any]]:
    """Who reviewed this person's pull requests, most often first."""
    start, end = f.window()
    clauses = ['r.org = ?', 'p.author_login = ?', 'v.author_login IS NOT NULL',
               'v.author_login != p.author_login']
    params: List[Any] = [org, login]
    if start:
        clauses.append('v.submitted_at >= ?')
        params.append(start)
    if end:
        clauses.append('v.submitted_at < ?')
        params.append(end)
    if f.repo:
        clauses.append('r.name = ?')
        params.append(f.repo)
    where = ' AND '.join(clauses) + _bot_clause('v.author_login', f.bots)
    params.extend(_bot_params(f.bots))
    return [dict(r) for r in conn.execute(f"""
        SELECT v.author_login AS login, COUNT(*) AS reviews
        FROM reviews v
        JOIN pulls p ON p.repo_id = v.repo_id AND p.number = v.pull_number
        JOIN repos r ON r.id = v.repo_id
        WHERE {where}
        GROUP BY login ORDER BY reviews DESC LIMIT 15""", params)]


def _reviewed_by(conn: sqlite3.Connection, org: str, login: str, f: Filters
                 ) -> List[Dict[str, Any]]:
    """Whose pull requests this person reviewed, most often first."""
    start, end = f.window()
    clauses = ['r.org = ?', 'v.author_login = ?', 'p.author_login IS NOT NULL',
               'v.author_login != p.author_login']
    params: List[Any] = [org, login]
    if start:
        clauses.append('v.submitted_at >= ?')
        params.append(start)
    if end:
        clauses.append('v.submitted_at < ?')
        params.append(end)
    if f.repo:
        clauses.append('r.name = ?')
        params.append(f.repo)
    where = ' AND '.join(clauses) + _bot_clause('p.author_login', f.bots)
    params.extend(_bot_params(f.bots))
    return [dict(r) for r in conn.execute(f"""
        SELECT p.author_login AS login, COUNT(*) AS reviews
        FROM reviews v
        JOIN pulls p ON p.repo_id = v.repo_id AND p.number = v.pull_number
        JOIN repos r ON r.id = v.repo_id
        WHERE {where}
        GROUP BY login ORDER BY reviews DESC LIMIT 15""", params)]


def repo_list(conn: sqlite3.Connection, org: str, f: Filters
              ) -> List[Dict[str, Any]]:
    """Repositories with activity in the window, busiest first."""
    rows = _grouped(conn, org, _with(f, repo=None), 'repo', limit=500)
    for row in rows:
        row['repo'] = row.pop('name')
    return rows


def repo_overview(conn: sqlite3.Connection, org: str, f: Filters
                  ) -> Dict[str, Any]:
    """The Repositories entry point: every repository, and PR size across them.

    The pull request card here is the same one a repository's page draws, over
    the whole organization, plus a row per repository -- a repository's own
    ratio only means something against the ones next to it.
    """
    f = _with(f, repo=None)
    return {
        'repos': repo_list(conn, org, f),
        'pulls': pull_discussion(conn, org, f, per_repo=True),
    }


def repo_detail(conn: sqlite3.Connection, org: str, name: str, f: Filters
                ) -> Dict[str, Any]:
    """What changed in one repository, and who changed it."""
    f = _with(f, repo=name)
    bundle = _slice(conn, org, f)
    bundle['repo'] = {
        'name': name,
        'known': conn.execute(
            'SELECT 1 FROM repos WHERE org = ? AND name = ?',
            (org, name)).fetchone() is not None,
        'unsyncable': UNSYNCABLE_REPOS.get(name),
        'coverage': [dict(r) for r in conn.execute("""
            SELECT c.kind, c.covered_from, c.covered_to
            FROM coverage c JOIN repos r ON r.id = c.repo_id
            WHERE r.org = ? AND r.name = ? ORDER BY c.kind""", (org, name))],
        'teams': [dict(r) for r in conn.execute("""
            SELECT t.slug, t.name, tr.permission
            FROM team_repos tr JOIN teams t ON t.slug = tr.team_slug
            WHERE tr.repo_name = ? AND tr.active = 1 AND t.active = 1
            ORDER BY t.name""", (name,))],
    }
    bundle['by_actor'] = _grouped(conn, org, f, 'actor')
    bundle['pulls'] = pull_discussion(conn, org, f)
    return bundle


def team_list(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Every team, with roster and repository-grant sizes.

    Takes no `org`: like `members`, teams are not org-scoped in the schema,
    because one store holds one organization's people.

    `unassigned` counts members on no team at all -- a third of them on the
    store this was built against. Leaving that implicit would make the team
    views look like they cover the organization when they cover two thirds
    of it.
    """
    teams = [dict(r) for r in conn.execute("""
        SELECT t.slug, t.name, t.description, t.parent_slug,
               (SELECT COUNT(*) FROM team_members m
                WHERE m.team_slug = t.slug AND m.active = 1) AS members,
               (SELECT COUNT(*) FROM team_repos tr
                WHERE tr.team_slug = t.slug AND tr.active = 1) AS repos
        FROM teams t WHERE t.active = 1
        ORDER BY members DESC, t.name""")]
    unassigned = conn.execute("""
        SELECT COUNT(*) FROM members m WHERE m.active = 1
        AND NOT EXISTS (SELECT 1 FROM team_members t
                        WHERE t.login = m.login AND t.active = 1)""").fetchone()[0]
    return {'teams': teams, 'unassigned': unassigned}


def team_detail(conn: sqlite3.Connection, org: str, slug: str, f: Filters
                ) -> Dict[str, Any]:
    """What a team changed, rolled up and per member.

    **Caveat, surfaced in the response.** GitHub reports current membership with
    no history, so a window that predates someone's move attributes their old
    work to their new team. Right for last week, wrong across a reorg.
    """
    f = _with(f, team=slug)
    bundle = _slice(conn, org, f)
    team = conn.execute(
        'SELECT slug, name, description, parent_slug FROM teams WHERE slug = ?',
        (slug,)).fetchone()
    roster = [dict(r) for r in conn.execute(
        'SELECT login, role, active FROM team_members WHERE team_slug = ? '
        'ORDER BY active DESC, login', (slug,))]

    per_member = {row['name']: row for row in _grouped(
        conn, org, f, 'actor', limit=500)}
    for person in roster:
        stats = per_member.get(person['login'], {})
        person.update({k: stats.get(k, 0) for k in
                       ('total', 'commits', 'pulls', 'merges', 'reviews',
                        'added', 'removed')})

    bundle['team'] = {
        **(dict(team) if team else {'slug': slug, 'name': slug}),
        'known': team is not None,
        'membership_is_current_only': True,
        'roster': sorted(roster, key=lambda p: -p['total']),
        # Left-joined against `repos` so the UI can mark a grant the sweep
        # does not cover -- an archived repository, typically.
        'repos': [dict(r) for r in conn.execute("""
            SELECT tr.repo_name AS name, tr.permission,
                   (r.id IS NOT NULL) AS tracked
            FROM team_repos tr
            LEFT JOIN repos r ON r.name = tr.repo_name AND r.org = ?
            WHERE tr.team_slug = ? AND tr.active = 1
            ORDER BY tr.repo_name LIMIT 400""", (org, slug))],
    }
    bundle['by_repo'] = _grouped(conn, org, f, 'repo')
    bundle['by_actor'] = _grouped(conn, org, f, 'actor')
    return bundle


def project_list(conn: sqlite3.Connection, org: str, f: Filters
                 ) -> List[Dict[str, Any]]:
    """Jira projects referenced in the window, busiest first."""
    return _issue_breakdown(conn, org, _with(f, project=None, issue=None),
                            limit=200)['projects']


def issue_detail(conn: sqlite3.Connection, org: str, key: str, f: Filters
                 ) -> Dict[str, Any]:
    """Everything referencing one issue key, across repositories and people."""
    f = _with(f, issue=key.upper(), project=None)
    bundle = _slice(conn, org, f)
    bundle['issue'] = {
        'key': key.upper(),
        'project': (conn.execute(
            'SELECT project FROM issue_refs WHERE issue_key = ? LIMIT 1',
            (key.upper(),)).fetchone() or {'project': None})['project'],
        'aliases': [r['key'] for r in conn.execute(
            'SELECT key FROM jira_projects WHERE canonical = '
            '(SELECT canonical FROM jira_projects WHERE key = ?) '
            'AND key != canonical', (key.upper().rsplit('-', 1)[0],))],
    }
    bundle['by_repo'] = _grouped(conn, org, f, 'repo')
    bundle['by_actor'] = _grouped(conn, org, f, 'actor')
    return bundle


def project_detail(conn: sqlite3.Connection, org: str, project: str, f: Filters
                   ) -> Dict[str, Any]:
    """One Jira project: its issues, and who moved them."""
    f = _with(f, project=project.upper(), issue=None)
    bundle = _slice(conn, org, f)
    bundle['project'] = {
        'key': project.upper(),
        'aliases': [r['key'] for r in conn.execute(
            'SELECT key FROM jira_projects WHERE canonical = ? AND key != ?',
            (project.upper(), project.upper()))],
    }
    bundle['by_repo'] = _grouped(conn, org, f, 'repo')
    bundle['by_actor'] = _grouped(conn, org, f, 'actor')
    return bundle


def day_detail(conn: sqlite3.Connection, org: str, day: str, f: Filters
               ) -> Dict[str, Any]:
    """One local day, cross-cut by team, repository and person.

    The narrowest useful window and the one the other views hand off to: a
    spike on a daily chart is only interesting once you can see what it was.
    """
    f = _with(f, frm=day, to=day, limit=max(f.limit, 300))
    bundle = _slice(conn, org, f)
    bundle['day'] = {
        'date': day,
        'by_team': _by_team(conn, org, f),
    }
    bundle['by_repo'] = _grouped(conn, org, f, 'repo', limit=60)
    bundle['by_actor'] = _grouped(conn, org, f, 'actor', limit=60)
    return bundle


def _by_team(conn: sqlite3.Connection, org: str, f: Filters
             ) -> List[Dict[str, Any]]:
    """Split a slice by the team each actor currently belongs to.

    Someone on two teams counts once for each -- the totals therefore do not
    sum to the slice total, which is the honest rendering of overlapping
    rosters rather than an arbitrary tie-break.
    """
    union, params = _union(f, org)
    rows = conn.execute(f"""
        SELECT COALESCE(m.team_slug, '(no team)') AS team,
               COUNT(*) AS total,
               SUM(e.kind = 'commit') AS commits,
               SUM(e.kind = 'pull') AS pulls,
               SUM(e.kind = 'merge') AS merges,
               SUM(e.kind = 'review') AS reviews,
               COUNT(DISTINCT e.actor) AS people
        FROM ({union}) e
        LEFT JOIN team_members m ON m.login = e.actor AND m.active = 1
        GROUP BY team ORDER BY total DESC""", params).fetchall()
    return [dict(r) for r in rows]


# -- pull request size and discussion --------------------------------------

# Above this many pull requests the scatter stops being a plot and starts being
# a smear, and the payload stops being small. The newest are kept, because the
# question the chart answers is about the trend ending today.
SCATTER_CAP = 2000

# Where the trend buckets switch from weeks to months. A quarter drawn in
# months is four points, which is not a trend; two years drawn in weeks is a
# hundred, which is not readable.
WEEKLY_DAYS = 120


def _median(values: Sequence[float]) -> float:
    """Middle value, or the mean of the middle pair. Zero for nothing.

    Medians rather than means throughout this section. Pull request size is
    heavily skewed -- one lockfile refresh or generated-client bump is tens of
    thousands of lines and drags a monthly mean past every real change in the
    month. The means are returned alongside, because the gap between the two is
    itself the signal that a month had one of those.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _bucket_of(day: str, weekly: bool) -> str:
    """The trend bucket a local date falls in.

    Weeks are labelled by their Monday, so a bucket is a date the rest of the
    UI can already parse, rather than an ISO week number nobody can place in a
    year without counting.
    """
    if not weekly:
        return day[:7]
    stamp = date.fromisoformat(day)
    return (stamp - timedelta(days=stamp.weekday())).isoformat()


def _weekly(f: Filters, days: Sequence[str]) -> bool:
    """Whether the trend should bucket by week rather than by month."""
    if not days:
        return False
    first = f.frm or min(days)
    last = f.to or max(days)
    try:
        span = (date.fromisoformat(last) - date.fromisoformat(first)).days
    except ValueError:
        return False
    return span <= WEEKLY_DAYS


def _pull_summary(members: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Size and discussion over a set of measured pull requests.

    One shape for a trend bucket, the window's totals and a repository's row,
    so the three cannot drift into disagreeing about what a median is.
    """
    lines = [p['lines'] for p in members]
    discussion = [p['discussion'] for p in members]
    lines_total = sum(lines)
    count = len(members)
    return {
        'pulls': count,
        'merged': sum(1 for p in members if p['merged']),
        'lines_median': _median(lines),
        'lines_mean': lines_total / count if count else 0.0,
        'lines_total': lines_total,
        'files_median': _median([p['files'] for p in members]),
        'discussion_median': _median(discussion),
        'discussion_mean': sum(discussion) / count if count else 0.0,
        'discussion_total': sum(discussion),
        'conversation': sum(p['conversation'] for p in members),
        'inline': sum(p['inline'] for p in members),
        'reviews': sum(p['reviews'] for p in members),
        # Comments per 100 lines changed, over the set as a whole rather than
        # as a mean of per-PR ratios: a one-line PR with two comments would
        # otherwise contribute a ratio of 200 and outweigh every ordinary
        # change in the month.
        'per_100_lines': (sum(discussion) * 100.0 / lines_total
                          if lines_total else None),
        'undiscussed': sum(1 for p in members if not p['discussion']),
    }


def pull_discussion(conn: sqlite3.Connection, org: str, f: Filters, *,
                    per_repo: bool = False) -> Dict[str, Any]:
    """How big pull requests are, how much they are discussed, and the trend.

    Answers one question -- does review attention keep up with the size of what
    is being shipped -- in the two forms it is actually asked: the shape over
    time, and the individual outliers.

    **A pull request with no `pull_metrics` row is excluded, not counted as
    zero.** Everything synced before schema 5 has no measurement until
    `ghstats-backfill-pulls` has run, and coalescing that to zero would draw
    twenty months of unmeasured history as a flat line of undiscussed,
    zero-line pull requests -- indistinguishable from a real quiet period, and
    wrong in the direction that looks like a finding. `measured` and `total`
    are returned so the UI can say which it is.

    **Discussion is comments + inline review comments + reviews with a body.**
    An approve click is a review with an empty body and no comments; counting
    it would make every rubber-stamped pull request look debated. The three
    parts are returned separately as well as summed.

    **Trap: the bots filter cannot reach comment counts.** `author_login`
    filters which pull requests and which *reviews* are counted, but
    `comments` and `review_comments` are totals GitHub reports per pull
    request, with no author breakdown -- getting one would mean fetching every
    comment node instead of a count. So with bots excluded, a Renovate PR
    drops out entirely, while a human PR that Copilot left twelve inline
    comments on still carries them. That inflates discussion on repositories
    with review automation, and the UI says so rather than leaving it to be
    found.

    Returns:
        `buckets` (the trend, oldest first), `points` (per pull request, for
        the scatter), `totals`, and the measured/total coverage counts. With
        `per_repo`, also `by_repo`: the totals' shape once per repository that
        opened a pull request in the window, most pull requests first,
        including repositories none of whose pull requests are measured.
    """
    f = _with(f, kinds=('pull',))
    where, params = _where('pull', f, org)
    src = _SOURCES['pull']

    # Bot reviews are excluded from the review-body count on the same terms as
    # everything else, so the one part of discussion that *can* respect the
    # filter does.
    review_bots = _bot_clause('v.author_login', f.bots)

    rows = conn.execute(f"""
        SELECT r.name AS repo, p.number AS number, p.title AS title,
               p.author_login AS author, p.created_at AS at,
               local_date(p.created_at) AS day,
               p.merged_at IS NOT NULL AS merged,
               m.additions AS added, m.deletions AS removed,
               m.changed_files AS files,
               m.comments AS conversation, m.review_comments AS inline,
               (SELECT COUNT(*) FROM reviews v
                 WHERE v.repo_id = p.repo_id AND v.pull_number = p.number
                   AND TRIM(v.body) <> ''{review_bots}) AS review_bodies
        FROM {src['table']}
        JOIN pull_metrics m
          ON m.repo_id = p.repo_id AND m.number = p.number
        WHERE {where}
        ORDER BY p.created_at""", params).fetchall()

    # Every pull request in the window, measured or not, per repository: the
    # denominator the card states, and what a repository nobody has backfilled
    # yet still shows up in the per-repository table with.
    seen = {row['repo']: row['n'] for row in conn.execute(
        f"SELECT r.name AS repo, COUNT(*) AS n FROM {src['table']} "
        f"WHERE {where} GROUP BY r.name", params)}
    total = sum(seen.values())

    points: List[Dict[str, Any]] = []
    for row in rows:
        discussion = row['conversation'] + row['inline'] + row['review_bodies']
        points.append({
            'repo': row['repo'], 'number': row['number'],
            'title': row['title'], 'author': row['author'],
            'day': row['day'], 'merged': bool(row['merged']),
            'added': row['added'], 'removed': row['removed'],
            'lines': row['added'] + row['removed'],
            'files': row['files'],
            'conversation': row['conversation'], 'inline': row['inline'],
            'reviews': row['review_bodies'], 'discussion': discussion,
        })

    weekly = _weekly(f, [p['day'] for p in points])
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for point in points:
        grouped.setdefault(_bucket_of(point['day'], weekly), []).append(point)

    buckets = [{'bucket': key, **_pull_summary(grouped[key])}
               for key in sorted(grouped)]

    out = {
        'granularity': 'week' if weekly else 'month',
        'measured': len(points),
        'total': total,
        'unmeasured': max(0, total - len(points)),
        'truncated': max(0, len(points) - SCATTER_CAP),
        'buckets': buckets,
        # Newest first is what gets kept when there are more than the cap; the
        # list is re-sorted oldest-first so the client never has to care.
        'points': sorted(points[-SCATTER_CAP:], key=lambda p: p['day']),
        'totals': _pull_summary(points),
    }
    if per_repo:
        by_repo: Dict[str, List[Dict[str, Any]]] = {}
        for point in points:
            by_repo.setdefault(point['repo'], []).append(point)
        out['by_repo'] = sorted((
            {'repo': name, 'total': count,
             'unmeasured': count - len(by_repo.get(name, ())),
             **_pull_summary(by_repo.get(name, ()))}
            for name, count in seen.items()),
            key=lambda r: (-r['total'], r['repo']))
    return out
