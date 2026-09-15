# The explorer

> Measurements and examples below come from the one organization this was developed
> against, referred to as the source organization. Nothing org-specific ships in the
> code — see [Configuring for your organization](../README.md#configuring-for-your-organization).

This is how the collected data is read. The explorer answers *what*: which
commits, whose, in which repository, against which ticket — questions whose
shape is not known until someone asks one.

It replaced a pre-rendered HTML report that answered only *how much* — counts and
histograms, one file per person, generated ahead of time. That report is gone;
the reasoning for serving instead of generating is below.

```bash
ghstats-explore --timezone Europe/Berlin --open
```

## Why it is served, not generated

The obvious extension of the old pipeline would have been more static HTML. That
does not work here, and the reason is arithmetic: members × repositories × days
is not a set of files anyone can pre-render. Nor can the answer be embedded in
one page — the store runs to tens of megabytes, and the interesting content is
the commit messages, which is most of it.

So the slices live as functions over the store (`explorer/queries.py`) and a
loopback HTTP server exposes them (`explorer/server.py`). The store stays the
single source of truth, and reads stay a pure function of whatever the last sync
wrote — the same property the offline pipeline has.

Three properties of the server are deliberate:

| Property | Why |
|---|---|
| SQLite opened `mode=ro` | A bug in a handler cannot damage a store that cost hours of API budget. A sync can run against it concurrently — that is what WAL is for. |
| Bound to 127.0.0.1, `Host` header checked | There is no auth because there is no remote listener. The header check is against DNS rebinding: a page in your browser can point a name it controls at 127.0.0.1. |
| One connection per thread | `sqlite3` connections are not thread-safe and `ThreadingHTTPServer` hands each request its own. |

## The event stream is the spine

Every entry point is a set of aggregates plus a link into one filterable query
over commits, pull requests, merges and reviews:

```
GET /api/events?from=&to=&user=&repo=&team=&project=&issue=&kinds=&q=&ai=&bots=
```

Adding an entry point means adding a summary and a filter, never a new way to
list activity. The drill-down is where the answer lives, so there is exactly one
implementation of it.

**A pull request contributes two events, not one.** It was opened on one day and
merged on another. "What did she work on" usually means the opening; "what
changed on Tuesday" usually means the merge. Collapsing both onto `created_at`
answers the second question with the wrong day's data, silently — so `pull` and
`merge` are separate kinds.

### How the stream reads

Rows are banded by local day, in the same zone `local_date` groups the charts
by — a stream dated in the reader's zone would file a row under a day header
the charts never counted it in. The band bleeds through the card padding
because a hundred rows of even weight read as one run, and a timestamp is not
something the eye parses at a glance. Each row then carries only its clock: the
date is on the band above it.

On a person's page each day is additionally split by repository, in repository
order. "What was she doing on Tuesday" is answered by two repositories and
eleven commits, not by eleven commits sorted by minute.

`/api/events` is the one endpoint that takes its entity as a *parameter* rather
than a path segment, because it has no segment to carry one. Paging past the
first hundred rows goes there rather than back to the entity endpoint: the next
page is a hundred more rows, not a recount of every aggregate behind them.
Because a day can straddle the page boundary, the client repaints the whole
list rather than appending — otherwise the day continuing onto page two starts
again under a duplicate header.

### Entry points

| Path | Answers |
|---|---|
| `#/users`, `#/users/<login>` | What did this person do — repos, issues, review partners, AI share |
| `#/repos`, `#/repos/<name>` | What changed here, and who changed it |
| `#/teams`, `#/teams/<slug>` | What a group changed, rolled up and per member |
| `#/projects`, `#/projects/<key>` | A Jira project's issues and who moved them |
| `#/issues/<key>` | Everything referencing one key, across repos and people |
| `#/day` | Which day to open — a picker, the daily bars and the calendar |
| `#/day/<date>` | One local day, cross-cut by team, repository and person |

The hash is the whole state, so every view is bookmarkable and sendable. Clicking
a bar in any activity chart opens that day.

## Local days, UTC storage

The store holds UTC at whole-second precision. "What changed on Tuesday" is a
question about a *local* day, so a window arrives as local dates and is converted
to a half-open UTC instant range before it reaches SQL (`window_utc`) — which
keeps the comparison on the indexed columns.

Grouping for the charts uses `local_date` / `local_hour` / `local_dow`, Python
functions registered per connection. They defer to `zoneinfo` per row rather than
adding a fixed offset, because **a fixed offset is wrong half the year**: the
window this store covers spans four DST transitions, and a constant `+02:00` puts
January activity on the wrong day either side of 23:00 local.

`to` is inclusive on the way in and exclusive on the way out. Comparing against
`<= '2026-08-17'` would keep only the midnight second of the final day.

## Teams

`ghstats-sync` sweeps them, like it does members, for a handful of GraphQL
points. Skip with `--skip-teams`.

Teams are fetched 20 at a time, not 100: GraphQL bills nested connections
multiplicatively, so `teams(100) { members(100) }` is priced as 10,000 nodes.
The inner connections paginate too, and a team with more than 100 members or
repositories is drained with follow-up queries — reading only the first page
would silently truncate the largest teams, which are the ones that matter.

**Membership is current-only.** GitHub reports who is on a team today with no
history, so a window that predates someone's move credits their old work to
their new team. That is right for last week and wrong across a reorg, and the
team view says so on screen rather than leaving it to be discovered.

**A third of members are on no team.** `team_list` reports that count, because
team views otherwise look like they cover the organization when they cover two
thirds of it.

**`team_repos` is keyed on repository *name*, with no foreign key into `repos`.**
The obvious design resolves each grant through `repo_id()`, which inserts the
repository when absent — and team grants reach archived repositories the sweep
deliberately skips. On a real organization that added hundreds of coverage-less
rows to `repos`; `coverage_summary` cross-joins every repository against every
kind, `unusable_pairs` turns a missing pair into a hard error, and every
window-taking command refused for every member. Storing the name records what
GitHub said without asserting the repository is tracked. A large minority of
grants point at repositories outside the sweep, and the UI marks them.

## Jira

Issue keys are parsed out of commit messages and PR titles by `ghstats-reindex`
into `issue_refs` — derived, offline, never fetched. Adding a project and
reindexing reclassifies twenty months of history without an API call, the same
payoff the co-author trailers get.

**The extractor cannot be an open regex.** `[A-Z]{2,}-[0-9]+` matches a great
many distinct prefixes across these commit messages and most are not issues:

```
ISO-8601   HTTP-2   AES-256   RFC-3339   PSR-4   SHA-1   UTF-8
ADR-001    R2025-04  P1-3     STEP-1     E2E-1   TLS13-…
```

Extracting those invents a hundred phantom projects. So the pattern is
permissive and `jira_projects` decides — a curated table, seeded from
`JIRA_PROJECT_SEED`, `INSERT OR IGNORE` so hand additions survive a reindex.

**One project arrives under five spellings.** In the source organization one
project key was typed four different wrong ways — a transposition, a dropped
leading letter, two other slips — another was typed with a zero for its O, and a
third picked up an unrelated product name. The table maps alias → canonical, so
`BILLNIG-4131` and `BILLING-4131` are one issue — which is what they are in
Jira. Numbers are normalised through `int()`, so `ORB-0042` is `ORB-42`.

Matching is **case-insensitive**, which the whitelist makes safe: a great many
keys here are lowercase, usually a branch name carried into a merge commit, and
a case-sensitive pattern loses every one.

To adopt a project that shows up later:

```bash
ghstats-reindex --unknown-issues     # key-shaped prefixes matching no project
```

Expect noise — `ISO`, `HTTP` and `AES` will always be near the top, because
standards are written the way issue keys are. Adopt one with an `INSERT` into
`jira_projects` and reindex.

### References versus activity

An issue's **reference** count and its **event** count answer different
questions, and they do not match:

- `issue_refs` counts text naming a key. A review names nothing.
- The event stream reaches a review through the pull request it is on, so
  reviewing the PR that implements an issue counts as work on that issue.

Both are right. The first is "how often was this key written", the second is
"what work touched it".

## Bots

`bot_logins` is derived by `ghstats-reindex`. Two shapes that look equivalent
are not:

- `login LIKE '%bot%'` classifies a person whose login merely contains the
  substring as an automation — one such account was among the most prolific
  committers in the source organization.
- `login NOT LIKE '%[bot]'` catches bot commits and **no** bot pull requests,
  because GraphQL resolves commit authorship to the account record (which carries
  the suffix) while `PullRequest.author` returns the bare handle. The same
  account is `renovate[bot]` on a commit and `renovate` on a PR — and Renovate
  opened more PRs here than any human.

So the table records both spellings, derived from evidence: any login seen
suffixed anywhere, plus its bare form when that bare form is not an active
member. The membership guard is what keeps a hypothetical human `renovate` out.

**Review-only automations need the curated list.** The code review apps author
reviews and nothing else, so no suffixed spelling exists anywhere in the store to
derive the bare one from. `cursor` (Bugbot), `copilot-pull-request-reviewer`,
`renovate-approve` and `claude` together account for a sizeable share of all
reviews — and every one would otherwise read as a person reviewing code. They
live in `BOT_LOGINS`.

To find more:

```sql
SELECT v.author_login, COUNT(*) FROM reviews v
 WHERE v.author_login NOT IN (SELECT login FROM members)
   AND v.author_login NOT IN (SELECT login FROM bot_logins)
   AND NOT EXISTS (SELECT 1 FROM commits c
                    WHERE c.author_login = v.author_login)
 GROUP BY 1 ORDER BY 2 DESC;
```

Read the result before adding anything — it also surfaces people who left the
organization, whose reviews are real.

## Schema migrations

`connect()` now carries a ladder. Before it, any `user_version` mismatch was a
hard error, which for a store of any size would have meant re-syncing twenty
months of history to add a table.

Every migration is additive — new tables only, no `ALTER`, no rewrite — so it is
instant at any size and cannot damage the collected history. `V4_TABLES` is
applied both by a fresh install and by the v3→v4 step, so the two paths converge
by construction; `tests/test_migrations.py` asserts a migrated store and a fresh
one have byte-identical schemas, which is the alarm for someone adding a table to
one path and forgetting the other.

A version *newer* than the code understands is still refused. That is not
migratable and must not be guessed at.

## The charts

Chart.js, vendored under `static/vendor/` rather than linked from a CDN.
Everything downstream of `ghstats-sync` is offline by contract; a chart that
only draws when jsdelivr answers would make *looking at the results* the step
that needs the network. The static report loads the same version from the CDN,
where a 200KB inline copy would not be worth it — the two versions are meant to
stay in step, and `static/vendor/README.md` says where to change both.

Three graphs, and the same three everywhere they make sense — the People entry
point draws them over everyone, a person's page draws them over one person. A
personal rhythm only means something against the one it sits inside, and a
shape you have to remember from another page is not a comparison. (A
repository's page carries two more, which only it can answer — see
[PR size against discussion](#pr-size-against-discussion).)

| Graph | Shape | Why |
|---|---|---|
| Activity by day | Stacked bars, one stack per local day, quiet days included | Clicking a stack opens that day; a spike is only useful once you can reach it |
| Contribution calendar | Week grid, Monday at the top | Gaps. A fortnight of silence is a shape here and a flat stretch of axis on the bars |
| Activity patterns | Grouped bars by weekday, filled lines by hour | Whether reviews follow commits through the week and through the day |

**The daily series carries its zeroes.** `_by_day` returns every day in the
window, not only the days something happened on. Omitting them draws a
fortnight of silence as no gap at all — the bars either side end up adjacent
and the axis quietly relabels itself, so a stop-start month reads as a steady
one and a working week is indistinguishable from a seven-day grind. The span is
the requested window, or the span of the data when the window is open-ended,
clamped to today; an empty slice still returns nothing, so "no activity in this
window" stays a sentence rather than a flat row of zeroes.

The calendar is not a Chart.js chart. It is days on a week grid with month
rules — layout, not a plot — and the matrix plugin that could draw it costs
another dependency to end up with less control over the thing GitHub renders as
a plain grid. Its ramp is mixed from the primary accent rather than a fifth
hue, because the four categorical slots are spoken for and a green square would
read as "merge". The steps are quartiles over the days that had activity, so
the ramp describes the slice it was drawn for — which is why the key says
"Less / More" and not a number.

Two things the switch to a canvas cost, and what was done about them:

- **Keyboard.** The hand-drawn bars were focusable and openable with Enter; a
  canvas is one element. Each chart carries an `aria-label` with its totals,
  the calendar keeps a roving tabindex — one tab stop, arrows within it, Enter
  to open a day — and the event stream underneath is the same data as text.
- **Colour on a theme flip.** Colour is baked into a chart when it is built, so
  the client rebuilds the view when `prefers-color-scheme` changes. Nothing
  watches for a resize any more: Chart.js observes its own container, which
  removed a refetch that used to fire on every window drag.

## PR size against discussion

A repository's page carries one more card: how big its pull requests are, how
much discussion each drew, and whether the ratio is holding over time. The
question behind it is whether review attention is keeping up with what is being
shipped — a repository whose median PR doubles while its comments per hundred
lines halves is telling you something a commit count cannot.

Three readings, because none is sufficient alone:

| Reading | Shape | Why |
|---|---|---|
| Trend | Bars of PRs opened per week or month, against a line of comments per 100 lines changed | Whether attention tracks volume. One count, one ratio, two axes |
| Each PR | Scatter, size against comments, log x | The outliers. A four-thousand-line change nobody commented on is a point in the bottom right, and no aggregate will show it to you |
| The numbers | A row per bucket | A chart that cannot be read off is not evidence anyone can take to a retro |

Buckets are weeks up to a 120-day window and months beyond it: a quarter drawn
in months is four points, and two years drawn in weeks is a hundred. A week is
labelled by its Monday rather than an ISO week number, which is not something a
reader can place in a year without counting.

### Measured is not the same as seen

**A pull request with no `pull_metrics` row is excluded from every number on
the card, never counted as zero.** This is the defect the card is most careful
about, because it is the one that looks like a finding. Everything collected
before schema 5 has a `pulls` row and no measurement, and coalescing that to
zero draws twenty months of enormous, undiscussed pull requests — which is
indistinguishable on the chart from a real stretch of unreviewed work.

So the card says what fraction it is drawing, and names the command that fixes
it:

```bash
ghstats-backfill-pulls --org <org>          # everything unmeasured, newest first
ghstats-backfill-pulls --org <org> --limit 500   # a fixed amount of budget
ghstats-backfill-pulls --org <org> --dry-run     # what is missing, and where
```

It is a separate command rather than part of the sync because its cost is
proportional to *history* rather than to what changed, which is the opposite of
everything else `ghstats-sync` does — folding it in would make an unattended
nightly run occasionally spend an hour of API budget. It is resumable: each
batch commits on its own and the next run re-derives what is still missing from
the store, so there is no progress file to trust. It never advances a coverage
watermark, because it measures what was already collected rather than
collecting anything.

### What counts as a comment

GitHub splits pull request discussion three ways and no single field totals
them:

| Source | Field |
|---|---|
| The conversation tab | `PullRequest.comments` |
| Inline comments on the diff | `PullRequestReview.comments`, summed over reviews |
| The review submission itself | `PullRequestReview.body`, when non-empty |

All three are summed, and stored apart so the definition can change without a
re-fetch. **An empty review body is not discussion**: an approve click is a
review with no body and no comments, and counting it would make every
rubber-stamped pull request look debated.

Size is `additions + deletions`, which puts it on the same scale the rest of
the explorer already measures commits in. `changed_files` is collected too and
shows up in the scatter's tooltip.

**Trap: the bots filter cannot reach the comment counts.** `author_login`
decides which pull requests and which *reviews* are counted, but `comments` and
`review_comments` are per-PR totals GitHub reports with no author breakdown —
getting one would mean fetching every comment node instead of a count, which is
the cost this design exists to avoid. So with bots excluded a Renovate PR drops
out entirely, while a human PR that a review bot left twelve inline comments on
still carries all twelve. On a repository with review automation that inflates
discussion, and the card says so on screen rather than leaving it to be found.

### Medians, not means

Pull request size is heavily skewed: one lockfile refresh or generated-client
bump is tens of thousands of lines and drags a monthly mean past every real
change in the month. The medians are what the chart and the headline tiles
draw; the means are returned alongside, because the gap between the two is
itself the signal that a month had one of those.

The ratio is computed over the bucket as a whole — total comments over total
lines — rather than as a mean of per-PR ratios. A one-line PR with two comments
has a per-PR ratio of 200 per 100 lines, and averaging those lets it outweigh
every ordinary change in the month. A bucket that changed no lines has no ratio
at all and is drawn as a gap, not a zero: a zero would read as the reviews
having stopped when in fact nothing was shipped to review.

The scatter's size axis is logarithmic because PR size spans four orders of
magnitude in any real repository, and on a linear axis every ordinary change
collapses into a stripe against the left edge while one lockfile refresh owns
the rest of the width. A zero-line pull request is plotted at 1 rather than
dropped — log scales have no zero, and dropping them would hide the reverts and
branch merges that change nothing and still get argued about.

The legend is DOM, not Chart.js. A Chart.js legend hides datasets in local
state, which would put a second, invisible filter next to the checkbox row;
clicking a key here writes the same `kinds` parameter the checkboxes write, so
a legend, a checkbox and a pasted link cannot disagree.

The four event kinds take the first four slots of a categorical palette
validated for colour-vision deficiency and contrast in both light and dark mode
(worst adjacent CVD ΔE 9.1 light / 8.4 dark against a ≥8 target). Colour follows
the kind, never its rank, so filtering the stream does not repaint the
survivors. Three light-mode slots sit below 3:1 contrast, which obliges a table
view — the event stream is that table.

All data reaches the DOM through `textContent`. Commit messages, branch names and
repository names are arbitrary strings from a third party; building HTML out of
them by concatenation is how a commit message becomes script. A test pins the
absence of `innerHTML` in the client.
