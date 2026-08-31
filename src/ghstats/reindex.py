#!/usr/bin/env python3
"""Rebuild the derived tables from what the store already holds.

Offline, idempotent, and cheap. `commit_trailers`, `identities` and `ai_tools`
are all functions of `commits`, so a new AI assistant or a corrected identity
costs a reindex rather than a re-sync:

    ghstats-reindex                         # rebuild everything
    ghstats-reindex --unresolved            # list identities needing a human

This is the whole reason the full commit message is stored. Parse it at read
time, not at fetch time, and the classification can be changed retroactively
over history that has already been collected.
"""
import argparse
import re
import sqlite3
import sys
from typing import Dict, List, Tuple

from ghstats.store import sqlite as sqlite_store

DEFAULT_DB = '.cache/ghstats.db'

# A git trailer is `Token: Value` on its own line in the message's trailer
# block. Only co-authorship is extracted; conventional-commit prefixes
# (`feat:`, `fix:`) match the general trailer shape but are subject lines, not
# trailers, and carry no identity.
#
# Leading whitespace is tolerated. Four real commits indent the whole message
# body, and GitHub's own attribution would miss those -- but the question here
# is whether an assistant was involved, not how GitHub renders the byline, and
# indentation is a formatting artifact either way. Anchoring at column zero
# made the parser's answer depend on it.
CO_AUTHOR = re.compile(
    r'^[ \t]*co-authored-by:[ \t]*(?P<name>.*?)[ \t]*<(?P<email>[^>]*)>[ \t]*\r?$',
    re.MULTILINE | re.IGNORECASE,
)


# A candidate issue key: a letter-leading prefix, a hyphen, a number.
#
# Deliberately permissive, because the prefix is not what decides whether this
# is an issue -- `jira_projects` is. Over one real org's commit messages the
# pattern matched 132 distinct prefixes, and most were not issues at all
# (`ISO-8601`, `AES-256`, `PSR-4`, `ADR-001`). Filtering on a curated table
# instead of on the shape of the match is what keeps `HTTP-2` out and a
# misspelled-but-real key in.
#
# Matching is **case-insensitive**, which the whitelist makes safe: 533 keys in
# those messages were typed in lowercase (`inv-4549`), usually
# inside a branch name carried into a merge commit, and a case-sensitive
# pattern silently loses every one of them.
ISSUE_KEY = re.compile(r'\b([A-Za-z][A-Za-z0-9]{1,9})-([0-9]{1,6})\b')


def parse_trailers(message: str) -> List[Tuple[str, str]]:
    """Extract (name, lowercased email) for each co-author trailer."""
    return [(m.group('name'), m.group('email').lower())
            for m in CO_AUTHOR.finditer(message)]


def rebuild_trailers(conn) -> int:
    """Reparse every commit message into `commit_trailers`.

    Purely derived, so it is dropped and rebuilt rather than merged.
    """
    conn.execute('DELETE FROM commit_trailers')
    rows = []
    for repo_id, oid, message in conn.execute(
            'SELECT repo_id, oid, message FROM commits WHERE message != ""'):
        for name, email in parse_trailers(message):
            rows.append((repo_id, oid, name, email))
    # OR IGNORE: one commit may repeat an identical trailer, and the primary
    # key already says those are the same fact.
    conn.executemany(
        'INSERT OR IGNORE INTO commit_trailers (repo_id, oid, name, email) '
        'VALUES (?,?,?,?)', rows)
    return conn.execute('SELECT COUNT(*) FROM commit_trailers').fetchone()[0]


def rebuild_bot_logins(conn) -> Dict[str, int]:
    """Collect every automation login, in both spellings GitHub uses.

    Evidence-driven: any login seen with a `[bot]` suffix anywhere is an
    automation, and so is its bare form -- unless that bare form is an active
    organization member, which would mean it names a person.

    `BOT_LOGINS` is added unconditionally. It is hand-curated for accounts whose
    login gives them away in neither spelling (`semantic-release-bot`, or an
    org's own release automation). Some of those *are* org members, so the
    membership guard must not apply to it.
    """
    conn.execute('DELETE FROM bot_logins')
    suffixed = {r['login'] for r in conn.execute("""
        SELECT DISTINCT author_login AS login FROM commits
         WHERE author_login LIKE '%[bot]'
        UNION SELECT DISTINCT author_login FROM pulls
         WHERE author_login LIKE '%[bot]'
        UNION SELECT DISTINCT author_login FROM reviews
         WHERE author_login LIKE '%[bot]'""")}
    members = {r['login'] for r in conn.execute(
        'SELECT login FROM members WHERE active = 1')}

    rows = [(login, 'suffix') for login in sorted(suffixed)]
    for login in sorted(suffixed):
        bare = login[:-len('[bot]')]
        if bare and bare not in members:
            rows.append((bare, 'bare'))
    rows.extend((login, 'listed') for login in sorted(sqlite_store.BOT_LOGINS))

    conn.executemany(
        'INSERT OR IGNORE INTO bot_logins (login, source) VALUES (?,?)', rows)
    return {
        'total': conn.execute('SELECT COUNT(*) FROM bot_logins').fetchone()[0],
        'suffixed': len(suffixed),
    }


def load_projects(conn) -> Dict[str, str]:
    """Map an upper-cased key prefix to the project it belongs to."""
    return {r['key'].upper(): r['canonical']
            for r in conn.execute('SELECT key, canonical FROM jira_projects')}


def parse_issue_keys(text: str, projects: Dict[str, str]) -> List[Tuple[str, str]]:
    """Extract (canonical issue key, project) pairs from one message or title.

    Unknown prefixes are dropped rather than guessed at. Results are
    deduplicated but order is preserved, so the first key mentioned stays
    first -- for a commit that is usually the one it was written against.

    Args:
        text: A commit message or pull request title.
        projects: Prefix -> canonical project, from `load_projects`.

    Returns:
        Pairs of (`INV-4131`, `INV`).
    """
    found: List[Tuple[str, str]] = []
    seen = set()
    for match in ISSUE_KEY.finditer(text or ''):
        project = projects.get(match.group(1).upper())
        if project is None:
            continue
        # Numbers are normalised by int() so `INV-0042` and `INV-42` are one
        # issue; Jira does not pad, so the padded spelling is the typo.
        key = f'{project}-{int(match.group(2))}'
        if key not in seen:
            seen.add(key)
            found.append((key, project))
    return found


def seed_jira_projects(conn) -> int:
    """Install the known projects and their misspellings, leaving additions be.

    `INSERT OR IGNORE`, like `seed_ai_tools`: a project added by hand survives
    every future reindex.
    """
    conn.executemany(
        'INSERT OR IGNORE INTO jira_projects (key, canonical) VALUES (?,?)',
        sorted(sqlite_store.JIRA_PROJECT_SEED.items()))
    return conn.execute('SELECT COUNT(*) FROM jira_projects').fetchone()[0]


def rebuild_issue_refs(conn) -> Dict[str, int]:
    """Reparse commit messages and PR titles into `issue_refs`.

    Purely derived, so dropped and rebuilt. That is the point: adding a project
    to `jira_projects` and reindexing reclassifies twenty months of history
    without a single API call.
    """
    projects = load_projects(conn)
    conn.execute('DELETE FROM issue_refs')

    rows = []
    for repo_id, oid, message in conn.execute(
            'SELECT repo_id, oid, message FROM commits WHERE message != ""'):
        for key, project in parse_issue_keys(message, projects):
            rows.append(('commit', repo_id, oid, key, project))
    commits = len(rows)

    for repo_id, number, title in conn.execute(
            'SELECT repo_id, number, title FROM pulls WHERE title != ""'):
        for key, project in parse_issue_keys(title, projects):
            # `ref` is TEXT for both kinds; see the schema note on issue_refs.
            rows.append(('pull', repo_id, str(number), key, project))

    conn.executemany(
        'INSERT OR IGNORE INTO issue_refs '
        '(kind, repo_id, ref, issue_key, project) VALUES (?,?,?,?,?)', rows)
    return {
        'refs': conn.execute('SELECT COUNT(*) FROM issue_refs').fetchone()[0],
        'commit_refs': commits,
        'pull_refs': len(rows) - commits,
        'issues': conn.execute(
            'SELECT COUNT(DISTINCT issue_key) FROM issue_refs').fetchone()[0],
    }


def unknown_prefixes(conn, limit: int = 40) -> List[Tuple[str, int]]:
    """Key-shaped prefixes that resolve to no project, most frequent first.

    The counterpart to `--unresolved` for identities: when a new Jira project
    appears in commit messages it shows up here, and adopting it is one INSERT
    into `jira_projects` plus a reindex.

    Expect noise. `ISO`, `HTTP`, `AES` and `RFC` will always be near the top,
    because standards are written the same way issue keys are.
    """
    projects = load_projects(conn)
    counts: Dict[str, int] = {}
    sources = [
        'SELECT message AS text FROM commits WHERE message != ""',
        'SELECT title AS text FROM pulls WHERE title != ""',
    ]
    for sql in sources:
        for (text,) in conn.execute(sql):
            for match in ISSUE_KEY.finditer(text or ''):
                prefix = match.group(1).upper()
                if prefix not in projects:
                    counts[prefix] = counts.get(prefix, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:limit]


def is_bot_login(login: str) -> bool:
    """Classify a login as a bot account.

    **Not** a substring test on `bot`: a login can contain the substring and
    still be a person -- one such account had 2453 commits in the org this was
    built against. GitHub app accounts end in a bracketed `[bot]` suffix; the
    rest are named explicitly.
    """
    return login.endswith('[bot]') or login in sqlite_store.BOT_LOGINS


def seed_identities(conn) -> Dict[str, int]:
    """Fill `identities` from the (email -> login) mapping in `commits`.

    Verified as a function over this data: no email in 113,525 commits maps to
    two logins. Rows already present are left alone, so a hand correction
    survives every future reindex -- that is the intended way to attach the
    5971 commits whose author has no linked GitHub account.
    """
    pairs = conn.execute("""
        SELECT DISTINCT LOWER(author_email) AS email, author_login
        FROM commits
        WHERE author_email IS NOT NULL AND author_login IS NOT NULL
    """).fetchall()
    conn.executemany(
        'INSERT OR IGNORE INTO identities (email, canonical_login, is_bot) '
        'VALUES (?,?,?)',
        [(r['email'], r['author_login'], int(is_bot_login(r['author_login'])))
         for r in pairs])
    total = conn.execute('SELECT COUNT(*) FROM identities').fetchone()[0]
    bots = conn.execute(
        'SELECT COUNT(*) FROM identities WHERE is_bot = 1').fetchone()[0]
    return {'identities': total, 'bots': bots, 'seeded': len(pairs)}


def seed_ai_tools(conn) -> int:
    """Install the default AI trailer addresses, leaving additions intact."""
    conn.executemany(
        'INSERT OR IGNORE INTO ai_tools (email, tool) VALUES (?,?)',
        sorted(sqlite_store.AI_TOOL_SEED.items()))
    return conn.execute('SELECT COUNT(*) FROM ai_tools').fetchone()[0]


def unresolved(conn) -> List[sqlite3.Row]:
    """Author identities with no GitHub login, worst offender first.

    These are the commits that fall on the floor today. Each needs one row in
    `identities` saying which member the address belongs to.
    """
    return conn.execute("""
        SELECT LOWER(author_email) AS email,
               author_name,
               COUNT(*) AS commits
        FROM commits
        WHERE author_login IS NULL AND author_email IS NOT NULL
          AND LOWER(author_email) NOT IN (SELECT email FROM identities)
        GROUP BY 1, 2
        ORDER BY commits DESC
    """).fetchall()


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Rebuild derived tables from the stored commits.')
    parser.add_argument('--db', default=DEFAULT_DB)
    parser.add_argument('--unresolved', action='store_true',
                        help='List author identities awaiting a hand mapping '
                             'and exit without rebuilding')
    parser.add_argument('--unknown-issues', action='store_true',
                        help='List issue-key prefixes that match no project '
                             'and exit without rebuilding')
    args = parser.parse_args()

    # `create=False`: the reindex derives tables from commits the sync fetched,
    # so an absent store is a missing prerequisite, not something to conjure.
    try:
        conn = sqlite_store.connect(args.db, create=False)
    except (ValueError, sqlite3.Error) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    try:
        if args.unresolved:
            rows = unresolved(conn)
            total = sum(r['commits'] for r in rows)
            print(f'{len(rows)} unresolved identities, {total:,} commits:\n')
            for r in rows:
                print(f'  {r["commits"]:>6,}  {r["author_name"] or "?":<28} '
                      f'{r["email"]}')
            print('\nMap one with:\n  INSERT INTO identities '
                  '(email, canonical_login) VALUES (\'<email>\', \'<login>\');')
            return 0

        if args.unknown_issues:
            rows = unknown_prefixes(conn)
            print(f'{len(rows)} unrecognised key prefixes:\n')
            for prefix, count in rows:
                print(f'  {count:>6,}  {prefix}')
            print('\nAdopt one with:\n  INSERT INTO jira_projects '
                  "(key, canonical) VALUES ('<PREFIX>', '<PROJECT>');")
            return 0

        with conn:
            trailers = rebuild_trailers(conn)
            ident = seed_identities(conn)
            tools = seed_ai_tools(conn)
            # Projects are seeded before the refs are parsed, because the refs
            # are filtered through them.
            projects = seed_jira_projects(conn)
            issues = rebuild_issue_refs(conn)
            bots = rebuild_bot_logins(conn)

        print(f'commit_trailers  {trailers:>8,}')
        print(f'identities       {ident["identities"]:>8,}  '
              f'({ident["bots"]} bots)')
        print(f'ai_tools         {tools:>8,}')
        print(f'jira_projects    {projects:>8,}')
        print(f'bot_logins       {bots["total"]:>8,}  '
              f'({bots["suffixed"]} seen with a [bot] suffix)')
        print(f'issue_refs       {issues["refs"]:>8,}  '
              f'({issues["issues"]:,} issues: {issues["commit_refs"]:,} from '
              f'commits, {issues["pull_refs"]:,} from PRs)')

        rows = conn.execute("""
            SELECT tool, COUNT(DISTINCT repo_id || oid) AS commits,
                   COUNT(*) AS trailers
            FROM v_commit_ai GROUP BY tool ORDER BY commits DESC""").fetchall()
        print('\nAI-assisted commits:')
        for r in rows:
            print(f'  {r["tool"]:<16} {r["commits"]:>7,} commits '
                  f'({r["trailers"]:,} trailers)')

        top = conn.execute("""
            SELECT project, COUNT(DISTINCT issue_key) AS issues,
                   COUNT(*) AS refs
            FROM issue_refs GROUP BY project
            ORDER BY refs DESC LIMIT 10""").fetchall()
        if top:
            print('\nBusiest Jira projects:')
            for r in top:
                print(f'  {r["project"]:<16} {r["issues"]:>5,} issues '
                      f'({r["refs"]:,} references)')

        pending = len(unresolved(conn))
        if pending:
            print(f'\n{pending} author identities still unresolved; '
                  f'see `ghstats-reindex --unresolved`')
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
