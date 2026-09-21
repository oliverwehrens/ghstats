"""SQLite schema and connection handling for the activity store.

Replaces the per-repo JSON cache of `store.json_cache`. The invariant is
unchanged: every repository records the window it actually holds, and a read
that asks for more than that fails rather than under-reporting.

**Trap: timestamps must be one canonical spelling and one precision.** They are
stored as ISO-8601 UTC text ending in `Z`, at whole-second precision, because
SQLite compares TEXT lexically and both variations break that comparison while
looking harmless:

    '2025-01-01T00:00:00Z'        > '2025-01-01T00:00:00+00:00'   -> 1
    '2026-08-17T13:48:43.869494Z' > '2026-08-17T13:48:43Z'        -> 0

Both are the wrong answer for the same instant. The JSON cache mixed exactly
these: GraphQL emits `Z` at second precision for every event, while
`cache_store.iso()` wrote `+00:00` for watermarks and gave `covered_to`
microseconds. `canonical_ts` flattens both. `strftime` and `julianday` cope
with any of these spellings, which is what makes the bug quiet -- only range
comparisons are wrong, and only sometimes.
"""
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 6

DEFAULT_DB = '.cache/ghstats.db'

# Trailer emails that identify an AI assistant. Seeded into `ai_tools`, which
# is a table rather than a constant so that adding a tool is an INSERT and a
# reindex -- never a code change, and never a re-sync.
#
# Matching is on **email**, never display name: `noreply@anthropic.com` arrives
# under at least eleven names (`Claude`, `Claude Opus 4.8 (1M context)`,
# `Claude Fable 5`, ...) and a new one appears with every model release.
AI_TOOL_SEED = {
    'noreply@anthropic.com': 'Claude',
    'cursoragent@cursor.com': 'Cursor',
    '175728472+copilot@users.noreply.github.com': 'GitHub Copilot',
}

# Observed key prefix -> the canonical project it belongs to. Seeded into
# `jira_projects`, a table rather than a constant for the same reason
# `ai_tools` is one: a new project is an INSERT plus a reindex.
#
# **Ships empty on purpose.** Issue keys are specific to one organization, so
# there is no useful default. Populate it for yours, either here or straight
# into the table:
#
#   INSERT INTO jira_projects (key, canonical) VALUES ('ORB','ORB');
#
# `ghstats-reindex --unknown-issues` ranks the key-shaped prefixes in your own
# commit messages that no project claims yet, which is where the list comes
# from. Expect two traps while building it.
#
# **Trap: this cannot be an open regex.** `[A-Z]{2,}-[0-9]+` matches a great
# many distinct prefixes across a real corpus of commit messages, and most are
# not issues at all -- `ISO-8601`, `HTTP-2`, `AES-256`, `RFC-3339`, `PSR-4`,
# `ADR-001`, `R2025-...`, `P1-...`. Extracting
# those invents a hundred phantom projects, each with a handful of "issues"
# that never existed. The whitelist is what makes the permissive pattern safe.
#
# **Trap: one project arrives under several spellings.** A fat-fingered key
# lands in a commit message and stays there forever: a transposition, a dropped
# first letter, a zero for an O. Map the misspelling onto what it meant rather
# than rewriting history, and the two spellings become one issue in the
# explorer -- which is what they are in Jira:
#
#   INSERT INTO jira_projects (key, canonical) VALUES ('BILLNIG','BILLING');
JIRA_PROJECT_SEED: Dict[str, str] = {}

# Bot accounts whose login does not end in `[bot]`.
#
# **Trap:** the test cannot be `login LIKE '%bot%'`. In the organization this
# was built against that pattern caught one of its most prolific human
# contributors, whose login merely contains the substring; they would be
# classified as a bot and dropped from every human metric.
#
# `ghstats-reindex` derives most automations from evidence -- any login seen with
# a `[bot]` suffix, plus its bare form -- so this list only needs the accounts
# that evidence cannot reach. Two shapes end up here:
#
# - CI identities that commit under an ordinary-looking name
#   (`semantic-release-bot`, or your own release automation).
# - **Review-only automations, which never appear bracketed at all.** The code
#   review apps author reviews and nothing else, so there is no suffixed
#   spelling anywhere in the store to derive the bare one from. Together they
#   accounted for a sizeable share of all reviews in the organization this was
#   built against, and every one would otherwise be counted as a person
#   reviewing code.
#
# To find new ones: non-member logins with reviews but no commits.
#
#   SELECT v.author_login, COUNT(*) FROM reviews v
#    WHERE v.author_login NOT IN (SELECT login FROM members)
#      AND v.author_login NOT IN (SELECT login FROM bot_logins)
#      AND NOT EXISTS (SELECT 1 FROM commits c
#                       WHERE c.author_login = v.author_login)
#    GROUP BY 1 ORDER BY 2 DESC;
#
# Read the result before adding anything: the same query also surfaces people
# who left the organization, whose reviews are real.
#
# These are the automations common to any organization. Your own CI identities
# -- release bots, service accounts, anything committing under an ordinary
# looking name -- belong here too.
BOT_LOGINS = frozenset({
    'semantic-release-bot',
    'goreleaserbot',
    # Review-only automations. Identified from their review bodies:
    'cursor',                          # Cursor Bugbot
    'copilot-pull-request-reviewer',   # GitHub Copilot review
    'renovate-approve',                # Renovate auto-approve
    'claude',                          # Claude Code review
})

# Repositories known never to sync, mapped to why.
#
# A repository with no coverage row is a hard error: it contributes zero to
# every report and, before this list existed, said nothing about it. But an
# error nobody can clear is an error everybody learns to ignore, so a repo that
# genuinely cannot be fetched is recorded here rather than left to fail nightly.
#
# Keep this list short and justified. Anything on it is data the reports do not
# have, which is the thing the coverage invariant exists to make visible.
#
# Ships empty: which repositories are unfetchable is a property of your
# organization, not of this tool. `ghstats-inactive` names the offenders when it
# refuses, and the entry looks like:
#
#   'some-repo':
#       'commit query returns HTTP 502 on every attempt, at any batch size; '
#       'has never synced successfully and holds zero commits',
UNSYNCABLE_REPOS: Dict[str, str] = {}


# Repos, coverage, and the three event tables mirror what the JSON cache held.
# `commit_trailers` and `identities` are derived -- rebuildable offline from
# `commits.message`, never fetched. That is what makes a new AI tool a one-line
# change instead of a re-sync.
SCHEMA = """
CREATE TABLE repos (
  id    INTEGER PRIMARY KEY,
  org   TEXT NOT NULL,
  name  TEXT NOT NULL,
  UNIQUE (org, name)
);

CREATE TABLE coverage (
  repo_id      INTEGER NOT NULL REFERENCES repos(id),
  kind         TEXT    NOT NULL CHECK (kind IN ('commits','pulls')),
  covered_from TEXT    NOT NULL,
  covered_to   TEXT    NOT NULL,
  PRIMARY KEY (repo_id, kind)
) WITHOUT ROWID;

-- Keyed on (repo_id, oid), never oid alone: a great many commits live in two
-- repos each wherever one repository is a fork of another. A bare oid primary
-- key silently discards one copy.
CREATE TABLE commits (
  repo_id        INTEGER NOT NULL REFERENCES repos(id),
  oid            TEXT    NOT NULL,
  author_login   TEXT,
  author_name    TEXT,
  author_email   TEXT,
  committed_date TEXT    NOT NULL,
  authored_date  TEXT,
  additions      INTEGER NOT NULL DEFAULT 0,
  deletions      INTEGER NOT NULL DEFAULT 0,
  message        TEXT    NOT NULL DEFAULT '',
  PRIMARY KEY (repo_id, oid)
);

CREATE TABLE pulls (
  repo_id      INTEGER NOT NULL REFERENCES repos(id),
  number       INTEGER NOT NULL,
  author_login TEXT,
  title        TEXT    NOT NULL DEFAULT '',
  state        TEXT    NOT NULL CHECK (state IN ('OPEN','CLOSED','MERGED')),
  created_at   TEXT    NOT NULL,
  updated_at   TEXT,
  merged_at    TEXT,
  closed_at    TEXT,
  PRIMARY KEY (repo_id, number)
);

CREATE TABLE reviews (
  id           TEXT    PRIMARY KEY,
  repo_id      INTEGER NOT NULL,
  pull_number  INTEGER NOT NULL,
  author_login TEXT,
  submitted_at TEXT,
  state        TEXT    NOT NULL
                 CHECK (state IN ('APPROVED','CHANGES_REQUESTED',
                                  'COMMENTED','DISMISSED')),
  body         TEXT    NOT NULL DEFAULT '',
  FOREIGN KEY (repo_id, pull_number) REFERENCES pulls(repo_id, number)
);

CREATE TABLE members (
  login      TEXT PRIMARY KEY,
  first_seen TEXT NOT NULL,
  last_seen  TEXT NOT NULL,
  active     INTEGER NOT NULL DEFAULT 1
);

-- One row per run, appended. The JSON state file held only the most recent
-- run and overwrote it nightly.
--
-- `complete` distinguishes a full sweep from a partial one (`--repos`,
-- `--limit`, any `--skip-*`). Without it a members-only refresh would look
-- like fresh coverage of the whole organization, which is exactly what the
-- old `last_successful_run` field existed to prevent.
CREATE TABLE sync_runs (
  id           INTEGER PRIMARY KEY,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  repos_synced INTEGER,
  repos_failed INTEGER,
  points       INTEGER,
  seconds      REAL,
  complete     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE commit_trailers (
  repo_id INTEGER NOT NULL,
  oid     TEXT    NOT NULL,
  name    TEXT    NOT NULL,
  email   TEXT    NOT NULL,
  PRIMARY KEY (repo_id, oid, email, name),
  FOREIGN KEY (repo_id, oid) REFERENCES commits(repo_id, oid)
) WITHOUT ROWID;

CREATE TABLE identities (
  email           TEXT PRIMARY KEY,   -- lowercased
  canonical_login TEXT NOT NULL,
  is_bot          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE ai_tools (
  email TEXT PRIMARY KEY,             -- lowercased trailer address
  tool  TEXT NOT NULL
);

-- One row per (commit, AI tool) pair. A commit can carry several: a session
-- that spans two models leaves two trailers, so Claude-assisted commits carry
-- appreciably more trailers than there are commits. COUNT(DISTINCT oid) when
-- counting commits.
CREATE VIEW v_commit_ai AS
SELECT c.repo_id, c.oid, c.author_login, c.author_email,
       c.committed_date, c.additions, c.deletions,
       t.email AS tool_email, a.tool
FROM commits c
JOIN commit_trailers t ON t.repo_id = c.repo_id AND t.oid = c.oid
JOIN ai_tools a        ON a.email   = t.email;

CREATE INDEX ix_commits_author_date ON commits (author_login, committed_date);
CREATE INDEX ix_commits_repo_date   ON commits (repo_id, committed_date);
CREATE INDEX ix_reviews_author_date ON reviews (author_login, submitted_at);
CREATE INDEX ix_reviews_pull        ON reviews (repo_id, pull_number);
CREATE INDEX ix_pulls_author_date   ON pulls (author_login, created_at);
CREATE INDEX ix_trailers_email      ON commit_trailers (email);
"""


# Tables added in schema 4, kept in one constant because both a fresh install
# and the v3 -> v4 migration apply them. Defining them twice is how a migrated
# store and a new one drift apart; `tests/test_migrations.py` asserts they do
# not.
#
# All additive: no existing table is altered and no data is rewritten, so a
# store already on disk upgrades in place rather than needing a re-sync.
V4_TABLES = """
-- GitHub teams, sweep-maintained like `members`.
--
-- **Trap: membership is current, not historical.** GitHub's API reports who is
-- on a team today, with no history, so grouping a commit from January by its
-- author's team attributes it to whatever team they are on now. For "what did
-- this team change last week" that is right; for a year-long trend across a
-- reorg it is not, and the explorer says so rather than pretending otherwise.
CREATE TABLE teams (
  slug        TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  description TEXT,
  parent_slug TEXT,
  first_seen  TEXT NOT NULL,
  last_seen   TEXT NOT NULL,
  active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE team_members (
  team_slug  TEXT NOT NULL REFERENCES teams(slug),
  login      TEXT NOT NULL,
  role       TEXT,
  first_seen TEXT NOT NULL,
  last_seen  TEXT NOT NULL,
  active     INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (team_slug, login)
) WITHOUT ROWID;

-- Which repositories a team has access to. Not the same as which it commits
-- to, and useful precisely because of the difference.
--
-- **Trap: keyed on repo *name*, with no foreign key into `repos`.** The
-- obvious design resolves each grant through `repo_id()`, which inserts the
-- repository if absent -- and team grants reach archived repositories the
-- sweep deliberately skips, so on a real organization that silently added
-- hundreds of rows to `repos` with no `coverage`. `coverage_summary` cross-joins
-- every repository against every kind, `unusable_pairs` turns a missing pair into a
-- hard error, and every window-taking command therefore refuses for every
-- member. Storing the name records what GitHub said without asserting the
-- repository is part of the tracked set.
CREATE TABLE team_repos (
  team_slug  TEXT NOT NULL REFERENCES teams(slug),
  repo_name  TEXT NOT NULL,
  permission TEXT,
  active     INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (team_slug, repo_name)
) WITHOUT ROWID;

-- Derived: every login that is an automation, in both spellings GitHub uses.
--
-- **Trap: one bot arrives under two logins.** GraphQL resolves commit
-- authorship to the account record, which carries the bracketed suffix
-- (`renovate[bot]`), but `PullRequest.author` and `PullRequestReview.author`
-- return the bare handle (`renovate`). A filter written as
-- `login NOT LIKE '%[bot]'` therefore drops bot commits and keeps every bot
-- pull request -- and in the organization this was built against Renovate
-- opened more PRs than any human, so the miss is not a rounding error.
--
-- Rebuilt by `ghstats-reindex` from evidence rather than hardcoded, so a new
-- automation is picked up by the next reindex. The bare form is only adopted
-- when it is not an active member, which is what keeps a hypothetical human
-- `renovate` out of it -- and what already protects a person whose login
-- happens to contain `bot` and who is never seen bracketed.
CREATE TABLE bot_logins (
  login  TEXT PRIMARY KEY,
  source TEXT NOT NULL          -- 'suffix' | 'bare' | 'listed'
);

-- Curated. `key` is the prefix as written, `canonical` the project it means.
-- For a correctly spelled project the two are equal; for a typo they differ.
CREATE TABLE jira_projects (
  key       TEXT PRIMARY KEY,
  canonical TEXT NOT NULL,
  name      TEXT
);

-- Derived: issue keys parsed out of commit messages and PR titles. Rebuilt by
-- `ghstats-reindex`, never fetched, so correcting `jira_projects` reclassifies
-- history already collected.
--
-- **Trap: `ref` is TEXT for both kinds.** It holds `commits.oid` when kind is
-- 'commit' and a stringified `pulls.number` when kind is 'pull', because a
-- WITHOUT ROWID primary key cannot hold the NULLs that two typed columns would
-- need. Joins to `pulls` must therefore cast:
--
--     JOIN pulls p ON p.repo_id = i.repo_id AND p.number = CAST(i.ref AS INTEGER)
CREATE TABLE issue_refs (
  kind      TEXT    NOT NULL CHECK (kind IN ('commit','pull')),
  repo_id   INTEGER NOT NULL REFERENCES repos(id),
  ref       TEXT    NOT NULL,
  issue_key TEXT    NOT NULL,
  project   TEXT    NOT NULL,
  PRIMARY KEY (kind, repo_id, ref, issue_key)
) WITHOUT ROWID;

CREATE INDEX ix_issue_refs_key     ON issue_refs (issue_key);
CREATE INDEX ix_issue_refs_project ON issue_refs (project, kind);
CREATE INDEX ix_team_members_login ON team_members (login, active);
"""

# Tables added in schema 5, applied by both a fresh install and the v4 -> v5
# step, for the same reason `V4_TABLES` is.
V5_TABLES = """
-- Size and discussion volume for one pull request.
--
-- **A side table rather than columns on `pulls`, on purpose.** The migration
-- ladder is additive -- new tables only -- so a twenty-month store gains this
-- instantly instead of rewriting every pull row. It also keeps the distinction
-- that matters operationally: a `pulls` row means "this PR was seen", a
-- `pull_metrics` row means "its size and discussion were measured". A PR
-- synced before this table existed has the first and not the second, and
-- `ghstats-backfill-pulls` is what closes the gap. Reads must therefore treat
-- a missing row as *unmeasured*, never as zero -- a LEFT JOIN that coalesces
-- to 0 would draw twenty months of un-backfilled history as a flat line of
-- silent, wrong zeroes, which is exactly the shape a real quiet period has.
--
-- **Trap: `comments` and `review_comments` are different connections.**
-- GitHub splits PR discussion three ways and no single field totals them:
--
--   PullRequest.comments        the conversation tab -- issue comments
--   PullRequestReview.comments  inline comments, hanging off a review
--   PullRequestReview (itself)  the submission, whose body may be empty
--
-- An approve click is a review with no body and no comments, and counting it
-- as discussion makes every rubber-stamped PR look debated. `discussion` in
-- the explorer is comments + review_comments + reviews-with-a-body; the three
-- are stored apart so that definition can change without a re-fetch.
CREATE TABLE pull_metrics (
  repo_id         INTEGER NOT NULL,
  number          INTEGER NOT NULL,
  additions       INTEGER NOT NULL DEFAULT 0,
  deletions       INTEGER NOT NULL DEFAULT 0,
  changed_files   INTEGER NOT NULL DEFAULT 0,
  comments        INTEGER NOT NULL DEFAULT 0,
  review_comments INTEGER NOT NULL DEFAULT 0,
  measured_at     TEXT    NOT NULL,
  PRIMARY KEY (repo_id, number),
  FOREIGN KEY (repo_id, number) REFERENCES pulls(repo_id, number)
) WITHOUT ROWID;
"""

# Tables added in schema 6, applied by both a fresh install and the v5 -> v6
# step, for the same reason `V4_TABLES` is.
V6_TABLES = """
-- SonarCloud's view of a repository: does it have a project, did the quality
-- gate pass, when was it last analysed.
--
-- **Written by `ghstats-sonar` alone, never by the GitHub sweep.** Sonar is a
-- second network source with its own credentials and its own failure modes,
-- and the two syncs share no rows precisely so that either can be re-run
-- without risk to the other's data. A gate status goes stale in hours; a
-- commit sweep takes minutes and costs API budget. Tying them together would
-- make the cheap refresh hostage to the expensive one.
--
-- **Keyed on the Sonar project, not the repository, and `repo_name` carries no
-- foreign key into `repos`** -- the same shape as `team_repos`, for the same
-- reason. A Sonar organization contains projects that correspond to no tracked
-- repository at all (archived, renamed, or belonging to another org), and
-- resolving those through `repo_id` would insert coverage-less rows into
-- `repos` and break every window-taking command. Storing the name records what
-- SonarCloud said without asserting the repository is part of the tracked set.
--
-- A row whose `repo_name` is NULL is a Sonar project nothing matched. It is
-- kept rather than discarded, because it is the evidence you need when the key
-- convention turns out not to be the one assumed.
--
-- **Trap: `gate_status` has four values, and `NONE` is not `ERROR`.**
-- SonarCloud reports `OK`, `ERROR`, `WARN` or `NONE`, where `NONE` means the
-- project exists but has no gate result yet -- a project created but never
-- analysed. Folding it into `ERROR` would report a failing gate for a project
-- that has never run, which is a different and much more alarming claim.
-- **Trap: `NOT NULL` on the primary key is not redundant.** SQLite permits
-- NULLs in a TEXT PRIMARY KEY on a rowid table -- a long-standing quirk kept
-- for backwards compatibility -- so a malformed row would insert silently and
-- then match nothing for the rest of the store's life.
CREATE TABLE sonar_projects (
  project_key   TEXT PRIMARY KEY NOT NULL,
  name          TEXT,
  repo_name     TEXT,            -- matched GitHub repo, NULL if none matched
  match_rule    TEXT,            -- 'prefixed' | 'bare' | 'suffix' | NULL
  last_analysis TEXT,            -- ISO-8601 UTC, NULL if never analysed
  gate_status   TEXT,            -- 'OK' | 'ERROR' | 'WARN' | 'NONE' | NULL
  fetched_at    TEXT NOT NULL
);

CREATE INDEX ix_sonar_projects_repo ON sonar_projects (repo_name);

-- One row per `ghstats-sonar` run, appended.
--
-- **Separate from `sync_runs` on purpose.** A successful Sonar run that
-- matches nothing leaves `sonar_projects` empty, which is indistinguishable
-- from never having run -- and the explorer must tell those apart, because the
-- first means "no repository here has a Sonar project" and the second means
-- "nobody has asked Sonar yet". Only a run record can carry that difference.
--
-- It is not a `kind` column on `sync_runs` because the two runs share no
-- measurements: `points` and `repos_failed` are meaningless for Sonar, and
-- `meta()` reads the newest complete `sync_runs` row to draw the GitHub
-- freshness banner. Mixing Sonar rows into that table would move the banner
-- every time a five-second job ran.
-- `base_url` is here rather than in a constant so the explorer can build a
-- link that points at the server the data actually came from. It is the only
-- thing that would need changing for a self-hosted SonarQube, and keeping it
-- beside the run means a store fetched from one server does not link to
-- another.
CREATE TABLE sonar_runs (
  id             INTEGER PRIMARY KEY,
  started_at     TEXT NOT NULL,
  finished_at    TEXT,
  sonar_org      TEXT NOT NULL,
  base_url       TEXT NOT NULL,
  projects_found INTEGER,
  repos_matched  INTEGER,
  seconds        REAL
);
"""

# Applied in ascending order by `connect` to bring an older store up to date.
# Keyed on the version each script *produces*.
MIGRATIONS = {
    4: V4_TABLES,
    5: V5_TABLES,
    6: V6_TABLES,
}


def unusable_pairs(summary: Dict[str, Any]) -> List[str]:
    """Repo/kind pairs a report cannot honestly ignore.

    Takes either store's `coverage_summary`, so the JSON and SQLite paths apply
    one rule. Known-unsyncable repositories are filtered out; an unreadable
    file never is, because that is a fault rather than a standing condition.

    Args:
        summary: A `coverage_summary` mapping, with `missing` and `unreadable`.

    Returns:
        Sorted `repo/kind` strings, empty when the store is complete.
    """
    missing = [pair for pair in summary.get('missing', ())
               if pair.split('/', 1)[0] not in UNSYNCABLE_REPOS]
    return sorted(missing) + sorted(summary.get('unreadable', ()))


def canonical_ts(value: Optional[str], *, floor: bool = True) -> Optional[str]:
    """Normalize a timestamp to `YYYY-MM-DDTHH:MM:SSZ`.

    Args:
        value: An ISO-8601 timestamp, or None.
        floor: Truncate sub-second precision rather than rejecting it. Flooring
            a `covered_to` is conservative -- it claims slightly less coverage
            than is held, never more. Flooring a `covered_from` would claim
            *more*, so callers pass watermarks through with that in mind; in
            this cache `covered_from` is always a whole-second midnight anyway.

    Returns:
        The canonical spelling, or None.
    """
    if value is None:
        return None
    if len(value) == 20 and value.endswith('Z'):
        return value  # already canonical; the overwhelmingly common case
    moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    if moment.microsecond and not floor:
        raise ValueError(f'sub-second precision not allowed here: {value!r}')
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')


def connect(path: str, *, create: bool = True) -> sqlite3.Connection:
    """Open the store, applying the schema to a fresh file.

    Raises:
        ValueError: The store does not exist and `create` is false, or it exists
            at a schema version this code cannot read.
    """
    # Checked before connecting, because `sqlite3.connect` would otherwise
    # create an empty file as a side effect of the reader's failure and leave it
    # behind -- so the next run reports a corrupt store rather than a missing
    # one. Readers pass `create=False` precisely because they must not conjure
    # a store; saying so is more use than an empty file and a schema error.
    if not create and not os.path.exists(path):
        raise ValueError(
            f'{path}: no store here yet. Run `ghstats-sync --org <org> '
            f'--from <YYYY-MM-DD>` to create it.')

    # `check_same_thread=False` because the sync's fetch threads share one
    # connection; `SyncStore` serializes every use of it behind a lock.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL so a long read (a report) does not block the writer (a sync).
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA synchronous = NORMAL')

    version = conn.execute('PRAGMA user_version').fetchone()[0]
    if version == 0:
        if not create:
            # An existing file at version 0 is either genuinely empty or not a
            # ghstats store at all. Either way the fix is the same.
            raise ValueError(
                f'{path}: exists but holds no ghstats store. Run '
                f'`ghstats-sync --org <org> --from <YYYY-MM-DD>` to populate '
                f'it, or point --db somewhere else.')
        conn.executescript(SCHEMA + V4_TABLES + V5_TABLES + V6_TABLES)
        conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        conn.commit()
    elif version < SCHEMA_VERSION:
        migrate(conn, version)
    elif version > SCHEMA_VERSION:
        raise ValueError(
            f'{path}: user_version {version} is newer than this code '
            f'understands (expected {SCHEMA_VERSION}); upgrade ghstats'
        )
    return conn


def migrate(conn: sqlite3.Connection, version: int) -> List[int]:
    """Bring an older store up to `SCHEMA_VERSION` in place.

    Every migration is additive -- new tables only, no ALTER and no rewrite --
    so this is seconds on a store of any size and never risks the commit
    history it took hours of API budget to collect. A store that cannot be
    migrated in place would mean re-syncing twenty months of data, which is the
    outcome this ladder exists to avoid.

    Args:
        conn: An open store.
        version: The `user_version` currently on the file.

    Returns:
        The versions applied, in order.

    Raises:
        ValueError: No migration is registered for a version in the gap.
    """
    applied = []
    for target in range(version + 1, SCHEMA_VERSION + 1):
        script = MIGRATIONS.get(target)
        if script is None:
            raise ValueError(
                f'no migration to schema {target}; store is at {version} '
                f'and this code expects {SCHEMA_VERSION}'
            )
        # One transaction per step: a half-applied migration would leave the
        # store at a version whose tables do not all exist.
        with conn:
            conn.executescript(script)
            conn.execute(f'PRAGMA user_version = {target}')
        applied.append(target)
    return applied


def stamp(moment: datetime) -> str:
    """Render a datetime in the store's canonical form."""
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _placeholders(count: int) -> str:
    """`?,?,?` for an IN clause. Callers must handle count == 0 themselves --
    `IN ()` is a syntax error in SQLite, so there is no useful empty form."""
    if count < 1:
        raise ValueError('IN () is not valid SQL; branch on the empty case')
    return ','.join('?' * count)


class SyncStore:
    """Write side of the store, safe to call from the sync's fetch threads.

    One connection, one lock. Fetching is the slow part and stays parallel;
    writes serialize, which is what SQLite wants anyway and what makes the
    per-repo transaction below meaningful.

    Each merge is one transaction covering both the rows and the repository's
    coverage row. Either a repository's new data and its advanced watermark
    both land, or neither does -- so a failure leaves the watermark where it
    was and the next run re-covers the gap. The JSON store got this by keeping
    data and watermark in the same file; here it is stated outright.
    """

    def __init__(self, conn: sqlite3.Connection, *, dry_run: bool = False):
        self.conn = conn
        self.dry_run = dry_run
        self._lock = threading.Lock()

    # -- reads -------------------------------------------------------------

    def list_repos(self, org: str) -> List[str]:
        """Repositories known to the store, in name order."""
        with self._lock:
            return [r['name'] for r in self.conn.execute(
                'SELECT name FROM repos WHERE org = ? ORDER BY name', (org,))]

    def coverage(self, org: str, repo: str, kind: str
                 ) -> Optional[Tuple[datetime, datetime]]:
        """Return (covered_from, covered_to) for a repo/kind, or None."""
        with self._lock:
            row = self.conn.execute("""
                SELECT c.covered_from, c.covered_to
                FROM coverage c JOIN repos r ON r.id = c.repo_id
                WHERE r.org = ? AND r.name = ? AND c.kind = ?""",
                (org, repo, kind)).fetchone()
        if row is None:
            return None
        return (datetime.fromisoformat(row['covered_from'].replace('Z', '+00:00')),
                datetime.fromisoformat(row['covered_to'].replace('Z', '+00:00')))

    # -- writes ------------------------------------------------------------

    def _set_coverage(self, rid: int, kind: str,
                      covered_from: datetime, covered_to: datetime) -> None:
        self.conn.execute(
            'INSERT INTO coverage (repo_id, kind, covered_from, covered_to) '
            'VALUES (?,?,?,?) ON CONFLICT(repo_id, kind) DO UPDATE SET '
            'covered_from = excluded.covered_from, '
            'covered_to = excluded.covered_to',
            (rid, kind, stamp(covered_from), stamp(covered_to)))

    def merge_commits(self, org: str, repo: str,
                      records: Sequence[Dict[str, Any]], *,
                      covered_from: datetime, covered_to: datetime) -> int:
        """Insert-only merge of commits, keyed on (repo, oid).

        Returns:
            How many commits were new.
        """
        if self.dry_run:
            return len(records)
        rows = [
            (c['oid'], c.get('author_login'), c.get('author_name'),
             c.get('author_email'), canonical_ts(c['committed_date']),
             canonical_ts(c.get('authored_date')),
             c.get('additions') or 0, c.get('deletions') or 0,
             c.get('message') or '')
            for c in records
        ]
        with self._lock, self.conn:
            rid = repo_id(self.conn, org, repo)
            before = self.conn.total_changes
            # `ON CONFLICT(repo_id, oid) DO NOTHING` rather than
            # `INSERT OR IGNORE`: the latter swallows *every* constraint
            # violation, so a malformed record would be dropped as quietly as a
            # duplicate. Only the re-fetch of a commit already held is expected
            # here; anything else should surface and roll the repo back.
            self.conn.executemany(
                'INSERT INTO commits (repo_id, oid, author_login, '
                'author_name, author_email, committed_date, authored_date, '
                'additions, deletions, message) VALUES (?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(repo_id, oid) DO NOTHING',
                [(rid, *row) for row in rows])
            added = self.conn.total_changes - before
            self._set_coverage(rid, 'commits', covered_from, covered_to)
        return added

    def merge_pulls(self, org: str, repo: str,
                    records: Sequence[Dict[str, Any]], *,
                    covered_from: datetime, covered_to: datetime
                    ) -> Tuple[int, int, int]:
        """Upsert pulls by number and reviews by id.

        A pull fetched now may carry fewer reviews than the store already holds
        -- the sync requests only the newest N -- so reviews are unioned rather
        than replaced.

        `pull_metrics` is written only for records that actually carry a
        measurement. A record without one leaves any existing row alone rather
        than overwriting it with zeroes: `ghstats-import-cache` replays a JSON
        cache that predates these fields, and a blind INSERT OR REPLACE there
        would erase a backfill that cost real API budget.

        Returns:
            Tuple of (pulls added, pulls updated, reviews added).
        """
        if self.dry_run:
            return len(records), 0, sum(len(p.get('reviews') or []) for p in records)
        with self._lock, self.conn:
            rid = repo_id(self.conn, org, repo)
            known_pulls = {r['number'] for r in self.conn.execute(
                'SELECT number FROM pulls WHERE repo_id = ?', (rid,))}
            known_reviews = {r['id'] for r in self.conn.execute(
                'SELECT id FROM reviews WHERE repo_id = ?', (rid,))}
            added = updated = reviews_added = 0

            for pull in records:
                if pull['number'] in known_pulls:
                    updated += 1
                else:
                    added += 1
                    known_pulls.add(pull['number'])
                self.conn.execute(
                    'INSERT OR REPLACE INTO pulls (repo_id, number, '
                    'author_login, title, state, created_at, updated_at, '
                    'merged_at, closed_at) VALUES (?,?,?,?,?,?,?,?,?)',
                    (rid, pull['number'], pull.get('author_login'),
                     pull.get('title') or '', pull['state'],
                     canonical_ts(pull['created_at']),
                     canonical_ts(pull.get('updated_at')),
                     canonical_ts(pull.get('merged_at')),
                     canonical_ts(pull.get('closed_at'))))
                if pull.get('metrics'):
                    self._put_metrics(rid, pull['number'], pull['metrics'],
                                      stamp(covered_to))
                for review in pull.get('reviews') or []:
                    if review['id'] not in known_reviews:
                        reviews_added += 1
                        known_reviews.add(review['id'])
                    self.conn.execute(
                        'INSERT OR REPLACE INTO reviews (id, repo_id, '
                        'pull_number, author_login, submitted_at, state, body) '
                        'VALUES (?,?,?,?,?,?,?)',
                        (review['id'], rid, pull['number'],
                         review.get('author_login'),
                         canonical_ts(review.get('submitted_at')),
                         review['state'], review.get('body') or ''))

            self._set_coverage(rid, 'pulls', covered_from, covered_to)
        return added, updated, reviews_added

    def _put_metrics(self, rid: int, number: int,
                     metrics: Dict[str, Any], measured_at: str) -> None:
        """Record one pull request's size and discussion volume.

        Assumes the caller already holds the lock and a transaction.
        """
        self.conn.execute(
            'INSERT OR REPLACE INTO pull_metrics (repo_id, number, additions, '
            'deletions, changed_files, comments, review_comments, measured_at) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (rid, number,
             int(metrics.get('additions') or 0),
             int(metrics.get('deletions') or 0),
             int(metrics.get('changed_files') or 0),
             int(metrics.get('comments') or 0),
             int(metrics.get('review_comments') or 0),
             measured_at))

    def unmeasured_pulls(self, org: str, *, repo: Optional[str] = None,
                         limit: Optional[int] = None
                         ) -> List[Tuple[str, int]]:
        """Pull requests in the store with no `pull_metrics` row yet.

        Newest first: a backfill interrupted halfway is far more useful having
        covered the recent end of history than the far end, and the trend chart
        reads right-to-left from today.

        Args:
            org: Organization the store holds.
            repo: Restrict to one repository.
            limit: Stop after this many.

        Returns:
            (repo name, pull number) pairs, newest pull first.
        """
        sql = """
            SELECT r.name AS repo, p.number AS number
            FROM pulls p
            JOIN repos r ON r.id = p.repo_id
            LEFT JOIN pull_metrics m
                   ON m.repo_id = p.repo_id AND m.number = p.number
            WHERE r.org = ? AND m.repo_id IS NULL"""
        params: List[Any] = [org]
        if repo:
            sql += ' AND r.name = ?'
            params.append(repo)
        sql += ' ORDER BY p.created_at DESC'
        if limit is not None:
            sql += ' LIMIT ?'
            params.append(limit)
        with self._lock:
            return [(row['repo'], row['number'])
                    for row in self.conn.execute(sql, params)]

    def merge_pull_metrics(self, org: str, repo: str,
                           records: Sequence[Dict[str, Any]],
                           now: datetime) -> int:
        """Write measurements for pull requests already in the store.

        Unlike `merge_pulls` this touches no coverage watermark and creates no
        pull rows: a backfill measures what was already collected, and must not
        be able to claim coverage the sync has not actually established.

        Returns:
            How many rows were written.
        """
        if self.dry_run:
            return len(records)
        measured_at = stamp(now)
        with self._lock, self.conn:
            rid = repo_id(self.conn, org, repo)
            known = {row['number'] for row in self.conn.execute(
                'SELECT number FROM pulls WHERE repo_id = ?', (rid,))}
            written = 0
            for record in records:
                # A pull the store does not hold would violate the foreign key.
                # It can happen: a PR opened since the last sync shows up in a
                # backfill's enumeration but has no row yet.
                if record['number'] not in known:
                    continue
                self._put_metrics(rid, record['number'], record, measured_at)
                written += 1
        return written

    # -- membership and run history ----------------------------------------

    def record_members(self, logins: Sequence[str], now: datetime
                       ) -> Tuple[List[str], List[str]]:
        """Reconcile the member table against the organization's actual roster.

        Unlike the file it replaces, this keeps `first_seen` across a departure
        and return, and marks leavers inactive rather than forgetting them --
        so churn accumulates instead of being overwritten every night.

        Returns:
            Tuple of (joined, left), ordered case-insensitively.
        """
        current = set(logins)
        with self._lock, self.conn:
            previous = {r['login'] for r in self.conn.execute(
                'SELECT login FROM members WHERE active = 1')}
            joined = sorted(current - previous, key=str.lower)
            left = sorted(previous - current, key=str.lower)
            if self.dry_run:
                return joined, left
            when = stamp(now)
            self.conn.executemany(
                'INSERT INTO members (login, first_seen, last_seen, active) '
                'VALUES (?,?,?,1) ON CONFLICT(login) DO UPDATE SET '
                'last_seen = excluded.last_seen, active = 1',
                [(login, when, when) for login in sorted(current)])
            self.conn.executemany(
                'UPDATE members SET active = 0 WHERE login = ?',
                [(login,) for login in left])
        return joined, left

    def record_teams(self, teams: Sequence[Dict[str, Any]], now: datetime
                     ) -> Dict[str, Any]:
        """Reconcile teams, their members and their repositories.

        Same shape as `record_members`: nothing is deleted, `first_seen`
        survives a departure and return, and anything absent from the sweep is
        marked inactive. A team that is dissolved keeps its rows, so last
        quarter's activity still resolves to a group name instead of going
        blank.

        Args:
            teams: Dicts of `slug`, `name`, `description`, `parent`, `members`
                (logins) and `repos` ((name, permission) pairs).
            now: Sweep timestamp.

        Returns:
            `teams_joined` / `teams_left` (slugs), and `member_changes` mapping
            slug -> (joined, left) for teams whose roster moved.
        """
        current = {t['slug'] for t in teams}
        with self._lock, self.conn:
            previous = {r['slug'] for r in self.conn.execute(
                'SELECT slug FROM teams WHERE active = 1')}
            joined = sorted(current - previous, key=str.lower)
            left = sorted(previous - current, key=str.lower)

            member_changes: Dict[str, Tuple[List[str], List[str]]] = {}
            for team in teams:
                was = {r['login'] for r in self.conn.execute(
                    'SELECT login FROM team_members '
                    'WHERE team_slug = ? AND active = 1', (team['slug'],))}
                now_members = set(team['members'])
                added = sorted(now_members - was, key=str.lower)
                dropped = sorted(was - now_members, key=str.lower)
                if added or dropped:
                    member_changes[team['slug']] = (added, dropped)

            if self.dry_run:
                return {'teams_joined': joined, 'teams_left': left,
                        'member_changes': member_changes}

            when = stamp(now)
            self.conn.executemany(
                'INSERT INTO teams (slug, name, description, parent_slug, '
                'first_seen, last_seen, active) VALUES (?,?,?,?,?,?,1) '
                'ON CONFLICT(slug) DO UPDATE SET name = excluded.name, '
                'description = excluded.description, '
                'parent_slug = excluded.parent_slug, '
                'last_seen = excluded.last_seen, active = 1',
                [(t['slug'], t['name'], t.get('description'), t.get('parent'),
                  when, when) for t in teams])
            self.conn.executemany(
                'UPDATE teams SET active = 0 WHERE slug = ?',
                [(slug,) for slug in left])

            for team in teams:
                self.conn.executemany(
                    'INSERT INTO team_members (team_slug, login, role, '
                    'first_seen, last_seen, active) VALUES (?,?,?,?,?,1) '
                    'ON CONFLICT(team_slug, login) DO UPDATE SET '
                    'role = excluded.role, last_seen = excluded.last_seen, '
                    'active = 1',
                    [(team['slug'], login, team.get('roles', {}).get(login),
                      when, when) for login in sorted(team['members'])])
                # Deactivate rather than delete: the explorer still needs to
                # name the team someone was on when it renders old activity.
                # An emptied team deactivates its whole roster, so the NOT IN
                # is dropped rather than built from zero placeholders.
                roster = sorted(team['members'])
                if roster:
                    self.conn.execute(
                        'UPDATE team_members SET active = 0 '
                        'WHERE team_slug = ? AND login NOT IN '
                        f'({_placeholders(len(roster))})',
                        [team['slug'], *roster])
                else:
                    self.conn.execute(
                        'UPDATE team_members SET active = 0 WHERE team_slug = ?',
                        (team['slug'],))

                # Names, not ids: resolving these through `repo_id` would
                # insert coverage-less repositories. See the schema note.
                pairs = list(team.get('repos', ()))
                self.conn.executemany(
                    'INSERT INTO team_repos (team_slug, repo_name, permission, '
                    'active) VALUES (?,?,?,1) '
                    'ON CONFLICT(team_slug, repo_name) DO UPDATE SET '
                    'permission = excluded.permission, active = 1',
                    [(team['slug'], name, perm) for name, perm in pairs])
                granted = sorted({name for name, _ in pairs})
                if granted:
                    self.conn.execute(
                        'UPDATE team_repos SET active = 0 '
                        'WHERE team_slug = ? AND repo_name NOT IN '
                        f'({_placeholders(len(granted))})',
                        [team['slug'], *granted])
                else:
                    self.conn.execute(
                        'UPDATE team_repos SET active = 0 WHERE team_slug = ?',
                        (team['slug'],))

            # A dissolved team's roster is inactive too, or its members would
            # still read as a current group.
            self.conn.executemany(
                'UPDATE team_members SET active = 0 WHERE team_slug = ?',
                [(slug,) for slug in left])
        return {'teams_joined': joined, 'teams_left': left,
                'member_changes': member_changes}

    # -- sonar -------------------------------------------------------------

    def replace_sonar_projects(self, records: Sequence[Dict[str, Any]],
                               now: datetime) -> int:
        """Replace the whole SonarCloud snapshot in one transaction.

        **All or nothing, and a full replacement rather than a merge.** Sonar
        reports current state, not history: a project deleted from the
        organization, or renamed, must vanish from the store rather than linger
        as a gate status that will never change again. The dataset is one
        organization's project list -- small enough to hold in memory -- so the
        old rows are dropped and the new ones written inside a single
        transaction. A fetch that fails halfway therefore leaves yesterday's
        complete snapshot rather than a table that is half old and half new
        with no way to tell which rows are which.

        Args:
            records: Dicts of `project_key`, `name`, `repo_name`, `match_rule`,
                `last_analysis` and `gate_status`.
            now: Fetch timestamp, stamped onto every row.

        Returns:
            How many rows were written.
        """
        if self.dry_run:
            return len(records)
        fetched_at = stamp(now)
        with self._lock, self.conn:
            self.conn.execute('DELETE FROM sonar_projects')
            self.conn.executemany(
                'INSERT INTO sonar_projects (project_key, name, repo_name, '
                'match_rule, last_analysis, gate_status, fetched_at) '
                'VALUES (?,?,?,?,?,?,?)',
                [(r['project_key'], r.get('name'), r.get('repo_name'),
                  r.get('match_rule'), canonical_ts(r.get('last_analysis')),
                  r.get('gate_status'), fetched_at) for r in records])
        return len(records)

    def record_sonar_run(self, *, started_at: datetime, finished_at: datetime,
                         sonar_org: str, base_url: str, projects_found: int,
                         repos_matched: int, seconds: Optional[float]) -> None:
        """Append one row of Sonar sync history.

        This is what distinguishes "no repository has a Sonar project" from
        "nobody has asked Sonar yet"; the explorer renders the two differently
        and cannot tell them apart from `sonar_projects` alone.
        """
        if self.dry_run:
            return
        with self._lock, self.conn:
            self.conn.execute(
                'INSERT INTO sonar_runs (started_at, finished_at, sonar_org, '
                'base_url, projects_found, repos_matched, seconds) '
                'VALUES (?,?,?,?,?,?,?)',
                (stamp(started_at), stamp(finished_at), sonar_org,
                 base_url, projects_found, repos_matched, seconds))

    def record_run(self, *, started_at: datetime, finished_at: datetime,
                   repos_synced: int, repos_failed: int, points: Optional[int],
                   seconds: Optional[float], complete: bool = True) -> None:
        """Append one row of sync history.

        Args:
            complete: False for a partial sweep -- any `--skip-*`, `--repos` or
                `--limit`. A partial run must not read as fresh coverage of the
                whole organization.
        """
        if self.dry_run:
            return
        with self._lock, self.conn:
            self.conn.execute(
                'INSERT INTO sync_runs (started_at, finished_at, repos_synced, '
                'repos_failed, points, seconds, complete) VALUES (?,?,?,?,?,?,?)',
                (stamp(started_at), stamp(finished_at), repos_synced,
                 repos_failed, points, seconds, int(complete)))


def repo_id(conn: sqlite3.Connection, org: str, name: str) -> int:
    """Return the id for a repository, inserting it if new."""
    row = conn.execute(
        'SELECT id FROM repos WHERE org = ? AND name = ?', (org, name)
    ).fetchone()
    if row is not None:
        return row['id']
    cur = conn.execute(
        'INSERT INTO repos (org, name) VALUES (?, ?)', (org, name)
    )
    if cur.lastrowid is None:
        raise RuntimeError(f'insert of {org}/{name} returned no rowid')
    return cur.lastrowid
