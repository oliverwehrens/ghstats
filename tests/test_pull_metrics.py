"""Tests for measuring pull request size and discussion.

Run: python -m unittest tests.test_pull_metrics -v

Covers the normalisation between GraphQL and the store, and the batching the
backfill does. The GraphQL shape is the risk here: three different connections
carry PR discussion and none of them totals the others.
"""
import unittest

from ghstats.sync import _pull_metrics, _pull_record, _review_record
from ghstats.tools import backfill_pulls


def review_node(comments=0, **kw):
    node = {'id': 'v1', 'state': 'COMMENTED', 'submittedAt': '2026-07-01T00:00:00Z',
            'body': 'looks off', 'author': {'login': 'grace'},
            'comments': {'totalCount': comments}}
    node.update(kw)
    return node


def pull_node(**kw):
    node = {'number': 7, 'title': 'a change', 'state': 'MERGED',
            'createdAt': '2026-07-01T00:00:00Z',
            'updatedAt': '2026-07-02T00:00:00Z',
            'mergedAt': '2026-07-02T00:00:00Z', 'closedAt': None,
            'author': {'login': 'ada'},
            'additions': 120, 'deletions': 30, 'changedFiles': 6,
            'comments': {'totalCount': 3},
            'reviews': {'totalCount': 2, 'nodes': []}}
    node.update(kw)
    return node


class MetricsNormalisationTest(unittest.TestCase):

    def test_reads_size_off_the_node(self):
        out = _pull_metrics(pull_node(), [])
        self.assertEqual((out['additions'], out['deletions'], out['changed_files']),
                         (120, 30, 6))

    def test_conversation_comments_come_from_the_pull(self):
        self.assertEqual(_pull_metrics(pull_node(), [])['comments'], 3)

    def test_inline_comments_are_summed_off_the_reviews(self):
        """There is no PR-level total for inline review comments anywhere in
        the schema; they hang off each review."""
        reviews = [_review_record(review_node(comments=4)),
                   _review_record(review_node(comments=7))]
        self.assertEqual(_pull_metrics(pull_node(), reviews)['review_comments'], 11)

    def test_a_missing_field_reads_as_zero_not_a_crash(self):
        """A PR the token cannot see the diff of returns nulls rather than an
        error, and one repository must not fail a batch of fifty."""
        bare = {'number': 1, 'additions': None, 'deletions': None,
                'changedFiles': None, 'comments': None}
        self.assertEqual(_pull_metrics(bare, []),
                         {'additions': 0, 'deletions': 0, 'changed_files': 0,
                          'comments': 0, 'review_comments': 0})

    def test_a_pull_record_carries_its_metrics(self):
        record = _pull_record(pull_node(), [_review_record(review_node(comments=2))])
        self.assertEqual(record['metrics']['review_comments'], 2)
        self.assertEqual(record['metrics']['additions'], 120)
        # The pull's own fields are untouched by the addition.
        self.assertEqual(record['number'], 7)
        self.assertEqual(record['state'], 'MERGED')

    def test_a_review_record_keeps_its_comment_count(self):
        out = _review_record(review_node(comments=5))
        self.assertEqual(out['comments'], 5)
        self.assertEqual(out['body'], 'looks off')


class BackfillQueryTest(unittest.TestCase):

    def test_aliases_one_variable_per_pull(self):
        """Aliased siblings rather than a page: the numbers being backfilled
        are scattered through history, and paging to them would refetch
        everything in between."""
        query = backfill_pulls._query(3)
        for index in range(3):
            self.assertIn(f'$p{index}: Int!', query)
            self.assertIn(f'p{index}: pullRequest(number: $p{index})', query)

    def test_asks_for_every_field_the_metrics_need(self):
        query = backfill_pulls._query(1)
        for field in ('additions', 'deletions', 'changedFiles',
                      'comments { totalCount }'):
            self.assertIn(field, query)

    def test_groups_by_repository_keeping_order(self):
        self.assertEqual(
            backfill_pulls._group([('a', 3), ('b', 1), ('a', 2)]),
            {'a': [3, 2], 'b': [1]})

    def test_batches_do_not_drop_a_remainder(self):
        batches = backfill_pulls._batches(list(range(7)), 3)
        self.assertEqual([list(b) for b in batches],
                         [[0, 1, 2], [3, 4, 5], [6]])


class DeepReviewTest(unittest.TestCase):
    """A pull request with more reviews than one page.

    Summing over the newest page alone would undercount discussion on exactly
    the most-reviewed pull requests, which are the ones the chart is about.
    """

    class FakeClient:
        def __init__(self, pages):
            self.pages = pages
            self.calls = 0

        def query(self, query, variables, label=None, **kw):
            page = self.pages[self.calls]
            self.calls += 1
            return page

    def page(self, counts, cursor=None):
        return {'repository': {'pullRequest': {'reviews': {
            'pageInfo': {'hasNextPage': cursor is not None, 'endCursor': cursor},
            'nodes': [{'comments': {'totalCount': n}} for n in counts],
        }}}}

    def test_sums_across_every_page(self):
        client = self.FakeClient([self.page([1, 2], cursor='c1'),
                                  self.page([3, 4])])
        total = backfill_pulls._deep_review_comments(client, 'o', 'r', 1)
        self.assertEqual(total, 10)
        self.assertEqual(client.calls, 2)

    def test_a_vanished_pull_returns_zero_rather_than_raising(self):
        client = self.FakeClient([{'repository': {'pullRequest': None}}])
        self.assertEqual(
            backfill_pulls._deep_review_comments(client, 'o', 'r', 1), 0)
