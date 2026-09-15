-- title: PR size and discussion by repository
-- card: pr-size-by-repo
-- ignores: team, project, issue, q, ai
--
-- The per-repository table on the Repositories page: one row for every
-- repository that opened a pull request in the window, most pull requests
-- first.
--
-- A repository whose pull requests are all unmeasured keeps its row, with
-- pulls = 0 and no ratio. That is `seen LEFT JOIN summary`: the LEFT JOIN is
-- between repositories, never between a pull request and its metrics, so an
-- unmeasured PR still cannot turn into a zero-line one.
--
-- :repo is not applied: the table is the comparison across repositories.
-- The no-comment share on the page is undiscussed / pulls.

WITH window_pulls AS (
  SELECT r.name AS repo, p.repo_id, p.number, p.merged_at
  FROM pulls p
  JOIN repos r ON r.id = p.repo_id
  WHERE r.org = :org
    AND (:from IS NULL OR p.created_at >= :from)
    AND (:to IS NULL OR p.created_at < :to)
    AND (:user IS NULL OR p.author_login = :user)
    AND (:bots OR p.author_login IS NULL
         OR p.author_login NOT IN (SELECT login FROM bot_logins))
),
measured AS (
  SELECT w.repo,
         w.merged_at IS NOT NULL AS merged,
         m.additions + m.deletions AS lines,
         m.comments + m.review_comments
           + (SELECT COUNT(*) FROM reviews v
               WHERE v.repo_id = w.repo_id AND v.pull_number = w.number
                 AND TRIM(v.body) <> ''
                 AND (:bots OR v.author_login IS NULL
                      OR v.author_login NOT IN (SELECT login FROM bot_logins))
             ) AS discussion
  FROM window_pulls w
  JOIN pull_metrics m ON m.repo_id = w.repo_id AND m.number = w.number
),
seen AS (
  SELECT repo, COUNT(*) AS total FROM window_pulls GROUP BY repo
),
summary AS (
  SELECT repo,
         COUNT(*) AS pulls,
         SUM(merged) AS merged,
         median(lines) AS lines_median,
         median(discussion) AS discussion_median,
         SUM(lines) AS lines_total,
         SUM(discussion) AS discussion_total,
         COUNT(*) FILTER (WHERE discussion = 0) AS undiscussed
  FROM measured
  GROUP BY repo
)
SELECT seen.repo,
       seen.total,
       seen.total - COALESCE(summary.pulls, 0)            AS unmeasured,
       COALESCE(summary.pulls, 0)                         AS pulls,
       COALESCE(summary.merged, 0)                        AS merged,
       COALESCE(summary.lines_median, 0.0)                AS lines_median,
       COALESCE(summary.discussion_median, 0.0)           AS discussion_median,
       CASE WHEN summary.lines_total > 0
            THEN summary.discussion_total * 100.0 / summary.lines_total
       END                                                AS per_100_lines,
       COALESCE(summary.undiscussed, 0)                   AS undiscussed
FROM seen
LEFT JOIN summary ON summary.repo = seen.repo
ORDER BY seen.total DESC, seen.repo
