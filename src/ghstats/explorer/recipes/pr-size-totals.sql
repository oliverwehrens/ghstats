-- title: PR size and discussion: the headline numbers
-- card: pr-size
-- ignores: team, project, issue, q, ai
--
-- The tiles on the "PR size and discussion" card: how many pull requests
-- opened in the window were measured, their median size, how much discussion
-- they drew, and how many drew none.
--
-- Three things decide these numbers, and all three are spelled out below:
--
-- 1. A pull request with no pull_metrics row is UNMEASURED, not zero. It is
--    counted in `total` and `unmeasured`, and left out of everything else.
--    That is why `measured` is a JOIN, not a LEFT JOIN.
-- 2. Discussion is conversation comments + inline review comments + reviews
--    whose body is not blank. A bare approval is not discussion.
-- 3. Bots are dropped by author, from the pull requests and from the reviews.
--    Comment counts have no author, so bot comments on a human's PR stay in.
--
-- per_100_lines is total discussion over total lines, not an average of each
-- PR's ratio: a one-line PR with two comments would otherwise outweigh
-- everything else in the window.

WITH window_pulls AS (      -- every PR the card counts, measured or not
  SELECT p.repo_id, p.number, p.merged_at
  FROM pulls p
  JOIN repos r ON r.id = p.repo_id
  WHERE r.org = :org
    AND (:from IS NULL OR p.created_at >= :from)
    AND (:to IS NULL OR p.created_at < :to)
    AND (:repo IS NULL OR r.name = :repo)
    AND (:user IS NULL OR p.author_login = :user)
    AND (:bots OR p.author_login IS NULL
         OR p.author_login NOT IN (SELECT login FROM bot_logins))
),
measured AS (               -- only the PRs that have a pull_metrics row
  SELECT w.merged_at IS NOT NULL AS merged,
         m.additions + m.deletions AS lines,
         m.changed_files AS files,
         m.comments + m.review_comments
           + (SELECT COUNT(*) FROM reviews v
               WHERE v.repo_id = w.repo_id AND v.pull_number = w.number
                 AND TRIM(v.body) <> ''
                 AND (:bots OR v.author_login IS NULL
                      OR v.author_login NOT IN (SELECT login FROM bot_logins))
             ) AS discussion
  FROM window_pulls w
  JOIN pull_metrics m ON m.repo_id = w.repo_id AND m.number = w.number
)
SELECT
  (SELECT COUNT(*) FROM window_pulls)                   AS total,
  (SELECT COUNT(*) FROM window_pulls) - COUNT(*)        AS unmeasured,
  COUNT(*)                                              AS pulls,
  COALESCE(SUM(merged), 0)                              AS merged,
  COALESCE(median(lines), 0.0)                          AS lines_median,
  COALESCE(SUM(lines) * 1.0 / COUNT(*), 0.0)            AS lines_mean,
  COALESCE(SUM(lines), 0)                               AS lines_total,
  COALESCE(median(files), 0.0)                          AS files_median,
  COALESCE(median(discussion), 0.0)                     AS discussion_median,
  COALESCE(SUM(discussion) * 1.0 / COUNT(*), 0.0)       AS discussion_mean,
  COALESCE(SUM(discussion), 0)                          AS discussion_total,
  CASE WHEN SUM(lines) > 0
       THEN SUM(discussion) * 100.0 / SUM(lines) END    AS per_100_lines,
  COUNT(*) FILTER (WHERE discussion = 0)                AS undiscussed
FROM measured
