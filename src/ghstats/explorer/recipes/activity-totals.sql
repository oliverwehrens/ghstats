-- title: Activity: the headline counts
-- card: totals
-- ignores: team, project, issue, kinds, q, ai
--
-- The row of tiles at the top of the People, person, repository and day pages.
--
-- Four kinds of event, each dated by its own column:
--
--   commit  commits.committed_date
--   pull    pulls.created_at      (opened)
--   merge   pulls.merged_at       (the same PR again, on the day it merged)
--   review  reviews.submitted_at
--
-- So one merged PR counts once under "PRs opened" and once under "merged", in
-- whichever windows those two days fall. Lines are commit lines only; PR size
-- lives on the PR size card. People and repositories are distinct across all
-- four kinds, and a bot excluded by login is excluded from every kind.

WITH events AS (
  SELECT 'commit' AS kind, c.committed_date AS at, c.author_login AS actor,
         r.org, r.name AS repo, c.additions, c.deletions
  FROM commits c JOIN repos r ON r.id = c.repo_id
  UNION ALL
  SELECT 'pull', p.created_at, p.author_login, r.org, r.name, 0, 0
  FROM pulls p JOIN repos r ON r.id = p.repo_id
  UNION ALL
  SELECT 'merge', p.merged_at, p.author_login, r.org, r.name, 0, 0
  FROM pulls p JOIN repos r ON r.id = p.repo_id
  WHERE p.merged_at IS NOT NULL
  UNION ALL
  SELECT 'review', v.submitted_at, v.author_login, r.org, r.name, 0, 0
  FROM reviews v JOIN repos r ON r.id = v.repo_id
  WHERE v.submitted_at IS NOT NULL
),
in_window AS (
  SELECT * FROM events
  WHERE org = :org
    AND (:from IS NULL OR at >= :from)
    AND (:to IS NULL OR at < :to)
    AND (:repo IS NULL OR repo = :repo)
    AND (:user IS NULL OR actor = :user)
    AND (:bots OR actor IS NULL OR actor NOT IN (SELECT login FROM bot_logins))
)
SELECT
  COUNT(*) FILTER (WHERE kind = 'commit')  AS commits,
  COUNT(*) FILTER (WHERE kind = 'pull')    AS pulls,
  COUNT(*) FILTER (WHERE kind = 'merge')   AS merges,
  COUNT(*) FILTER (WHERE kind = 'review')  AS reviews,
  COALESCE(SUM(additions), 0)              AS lines_added,
  COALESCE(SUM(deletions), 0)              AS lines_removed,
  COALESCE(SUM(additions) - SUM(deletions), 0) AS lines_net,
  COUNT(DISTINCT repo)                     AS repos,
  COUNT(DISTINCT actor)                    AS people
FROM in_window
