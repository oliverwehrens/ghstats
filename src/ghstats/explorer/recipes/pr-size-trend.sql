-- title: PR size and discussion: the trend
-- card: pr-size
-- ignores: team, project, issue, q, ai
--
-- The trend chart and the table under it: measured pull requests per week or
-- per month, by the local day each was opened.
--
-- Weeks when the window spans 120 days or fewer, months beyond that. Without
-- a date filter the span is that of the measured PRs themselves. A week is
-- labelled by its Monday, a month as YYYY-MM.
--
-- Same definitions as the headline recipe: unmeasured PRs are left out,
-- discussion excludes blank reviews, and per_100_lines is a ratio of totals.
-- local_date() uses :tz, so a PR opened at 23:30 UTC can land on the next
-- day, and in the next week.

WITH measured AS (
  SELECT local_date(p.created_at) AS day,
         p.merged_at IS NOT NULL AS merged,
         m.additions + m.deletions AS lines,
         m.comments + m.review_comments
           + (SELECT COUNT(*) FROM reviews v
               WHERE v.repo_id = p.repo_id AND v.pull_number = p.number
                 AND TRIM(v.body) <> ''
                 AND (:bots OR v.author_login IS NULL
                      OR v.author_login NOT IN (SELECT login FROM bot_logins))
             ) AS discussion
  FROM pulls p
  JOIN repos r ON r.id = p.repo_id
  JOIN pull_metrics m ON m.repo_id = p.repo_id AND m.number = p.number
  WHERE r.org = :org
    AND (:from IS NULL OR p.created_at >= :from)
    AND (:to IS NULL OR p.created_at < :to)
    AND (:repo IS NULL OR r.name = :repo)
    AND (:user IS NULL OR p.author_login = :user)
    AND (:bots OR p.author_login IS NULL
         OR p.author_login NOT IN (SELECT login FROM bot_logins))
),
span AS (                   -- :to is exclusive, so its local day is one too far
  SELECT julianday(COALESCE(date(local_date(:to), '-1 day'), MAX(day)))
       - julianday(COALESCE(local_date(:from), MIN(day))) <= 120 AS weekly
  FROM measured
),
bucketed AS (
  SELECT CASE WHEN span.weekly
              THEN date(day, '-' || ((CAST(strftime('%w', day) AS INTEGER) + 6) % 7) || ' days')
              ELSE substr(day, 1, 7) END AS bucket,
         CASE WHEN span.weekly THEN 'week' ELSE 'month' END AS granularity,
         measured.*
  FROM measured, span
)
SELECT bucket,
       granularity,
       COUNT(*)                                          AS pulls,
       SUM(merged)                                       AS merged,
       median(lines)                                     AS lines_median,
       SUM(lines) * 1.0 / COUNT(*)                       AS lines_mean,
       median(discussion)                                AS discussion_median,
       SUM(discussion)                                   AS discussion_total,
       CASE WHEN SUM(lines) > 0
            THEN SUM(discussion) * 100.0 / SUM(lines) END AS per_100_lines,
       COUNT(*) FILTER (WHERE discussion = 0)            AS undiscussed
FROM bucketed
GROUP BY bucket
ORDER BY bucket
