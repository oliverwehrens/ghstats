"""Core activity analysis logic for GitHub user activity.

**Instants in UTC, buckets in the reader's zone.** Every timestamp is
normalized to UTC before it is compared, so windows and ordering are
unambiguous. Everything that answers "when do these people work" -- the
weekday histogram, the hour histogram, the contribution calendar -- is
bucketed in `tz_name` instead, which defaults to this machine's zone.

Those buckets used to be UTC, on the promise that the reporting layer would
convert them. That layer was the pre-rendered HTML report, and it is gone, so
the conversion never happened: a Berlin team's 23:30 commit was counted at
21:30 on the day before. A count cannot be re-zoned after the fact anyway --
by the time it is a number under `'Tuesday'` the instant that made it is gone
-- so the bucketing has to happen here, where the instant still exists.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from collections import defaultdict

from ghstats.localtime import local_zone_name, zone as _zone


def _normalize_datetime(dt: datetime) -> datetime:
    """Normalize datetime to UTC timezone-aware.
    
    Args:
        dt: datetime object (may be naive or aware)
        
    Returns:
        timezone-aware datetime in UTC
    """
    if dt.tzinfo is None:
        # Naive datetime - assume UTC
        return dt.replace(tzinfo=timezone.utc)
    else:
        # Convert to UTC if timezone-aware
        return dt.astimezone(timezone.utc)


class ActivityAnalyzer:
    """Analyzes GitHub user activity across repositories."""
    
    def __init__(self, client: Any, quiet: bool = False,
                 tz_name: Optional[str] = None):
        """Initialize analyzer with an activity source.

        Args:
            client: OfflineClient instance (reads the local cache; no network)
            quiet: Suppress per-repository progress. Essential for batch runs:
                one line per repo per user runs to hundreds of thousands of
                lines on a large organization.
            tz_name: IANA zone the weekday, hour and date buckets are counted
                in. Defaults to this machine's zone; an unknown name falls back
                to UTC rather than raising, and `metrics['timezone']` reports
                whichever one is in force.
        """
        self.client = client
        self.quiet = quiet
        self.tz_name = tz_name or local_zone_name()
        self.zone = _zone(self.tz_name)

    def _local(self, moment: datetime) -> datetime:
        """The instant as the reader's wall clock shows it."""
        return _normalize_datetime(moment).astimezone(self.zone)
    
    def analyze_user_activity(
        self,
        org_name: str,
        username: str,
        since: datetime,
        until: datetime,
        force_refresh: bool = False,
        repos_to_analyze: Optional[List[str]] = None
    ) -> Dict:
        """Analyze user activity within an organization.
        
        Args:
            org_name: GitHub organization name
            username: GitHub username
            since: Start date (inclusive)
            until: End date (inclusive)
            force_refresh: Force refresh repository cache (default: False)
            repos_to_analyze: Optional list of specific repository names to analyze.
                             If None, analyzes all repositories in the organization.
            
        Returns:
            Dictionary containing all activity metrics
        """
        if repos_to_analyze:
            repos = self.client.get_specific_repos(org_name, repos_to_analyze)
            if not self.quiet:
                print(f"Analyzing {len(repos)} specified repositories (out of {len(repos_to_analyze)} requested)")
        else:
            repos = self.client.get_organization_repos(org_name, force_refresh=force_refresh)
            if not self.quiet:
                print(f"Found {len(repos)} repositories in {org_name}")
        
        # Initialize metrics
        metrics = {
            'username': username,
            'organization': org_name,
            'date_range': {
                'since': since.isoformat(),
                'until': until.isoformat()
            },
            'timezone': self.tz_name,
            'repositories_analyzed': len(repos),
            'commits': {
                'total': 0,
                'lines_added': 0,
                'lines_removed': 0,
                'lines_net': 0,
                'by_repo': {},
                'by_day_of_week': defaultdict(int),
                'by_hour': defaultdict(int)
            },
            'pull_requests': {
                'created': 0,
                'reviewed': 0,
                'merged': 0,
                'created_by_repo': {},
                'reviewed_by_repo': {},
                'merged_by_repo': {},
                'created_by_day_of_week': defaultdict(int),
                'created_by_hour': defaultdict(int),
                'reviewed_by_day_of_week': defaultdict(int),
                'reviewed_by_hour': defaultdict(int)
            },
            'reviews': {
                'total': 0,
                'approvals': 0,
                'rejections': 0,
                'comments': 0,
                'by_repo': {},
                'by_day_of_week': defaultdict(int),
                'by_hour': defaultdict(int),
                'approvals_by_day_of_week': defaultdict(int),
                'approvals_by_hour': defaultdict(int),
                'rejections_by_day_of_week': defaultdict(int),
                'rejections_by_hour': defaultdict(int),
                'comments_by_day_of_week': defaultdict(int),
                'comments_by_hour': defaultdict(int)
            },
            'activity_by_date': defaultdict(int)  # Track all activity by date for contribution calendar
        }
        
        # Process each repository
        for idx, repo in enumerate(repos, 1):
            if not self.quiet:
                print(f"[{idx}/{len(repos)}] Analyzing {repo.name}...")
            repo_metrics = self._analyze_repo_activity(repo, username, since, until)
            
            # Aggregate commit metrics
            metrics['commits']['total'] += repo_metrics['commits']['total']
            metrics['commits']['lines_added'] += repo_metrics['commits']['lines_added']
            metrics['commits']['lines_removed'] += repo_metrics['commits']['lines_removed']
            metrics['commits']['lines_net'] += repo_metrics['commits']['lines_net']
            # Aggregate timing data
            for day, count in repo_metrics['commits']['by_day_of_week'].items():
                metrics['commits']['by_day_of_week'][day] += count
            for hour, count in repo_metrics['commits']['by_hour'].items():
                metrics['commits']['by_hour'][hour] += count
            if repo_metrics['commits']['total'] > 0:
                metrics['commits']['by_repo'][repo.name] = repo_metrics['commits']
            
            # Aggregate PR metrics
            metrics['pull_requests']['created'] += repo_metrics['pull_requests']['created']
            metrics['pull_requests']['reviewed'] += repo_metrics['pull_requests']['reviewed']
            metrics['pull_requests']['merged'] += repo_metrics['pull_requests']['merged']
            # Aggregate PR timing data
            for day, count in repo_metrics['pull_requests']['created_by_day_of_week'].items():
                metrics['pull_requests']['created_by_day_of_week'][day] += count
            for hour, count in repo_metrics['pull_requests']['created_by_hour'].items():
                metrics['pull_requests']['created_by_hour'][hour] += count
            for day, count in repo_metrics['pull_requests']['reviewed_by_day_of_week'].items():
                metrics['pull_requests']['reviewed_by_day_of_week'][day] += count
            for hour, count in repo_metrics['pull_requests']['reviewed_by_hour'].items():
                metrics['pull_requests']['reviewed_by_hour'][hour] += count
            if repo_metrics['pull_requests']['created'] > 0:
                metrics['pull_requests']['created_by_repo'][repo.name] = repo_metrics['pull_requests']['created']
            if repo_metrics['pull_requests']['reviewed'] > 0:
                metrics['pull_requests']['reviewed_by_repo'][repo.name] = repo_metrics['pull_requests']['reviewed']
            if repo_metrics['pull_requests']['merged'] > 0:
                metrics['pull_requests']['merged_by_repo'][repo.name] = repo_metrics['pull_requests']['merged']
            
            # Aggregate review metrics
            metrics['reviews']['total'] += repo_metrics['reviews']['total']
            metrics['reviews']['approvals'] += repo_metrics['reviews']['approvals']
            metrics['reviews']['rejections'] += repo_metrics['reviews']['rejections']
            metrics['reviews']['comments'] += repo_metrics['reviews']['comments']
            # Aggregate review timing data
            for day, count in repo_metrics['reviews']['by_day_of_week'].items():
                metrics['reviews']['by_day_of_week'][day] += count
            for hour, count in repo_metrics['reviews']['by_hour'].items():
                metrics['reviews']['by_hour'][hour] += count
            # Aggregate review type timing data
            for day, count in repo_metrics['reviews']['approvals_by_day_of_week'].items():
                metrics['reviews']['approvals_by_day_of_week'][day] += count
            for hour, count in repo_metrics['reviews']['approvals_by_hour'].items():
                metrics['reviews']['approvals_by_hour'][hour] += count
            for day, count in repo_metrics['reviews']['rejections_by_day_of_week'].items():
                metrics['reviews']['rejections_by_day_of_week'][day] += count
            for hour, count in repo_metrics['reviews']['rejections_by_hour'].items():
                metrics['reviews']['rejections_by_hour'][hour] += count
            for day, count in repo_metrics['reviews']['comments_by_day_of_week'].items():
                metrics['reviews']['comments_by_day_of_week'][day] += count
            for hour, count in repo_metrics['reviews']['comments_by_hour'].items():
                metrics['reviews']['comments_by_hour'][hour] += count
            if repo_metrics['reviews']['total'] > 0:
                metrics['reviews']['by_repo'][repo.name] = repo_metrics['reviews']
            
            # Aggregate activity by date for contribution calendar
            for date, count in repo_metrics['activity_by_date'].items():
                metrics['activity_by_date'][date] += count
        
        # Calculate net lines
        metrics['commits']['lines_net'] = (
            metrics['commits']['lines_added'] - metrics['commits']['lines_removed']
        )
        
        # Convert defaultdicts to regular dicts for JSON serialization
        metrics['commits']['by_day_of_week'] = dict(metrics['commits']['by_day_of_week'])
        metrics['commits']['by_hour'] = dict(metrics['commits']['by_hour'])
        metrics['pull_requests']['created_by_day_of_week'] = dict(metrics['pull_requests']['created_by_day_of_week'])
        metrics['pull_requests']['created_by_hour'] = dict(metrics['pull_requests']['created_by_hour'])
        metrics['pull_requests']['reviewed_by_day_of_week'] = dict(metrics['pull_requests']['reviewed_by_day_of_week'])
        metrics['pull_requests']['reviewed_by_hour'] = dict(metrics['pull_requests']['reviewed_by_hour'])
        metrics['reviews']['by_day_of_week'] = dict(metrics['reviews']['by_day_of_week'])
        metrics['reviews']['by_hour'] = dict(metrics['reviews']['by_hour'])
        metrics['reviews']['approvals_by_day_of_week'] = dict(metrics['reviews']['approvals_by_day_of_week'])
        metrics['reviews']['approvals_by_hour'] = dict(metrics['reviews']['approvals_by_hour'])
        metrics['reviews']['rejections_by_day_of_week'] = dict(metrics['reviews']['rejections_by_day_of_week'])
        metrics['reviews']['rejections_by_hour'] = dict(metrics['reviews']['rejections_by_hour'])
        metrics['reviews']['comments_by_day_of_week'] = dict(metrics['reviews']['comments_by_day_of_week'])
        metrics['reviews']['comments_by_hour'] = dict(metrics['reviews']['comments_by_hour'])
        metrics['activity_by_date'] = dict(metrics['activity_by_date'])
        
        # Build list of repos where user had any activity
        active_repos = set()
        active_repos.update(metrics['commits']['by_repo'].keys())
        active_repos.update(metrics['pull_requests']['created_by_repo'].keys())
        active_repos.update(metrics['pull_requests']['reviewed_by_repo'].keys())
        active_repos.update(metrics['reviews']['by_repo'].keys())
        
        # Create detailed active repos list with activity summary
        metrics['active_repos'] = []
        for repo_name in sorted(active_repos):
            commit_data = metrics['commits']['by_repo'].get(repo_name, {})
            repo_activity = {
                'name': repo_name,
                'commits': commit_data.get('total', 0),
                'lines_added': commit_data.get('lines_added', 0),
                'lines_removed': commit_data.get('lines_removed', 0),
                'prs_created': metrics['pull_requests']['created_by_repo'].get(repo_name, 0),
                'prs_reviewed': metrics['pull_requests']['reviewed_by_repo'].get(repo_name, 0),
                'reviews': metrics['reviews']['by_repo'].get(repo_name, {}).get('total', 0)
            }
            metrics['active_repos'].append(repo_activity)
        
        return metrics
    
    def _analyze_repo_activity(
        self,
        repo: Any,
        username: str,
        since: datetime,
        until: datetime
    ) -> Dict:
        """Analyze user activity in a single repository.
        
        Args:
            repo: Repository object
            username: GitHub username
            since: Start date (inclusive)
            until: End date (inclusive)
            
        Returns:
            Dictionary containing repository-specific metrics
        """
        repo_metrics = {
            'commits': {
                'total': 0,
                'lines_added': 0,
                'lines_removed': 0,
                'lines_net': 0,
                'by_day_of_week': defaultdict(int),
                'by_hour': defaultdict(int)
            },
            'pull_requests': {
                'created': 0,
                'reviewed': 0,
                'merged': 0,
                'created_by_day_of_week': defaultdict(int),
                'created_by_hour': defaultdict(int),
                'reviewed_by_day_of_week': defaultdict(int),
                'reviewed_by_hour': defaultdict(int)
            },
            'reviews': {
                'total': 0,
                'approvals': 0,
                'rejections': 0,
                'comments': 0,
                'by_day_of_week': defaultdict(int),
                'by_hour': defaultdict(int),
                'approvals_by_day_of_week': defaultdict(int),
                'approvals_by_hour': defaultdict(int),
                'rejections_by_day_of_week': defaultdict(int),
                'rejections_by_hour': defaultdict(int),
                'comments_by_day_of_week': defaultdict(int),
                'comments_by_hour': defaultdict(int)
            },
            'activity_by_date': defaultdict(int)  # Track all activity by date
        }
        
        # Analyze commits
        commits = self.client.get_user_commits(repo, username, since, until)
        repo_metrics['commits']['total'] = len(commits)
        
        for commit in commits:
            additions, deletions = self.client.get_commit_stats(commit)
            repo_metrics['commits']['lines_added'] += additions
            repo_metrics['commits']['lines_removed'] += deletions
            
            # Timing buckets are the reader's wall clock, not UTC.
            commit_local = self._local(commit.date)
            day_of_week = commit_local.strftime('%A')  # Monday, Tuesday, etc.
            hour = commit_local.hour
            repo_metrics['commits']['by_day_of_week'][day_of_week] += 1
            repo_metrics['commits']['by_hour'][hour] += 1
            
            # Track activity by date for contribution calendar
            activity_date = commit_local.strftime('%Y-%m-%d')
            repo_metrics['activity_by_date'][activity_date] += 1
        
        repo_metrics['commits']['lines_net'] = (
            repo_metrics['commits']['lines_added'] - 
            repo_metrics['commits']['lines_removed']
        )
        
        # Analyze pull requests created
        created_prs = self.client.get_pull_requests_created(repo, username, since, until)
        repo_metrics['pull_requests']['created'] = len(created_prs)
        
        # Count merged PRs and track timing
        since_utc = _normalize_datetime(since)
        until_utc = _normalize_datetime(until)
        for pr in created_prs:
            pr_created_local = self._local(pr.created_at)
            day_of_week = pr_created_local.strftime('%A')
            hour = pr_created_local.hour
            repo_metrics['pull_requests']['created_by_day_of_week'][day_of_week] += 1
            repo_metrics['pull_requests']['created_by_hour'][hour] += 1
            
            # Track activity by date for contribution calendar
            activity_date = pr_created_local.strftime('%Y-%m-%d')
            repo_metrics['activity_by_date'][activity_date] += 1
            
            if pr.merged and pr.merged_at:
                pr_merged_at = _normalize_datetime(pr.merged_at)
                if since_utc <= pr_merged_at <= until_utc:
                    repo_metrics['pull_requests']['merged'] += 1
        
        # Analyze pull requests reviewed
        reviewed_prs = self.client.get_pull_requests_reviewed(repo, username, since, until)
        repo_metrics['pull_requests']['reviewed'] = len(reviewed_prs)
        
        # Track timing for reviewed PRs (use review submission time)
        # Note: We need to get the actual review time, which we'll do in the reviews section
        
        # Analyze reviews
        reviews = self.client.get_reviews_by_user(repo, username, since, until)
        repo_metrics['reviews']['total'] = len(reviews)
        
        # Track unique PRs for timing (to avoid counting same PR multiple times)
        reviewed_pr_timing_tracked = set()
        
        for review in reviews:
            state = review.state.lower()
            if state == 'approved':
                repo_metrics['reviews']['approvals'] += 1
            elif state == 'changes_requested':
                repo_metrics['reviews']['rejections'] += 1
            elif state == 'commented':
                repo_metrics['reviews']['comments'] += 1
            
            if review.submitted_at:
                review_local = self._local(review.submitted_at)
                day_of_week = review_local.strftime('%A')
                hour = review_local.hour
                
                # Reviews timing - count every review submission
                repo_metrics['reviews']['by_day_of_week'][day_of_week] += 1
                repo_metrics['reviews']['by_hour'][hour] += 1
                
                # Track timing per review type
                if state == 'approved':
                    repo_metrics['reviews']['approvals_by_day_of_week'][day_of_week] += 1
                    repo_metrics['reviews']['approvals_by_hour'][hour] += 1
                elif state == 'changes_requested':
                    repo_metrics['reviews']['rejections_by_day_of_week'][day_of_week] += 1
                    repo_metrics['reviews']['rejections_by_hour'][hour] += 1
                elif state == 'commented':
                    repo_metrics['reviews']['comments_by_day_of_week'][day_of_week] += 1
                    repo_metrics['reviews']['comments_by_hour'][hour] += 1
                
                # PRs Reviewed timing - count once per unique PR
                pr_number = getattr(review, 'pr_number', None)
                if pr_number is not None and pr_number not in reviewed_pr_timing_tracked:
                    reviewed_pr_timing_tracked.add(pr_number)
                    repo_metrics['pull_requests']['reviewed_by_day_of_week'][day_of_week] += 1
                    repo_metrics['pull_requests']['reviewed_by_hour'][hour] += 1
                
                # Track activity by date for contribution calendar
                activity_date = review_local.strftime('%Y-%m-%d')
                repo_metrics['activity_by_date'][activity_date] += 1
        
        return repo_metrics

