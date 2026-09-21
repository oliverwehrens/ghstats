#!/usr/bin/env python3
"""Check for inactive users in a GitHub organization.

Reads the local cache only; run `ghstats-sync` first. Previously this hit the
API once per user across every repository, which is the same structural problem
the per-user report had.
"""
import argparse
import sqlite3
import sys
from datetime import datetime, timezone

from dateutil import parser as date_parser

from ghstats.analysis import ActivityAnalyzer
from ghstats.clients import sqlite as sqlite_client
from ghstats.clients.sqlite import SqliteClient
from ghstats.localtime import local_zone_name, zone as _zone
from ghstats.store import sqlite as sqlite_store
from ghstats.store.json_cache import normalize


def shown(dt: datetime) -> str:
    """An instant as the operator's own clock shows it.

    Coverage bounds are stored and compared in UTC, but they are read off a
    terminal by someone deciding whether to re-sync, and "covered to 22:00" is
    a different decision from "covered to midnight". `iso()` stays the wire and
    storage format; this is only for the eye.
    """
    return normalize(dt).astimezone(_zone(local_zone_name())).strftime(
        '%Y-%m-%d %H:%M:%S %Z')


def parse_arguments():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='Check for inactive users in a GitHub organization',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  ghstats-inactive --users-file members.txt --org github --since 2024-01-01 --until 2024-12-31
  ghstats-inactive --users-file members.txt --org github --since 2024-01-01 --until 2024-12-31 --output inactive.txt
        """
    )
    
    parser.add_argument(
        '--users-file',
        default='members.txt',
        help='File containing usernames, one per line (default: members.txt, '
             'written by ghstats-sync from the live org membership)'
    )
    
    parser.add_argument(
        '--org',
        required=True,
        dest='organization',
        help='GitHub organization name'
    )
    
    parser.add_argument(
        '--since',
        required=True,
        help='Start date (YYYY-MM-DD or ISO format)'
    )
    
    parser.add_argument(
        '--until',
        default=None,
        help='End date (default: now, clamped to cache freshness)'
    )
    
    parser.add_argument(
        '--output',
        '-o',
        help='Output file to save inactive usernames (one per line)'
    )
    
    parser.add_argument(
        '--db', default=sqlite_store.DEFAULT_DB,
        help=f'Store path (default: {sqlite_store.DEFAULT_DB})'
    )
    
    return parser.parse_args()


def parse_date(date_str: str) -> datetime:
    """Parse date string to datetime object (timezone-aware, UTC)."""
    try:
        dt = date_parser.parse(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except Exception as e:
        raise ValueError(f"Invalid date format: {date_str}. Error: {e}")


def read_users_file(filepath: str) -> list[str]:
    """Read usernames from file (one per line)."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            usernames = [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]
        return usernames
    except IOError as e:
        print(f"Error: Could not read users file '{filepath}': {e}", file=sys.stderr)
        sys.exit(1)


def main():
    """Main entry point."""
    args = parse_arguments()
    
    # Parse dates
    try:
        since = parse_date(args.since)
        until = parse_date(args.until) if args.until else datetime.now(timezone.utc)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    
    if since > until:
        print("Error: Start date must be before end date.", file=sys.stderr)
        sys.exit(1)
    
    # Read users
    usernames = read_users_file(args.users_file)
    if not usernames:
        print(f"Error: No usernames found in '{args.users_file}'", file=sys.stderr)
        sys.exit(1)
    
    print(f"Checking {len(usernames)} users for activity in {args.organization} from {since.date()} to {until.date()}...")
    print()
    
    # Read from the cache; the coverage floor is a hard error because a short
    # cache would mark genuinely active people as inactive.
    try:
        conn = sqlite_store.connect(args.db, create=False)
    except (ValueError, sqlite3.Error) as exc:
        print(f'Error: could not open {args.db}: {exc}', file=sys.stderr)
        sys.exit(2)
    summary = sqlite_client.coverage_summary(conn, args.organization)
    if not summary['repos']:
        print(f"Error: no data for '{args.organization}'. Run: "
              f"ghstats-sync --org {args.organization} --from {since.date()}",
              file=sys.stderr)
        sys.exit(2)
    unusable = sqlite_store.unusable_pairs(summary)
    if unusable:
        print(f'Error: {len(unusable)} repository/kind pairs have no usable '
              f'data (e.g. {", ".join(unusable[:3])}).\n'
              f'       Marking users inactive on incomplete data would be wrong.',
              file=sys.stderr)
        sys.exit(2)
    if summary['covered_from'] is None or since < summary['covered_from']:
        print(f"Error: --since {shown(since)} predates cache coverage "
              f"{shown(summary['covered_from']) if summary['covered_from'] else 'unknown'}.\n"
              f"       Marking users inactive on incomplete data would be wrong.\n"
              f"       Run: ghstats-sync --org {args.organization} "
              f"--from {since.date()}", file=sys.stderr)
        sys.exit(2)
    until = min(until, summary['covered_to'])
    print(f"Cache covers {shown(summary['covered_from'])} to "
          f"{shown(summary['covered_to'])}")
    print()

    client = SqliteClient(conn, args.organization)
    analyzer = ActivityAnalyzer(client, quiet=True)
    
    # Check each user
    inactive_users = []
    active_users = []
    errors = []
    
    for idx, username in enumerate(usernames, 1):
        try:
            print(f"[{idx}/{len(usernames)}] Checking {username}...", end=' ', flush=True)
            
            # Analyze user activity
            metrics = analyzer.analyze_user_activity(
                args.organization,
                username,
                since,
                until,
                force_refresh=False
            )
            
            # Check if user has any activity
            has_commits = metrics['commits']['total'] > 0
            has_prs_created = metrics['pull_requests']['created'] > 0
            has_prs_reviewed = metrics['pull_requests']['reviewed'] > 0
            has_reviews = metrics['reviews']['total'] > 0
            
            if has_commits or has_prs_created or has_prs_reviewed or has_reviews:
                print("✓ active")
                active_users.append(username)
            else:
                print("✗ no activity")
                inactive_users.append(username)
        
        except Exception as e:
            print(f"⚠ error: {e}")
            errors.append((username, str(e)))
    
    # Print summary
    print()
    print("=" * 60)
    print("Summary:")
    print("=" * 60)
    print(f"Total users checked: {len(usernames)}")
    print(f"Active users: {len(active_users)}")
    print(f"Inactive users: {len(inactive_users)}")
    if errors:
        print(f"Errors: {len(errors)}")
    print()
    
    if inactive_users:
        print(f"Inactive users ({len(inactive_users)} out of {len(usernames)}):")
        for username in sorted(inactive_users):
            print(f"  - {username}")
        print()
        print("These users had no commits, PRs, or reviews in the specified period.")
        print("Consider reviewing their GitHub seat allocation.")
        
        # Save to file if requested
        if args.output:
            try:
                with open(args.output, 'w', encoding='utf-8') as f:
                    for username in sorted(inactive_users):
                        f.write(f"{username}\n")
                print()
                print(f"Inactive users saved to: {args.output}")
            except IOError as e:
                print(f"Warning: Could not write to output file: {e}", file=sys.stderr)
    else:
        print("All users were active during the specified period!")
    
    if errors:
        print()
        print("Errors encountered:")
        for username, error in errors:
            print(f"  - {username}: {error}")


if __name__ == '__main__':
    main()
