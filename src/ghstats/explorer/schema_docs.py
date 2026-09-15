"""What each table, column, function and parameter in the store means.

Served to the SQL page's schema panel. Names and types come from the store
itself (`describe`); this module adds the sentence that says what a value
*counts*, which no amount of `PRAGMA table_info` can.

**Complete by test.** `tests/test_sql.py` fails when a real column has no
description here, or a description names a column that no longer exists, so a
schema migration cannot land without saying what it added. The long-form
reasoning stays next to the DDL in `store/sqlite.py`; these are the one-line
versions, plus the traps a query writer walks into.
"""
import sqlite3
from typing import Any, Dict, List

# Table -> (what a row is, {column: meaning}). Ordered the way someone new to
# the store should read it: the event tables first, then who and where, then
# what is derived.
TABLES: Dict[str, Any] = {
    'commits': (
        'One commit in one repository. Lines here are commit lines, not PR '
        'lines. Keyed on (repo_id, oid): a commit in a fork and its parent is '
        'two rows.',
        {
            'repo_id': 'Repository, `repos.id`.',
            'oid': 'Commit SHA.',
            'author_login': 'GitHub login of the author; NULL when the email '
                            'is linked to no account. Bots end in `[bot]`.',
            'author_name': 'Author name as written in the commit.',
            'author_email': 'Author email as written in the commit.',
            'committed_date': 'When committed, UTC. The date the explorer '
                              'files a commit under.',
            'authored_date': 'When authored, UTC; earlier than committed_date '
                             'after a rebase.',
            'additions': 'Lines added by this commit.',
            'deletions': 'Lines removed by this commit.',
            'message': 'Full commit message, trailers included.',
        },
    ),
    'pulls': (
        'One pull request. It is two events in the explorer: opened '
        '(created_at) and merged (merged_at). Size and discussion live in '
        '`pull_metrics`.',
        {
            'repo_id': 'Repository, `repos.id`.',
            'number': 'PR number within the repository.',
            'author_login': 'Login of the author. Bots appear bare here '
                            '(`renovate`, not `renovate[bot]`).',
            'title': 'PR title.',
            'state': "'OPEN', 'CLOSED' (unmerged) or 'MERGED'.",
            'created_at': 'When opened, UTC. What the PR size card filters '
                          'the window on.',
            'updated_at': 'Last activity on GitHub when synced, UTC.',
            'merged_at': 'When merged, UTC; NULL unless merged.',
            'closed_at': 'When closed or merged, UTC; NULL while open.',
        },
    ),
    'pull_metrics': (
        'Size and discussion of one measured pull request. No row means '
        'unmeasured, never zero: join, do not LEFT JOIN and COALESCE. '
        '`ghstats-backfill-pulls` fills the gaps.',
        {
            'repo_id': 'Repository, `repos.id`.',
            'number': 'PR number, `pulls.number`.',
            'additions': 'Lines added across the PR.',
            'deletions': 'Lines removed across the PR. Lines changed is '
                         'additions + deletions.',
            'changed_files': 'Files touched.',
            'comments': 'Conversation-tab comments, from anyone, bots '
                        'included.',
            'review_comments': 'Inline review comments, from anyone, bots '
                               'included.',
            'measured_at': 'When these numbers were fetched, UTC.',
        },
    ),
    'reviews': (
        'One submitted review. An approval with an empty body is a review but '
        'not discussion: the cards count only reviews whose body is not blank.',
        {
            'id': 'GitHub node id of the review.',
            'repo_id': 'Repository, `repos.id`.',
            'pull_number': 'PR reviewed, `pulls.number`.',
            'author_login': 'Reviewer login. Review-only automations '
                            '(`cursor`, `claude`) are bare and in `bot_logins`.',
            'submitted_at': 'When submitted, UTC.',
            'state': "'APPROVED', 'CHANGES_REQUESTED', 'COMMENTED' or "
                     "'DISMISSED'.",
            'body': "Review summary text; '' for a bare approval.",
        },
    ),
    'repos': (
        'A repository of the organization.',
        {
            'id': 'Internal id every other table joins on.',
            'org': 'Organization login.',
            'name': 'Repository name, without the organization.',
        },
    ),
    'coverage': (
        'The window of history synced for each repository and kind. Outside '
        'it the store knows nothing, which is not the same as nothing '
        'happened.',
        {
            'repo_id': 'Repository, `repos.id`.',
            'kind': "'commits' or 'pulls'.",
            'covered_from': 'Start of the synced window, UTC.',
            'covered_to': 'End of the synced window, UTC.',
        },
    ),
    'members': (
        'Organization members, kept across departures rather than forgotten.',
        {
            'login': 'Member login.',
            'first_seen': 'First sync that found them in the organization.',
            'last_seen': 'Most recent sync that found them.',
            'active': '1 if in the organization at the last sync, 0 if they '
                      'left.',
        },
    ),
    'teams': (
        'GitHub teams. Membership is current only: a January commit is '
        'attributed to the team its author is on today.',
        {
            'slug': 'Team slug.',
            'name': 'Display name.',
            'description': 'Team description on GitHub.',
            'parent_slug': 'Parent team slug; NULL at the top level.',
            'first_seen': 'First sync that found the team.',
            'last_seen': 'Most recent sync that found the team.',
            'active': '1 if the team still exists, 0 if it was removed.',
        },
    ),
    'team_members': (
        'Who is on which team, as of the last sync.',
        {
            'team_slug': 'Team, `teams.slug`.',
            'login': 'Member login.',
            'role': "'MEMBER' or 'MAINTAINER'.",
            'first_seen': 'First sync that found them on the team.',
            'last_seen': 'Most recent sync that found them on the team.',
            'active': '1 if still on the team, 0 if they left it.',
        },
    ),
    'team_repos': (
        'Repositories a team has access to, which is not the same as the ones '
        'it works in. Keyed by name: join on `repos.name`, not an id.',
        {
            'team_slug': 'Team, `teams.slug`.',
            'repo_name': 'Repository name; may name one the store does not '
                         'sync, such as an archived repository.',
            'permission': "Access level, e.g. 'WRITE', 'ADMIN'.",
            'active': '1 if the grant still exists, 0 if revoked.',
        },
    ),
    'bot_logins': (
        'Derived: every automation login, in both spellings GitHub uses. The '
        'cards exclude `author_login NOT IN (SELECT login FROM bot_logins)` '
        'unless bots are shown.',
        {
            'login': 'The automation login.',
            'source': "'suffix' (seen as name[bot]), 'bare' (the same account "
                      "without the suffix) or 'listed' (curated in code).",
        },
    ),
    'commit_trailers': (
        'Derived: Co-authored-by trailers parsed from commit messages. A '
        'commit can have several.',
        {
            'repo_id': 'Repository, `repos.id`.',
            'oid': 'Commit SHA, `commits.oid`.',
            'name': 'Co-author name as written.',
            'email': 'Co-author email, lowercased. What `ai_tools` matches on.',
        },
    ),
    'ai_tools': (
        'Trailer emails that identify an AI assistant. Matched on email, never '
        'name.',
        {
            'email': 'Trailer email, lowercased.',
            'tool': "The assistant it identifies, e.g. 'Claude'.",
        },
    ),
    'v_commit_ai': (
        'View: one row per commit and AI tool. A commit with two tool trailers '
        'is two rows, so count commits with COUNT(DISTINCT repo_id || oid).',
        {
            'repo_id': 'Repository, `repos.id`.',
            'oid': 'Commit SHA.',
            'author_login': 'Commit author login.',
            'author_email': 'Commit author email.',
            'committed_date': 'When committed, UTC.',
            'additions': 'Lines added by the commit.',
            'deletions': 'Lines removed by the commit.',
            'tool_email': 'The trailer email that matched.',
            'tool': 'The assistant.',
        },
    ),
    'identities': (
        'Derived: commit author email to GitHub login, for commits whose login '
        'is missing. Hand corrections survive a reindex.',
        {
            'email': 'Author email, lowercased.',
            'canonical_login': 'The login that email belongs to.',
            'is_bot': '1 if the login is an automation.',
        },
    ),
    'jira_projects': (
        'Curated: issue-key prefixes that are real Jira projects, including '
        'misspellings mapped onto the project they meant.',
        {
            'key': 'Prefix as written, e.g. ORB or a typo like BILLNIG.',
            'canonical': 'The project it means; equal to key unless a typo.',
            'name': 'Project name, if recorded.',
        },
    ),
    'issue_refs': (
        'Derived: issue keys found in commit messages and PR titles. `ref` is '
        'text for both kinds; join pulls on CAST(ref AS INTEGER).',
        {
            'kind': "'commit' or 'pull'.",
            'repo_id': 'Repository, `repos.id`.',
            'ref': "commits.oid when kind is 'commit', the PR number as text "
                   "when 'pull'.",
            'issue_key': 'Canonical issue key, e.g. ORB-42.',
            'project': 'Canonical project, `jira_projects.canonical`.',
        },
    ),
    'sync_runs': (
        'One row per `ghstats-sync` run.',
        {
            'id': 'Run id.',
            'started_at': 'When the run started, UTC.',
            'finished_at': 'When it finished, UTC; NULL if it did not.',
            'repos_synced': 'Repositories synced.',
            'repos_failed': 'Repositories that failed.',
            'points': 'GitHub GraphQL rate-limit points spent.',
            'seconds': 'Wall-clock duration.',
            'complete': '1 for a full sweep, 0 for a partial one (--repos, '
                        '--limit, --skip-*).',
        },
    ),
}

FUNCTIONS: List[Dict[str, str]] = [
    {'name': 'local_date', 'signature': 'local_date(ts)',
     'about': 'The date of a UTC timestamp in :tz, as YYYY-MM-DD. What the '
              'cards group days by.'},
    {'name': 'local_hour', 'signature': 'local_hour(ts)',
     'about': 'The hour 0-23 of a UTC timestamp in :tz.'},
    {'name': 'local_dow', 'signature': 'local_dow(ts)',
     'about': "The weekday name of a UTC timestamp in :tz, e.g. 'Monday'."},
    {'name': 'median', 'signature': 'median(x)',
     'about': 'Aggregate: the middle value, or the mean of the middle two. '
              'NULLs are skipped; NULL for no rows. The cards show 0 there.'},
]

PARAMETERS: Dict[str, str] = {
    'from': 'Start of the date filter as a UTC instant, inclusive; NULL for '
            'no start. Compare with >=.',
    'to': 'End of the date filter as a UTC instant, exclusive; NULL for no '
          'end. Compare with <.',
    'tz': 'The timezone the date filter and local_* functions use.',
    'repo': 'Repository name, or NULL.',
    'user': 'Login, or NULL.',
    'bots': '1 when the bots toggle is on, else 0. The cards skip '
            '`bot_logins` unless it is 1.',
}


def describe(conn: sqlite3.Connection) -> Dict[str, Any]:
    """The store's tables and views, with their columns, and what each means.

    Structure comes from `sqlite_master`, so a table this module has not heard
    of still appears (with no description) rather than hiding.
    """
    objects = {r[0]: r[1] for r in conn.execute(
        "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') "
        "AND name NOT LIKE 'sqlite_%'")}
    order = [n for n in TABLES if n in objects] + \
        sorted(n for n in objects if n not in TABLES)

    tables = []
    for name in order:
        about, columns = TABLES.get(name, ('', {}))
        rows = conn.execute(
            'SELECT name, type, pk FROM pragma_table_info(?) ORDER BY cid',
            (name,)).fetchall()
        tables.append({
            'name': name,
            'kind': objects[name],
            'about': about,
            'columns': [{'name': r[0], 'type': r[1], 'pk': bool(r[2]),
                         'about': columns.get(r[0], '')} for r in rows],
        })
    return {
        'tables': tables,
        'functions': FUNCTIONS,
        'parameters': [{'name': n, 'about': a} for n, a in PARAMETERS.items()],
    }
