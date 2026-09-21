# SonarCloud beside the repositories

The Repositories page carries two columns SonarCloud owns: the quality gate as it stands
now, and when the project was last analysed. Both link to the project. This note is why
each of the non-obvious decisions went the way it did, because all four will look
arbitrary to the next reader otherwise.

## Why a separate command and not a step in the sync

The README's invariant is easy to misread. It is **not** "exactly one program talks to the
network" — it is "the explorer does no I/O". That is the property everything downstream
depends on: a page that cannot fail on a network call, cannot be slow because a third party
is slow, and cannot go blank because a token expired overnight.

Sonar is a second network source, with its own credentials, its own failure modes and a
wildly different cost:

```
ghstats-sync    minutes, thousands of GraphQL requests, a real point budget
ghstats-sonar   seconds, under ten REST requests, no budget worth tracking
```

Folding Sonar into the commit sweep would make the cheap refresh hostage to the expensive
one: you could not ask "did that gate go green yet" without paying for a full GitHub sweep.
Making it a third phase keeps the invariant that matters and lets the five-second job run
by itself, which is how you would actually use it.

The cost of that choice is a second command to configure, so `scripts/report.sh` runs it
automatically when `SONAR_ORG` and `SONAR_TOKEN` are set and skips it silently when they
are not. It is enrichment, not a requirement: an organization with no SonarCloud account
still gets a complete report, and an existing cron does not break on upgrade.

## Why matching beats convention

The obvious implementation derives the key SonarCloud's GitHub provisioning would have
created — `<sonar-org>_<repo>` — and queries it per repository. It is one line and it is
quietly wrong.

A Sonar organization that has been running for a few years holds at least three spellings:
the auto-provisioned `acme_widget`, the hand-made `widget` from before provisioning
existed, and `oldcorp_widget` from an import nobody renamed. A convention-based lookup
reports "no Sonar project" for the second and third, which is indistinguishable from the
truth and therefore never investigated. The column silently under-reports for exactly the
repositories whose history is longest.

`/api/projects/search` returns the authoritative key for every project in the organization
in one paginated call, so matching against reality costs nothing extra. Precedence, most
specific first:

| Rule | Key | Why it is in this position |
|---|---|---|
| `override` | from `sonar-projects.txt` | First: the only one anybody stated outright |
| `prefixed` | `<sonar-org>_<repo>` | What provisioning creates; the common case |
| `bare` | `<repo>` | A hand-made project |
| `suffix` | `*_<repo>`, case-blind | Last, because it can match a foreign prefix |

Matching is deterministic in both directions: repositories are considered in name order and
a tie between candidate projects breaks on key order, so the same two inputs always produce
the same matching. A project is claimed by at most one repository — one project describes
one codebase.

**Ambiguity is resolved and reported, never raised.** Two projects that both look like one
repository is a fact about your Sonar organization, not a fault in the tool; refusing to run
over it would make the whole column unavailable because of one stray import. The run prints
what it chose and what it did not.

**The assumed key is printed for every unmatched repository.** This is the part that makes
the whole approach pay off:

```
No Sonar project (46), with the key a convention-based lookup would have used:
  legacy-importer: looked for my-sonar-org_legacy-importer
```

"This repository has no Sonar project" and "its project is not named the way this tool
guessed" produce identical output everywhere else. That line is the only thing that
separates them, and the repository's own page repeats it for the same reason.

Projects that match no repository are stored rather than discarded. A Sonar project with
nothing beside it is the other half of the same evidence, and it costs one row.

### The escape hatch, and why it is a map and not a score

The rules reach pairs that follow *some* convention. Real organizations also hold pairs
that follow none: a project renamed after the repository was, a key with a plural or a
hyphen the repository does not have, a project created under a name with no relationship
to the codebase at all. `sonar-projects.txt` is where those go, and it outranks every rule
above.

**A file, not a constant, and not a table.** Which repository answers to which key is a
property of one company's history — it is organization data, exactly like `members.txt`
and `repos.txt`, and it sits beside them in `.gitignore`. Putting it in `store/sqlite.py`
as `JIRA_PROJECT_SEED` and `UNSYNCABLE_REPOS` are would mean every correction arrived as a
source diff, and a working tree nobody could pull without a merge conflict over somebody
else's mapping. A table like `jira_projects` is the other option and earns its keep there
because a reindex must reclassify collected history offline; a Sonar re-sync takes a
second, so that argument does not carry over, and a table would need its own editing story
on top.

The format is `repo = project-key`, one per line, `#` starts a comment. Plain text rather
than JSON or TOML for two reasons. **Comments:** the map wants them, because every line in
it is a claim somebody has to be able to justify later, and JSON has none. And the floor
is Python 3.10, which has no `tomllib` — TOML would mean a third dependency for a file
with one shape.

Missing is fine and says nothing; that is the normal case. Malformed is not: a line that
cannot be read is a mapping somebody wrote and believes is in effect, so the run stops
before it touches the network and names every bad line at once, rather than skipping into
silence or making you fix one error per run.

**It is deliberately not fuzzy matching.** Running a similarity score over the unmatched
repositories and the orphan projects is an obvious feature and a bad one. On the
organization this was built against, a cutoff loose enough to catch the real pairs also
proposed `master-data-service` → `customer-data-service` and
`tswp-control-center-api-tests` → `tswp-control-center-ui`: different codebases that merely
read alike. A wrong pairing here reports another project's quality gate beside your
repository, and nothing downstream can tell — not the column, not the detail page, not the
store. A map somebody wrote by hand is a decision; a score is a guess that looks like one.

Two failure modes are reported rather than raised, for the same reason ambiguity is:

- An entry naming a project the organization no longer holds is ignored, and the
  repository falls back to the ordinary rules. A project deleted in SonarCloud should not
  cost a repository the gate a rule would have found for it — but a hand-written line that
  quietly does nothing is exactly what nobody goes looking for, so it is printed first.
- Two entries naming one project: the first in name order keeps it. One project describes
  one codebase.

Overrides are applied in a pass of their own, before any rule runs. Claiming them in name
order would let an inferred match on an earlier repository take a project that a later
repository names outright, which would make the result depend on alphabetical accident.

## Why the writes are all-or-nothing

`sonar_projects` is replaced wholesale inside one transaction, never merged.

Sonar reports current state, not history. A project deleted from the organization, or
renamed, must vanish from the store — merged in, it would linger forever as a gate status
that can never change again, because nothing will ever update it. So the old rows are
dropped and the new ones written together.

That makes atomicity load-bearing rather than tidy. Fetching is several HTTP calls and any
of them can fail; a `DELETE` that had already committed would leave no Sonar data at all
where there had been a complete snapshot. Everything is fetched into memory before anything
is written, and the dataset — one organization's project list — is small enough that this
is not a real constraint. A failure therefore leaves yesterday's coherent snapshot, which
beats today's partial one.

## Why the explorer distinguishes two kinds of nothing

An empty `sonar_projects` means two very different things:

- **Nobody has asked Sonar yet.** The column should say so.
- **Sonar was asked and matched nothing.** The column should show dashes.

Rendering the first as the second tells you every repository lacks code quality coverage on
the strength of a question nobody asked — a confident lie, and the kind that gets repeated
in a meeting. Only a run record carries the difference, which is what `sonar_runs` is for.

It is a separate table rather than a `kind` column on `sync_runs` because the two share no
measurements — `points` and `repos_failed` are meaningless for Sonar — and because
`meta()` reads the newest complete `sync_runs` row to draw the GitHub freshness banner.
Mixing Sonar rows in would move that banner every time a five-second job ran.

## Why the columns do not move with the date filter

Every other column on that page is scoped by the window: commits, PRs, reviews and churn
all mean "inside the selected range". A quality gate has no such notion. Set the range to
last January and the Sonar columns stay exactly where they are.

That is correct and it is also confusing, so the column tooltips say it outright rather
than leaving someone to change the range, watch the gates not move, and conclude the page
is broken. The same reasoning put `sonar` beside the rows in the `/api/repos` payload
rather than inside them — it is the one thing there that the window does not touch.

The rows themselves come from `_grouped()`, which also backs People, Teams and Day. A
`LEFT JOIN` there would have leaked quality gates into three views with no use for them, so
the Sonar query is separate and the client looks each row up by name.

## Where the columns appear, and where they do not

Two places, both tables whose rows are repositories: the **Repositories** page, and a
team's **"Repositories worked in"**. Both carry gate and last analysis.

Both go through `sonarExtra()`, so the cell rendering and the sort keys have one
implementation. Its `key` option is which field on a row holds the repository name —
`repo` from `repo_list`, `name` from `_grouped`. That is stated at each call site rather
than sniffed, because getting it wrong draws a dash on every row instead of failing, which
looks exactly like an organization that uses no SonarCloud.

People and Day also call `rankTable` and pass no `extra`: their rows are people and dates,
which have no quality gate.

The team card is full width, with the issue card below rather than beside it: it carries
four extra columns and is the table people come to a team's page to read.

### "Worked in" is not "owns"

That table lists the repositories a team's *members were active in*, which is a fact about
commits. Whether the *team* has the repository is a fact about GitHub grants, and the two
diverge hard: on the organization this was built against, one team's members worked in 25
repositories and the team held a grant on exactly one of them.

Two columns say which is which. `team` is this team's own permission, or "not this team's";
`granted to` lists every team that does hold it.

**The word is "granted to", not "owned by", because GitHub records nothing stronger.**
Four grants in five are ADMIN there, and half the repositories are granted to two teams or
more — one to seventeen. Picking a single owner out of that would mean inventing a rule
GitHub does not have, and it would be wrong often enough to mislead. What the data can
answer is "does this team have it, and if not, who does", which is the question anyone
actually arrives with.

A repository nobody holds shows a dash — that happens, and it is worth seeing.

One click on `team` sorts the not-ours rows to the top rather than the bottom. They get an
explicit sort key of zero for it: leaving the key absent would make `cellValue` read null
and `sortBy` pin them to the bottom in *both* directions, which is the one place they must
not be.

The team card is wrapped in `sortableTables`, which nothing else on that page is. A gate
column you cannot click to float the failures to the top is most of the point thrown away.
The side effect is that its other five columns became sortable too, which is the behaviour
the Repositories page has had since `82a6125`.

## Why only three values are stored

`/api/measures/search` costs the same whether you ask for one metric or eight, so coverage,
bugs, vulnerabilities, code smells and duplication are free at fetch time. They are not
stored anyway.

A column nobody displays is a column nobody validates: it rots quietly, and the first
person to build on it inherits whatever went wrong in the meantime. Adding them later is
one more metric key and a re-sync that takes seconds — the cheapest possible change to
make when something actually needs them.

`alert_status` has four values and `NONE` is not `ERROR`. `NONE` means the project exists
but has never produced a gate result; folding it into a failure would report a failing gate
for a project that has never run, which is a different and far more alarming claim. The UI
renders it as an absence.

## What is not tested

The Python is covered in `tests/test_sonar.py`: match precedence, ambiguity, determinism,
the assumed key, the REST client's pagination, fallback and retry against a stubbed
transport, the atomic replacement, and the never-synced distinction.

The two columns' rendering and sort keys are not. This repository has no JavaScript test
infrastructure at all — no `package.json`, by design, since the frontend deliberately
vendors its dependencies — and introducing npm and a DOM shim to assert two `data-sort`
values would be a large permanent change to the project's shape for a small gain. The
matching logic is where the bugs will be, and that is pure Python.
