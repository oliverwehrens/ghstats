"""SonarCloud: the quality gate beside each repository.

Mirrors the layout of `ghstats.github`, not its code. The GitHub client carries
378 lines of rate-limit accounting because a sweep issues thousands of queries
against a point budget; a Sonar sweep is one paginated project list plus one
batched measures call per hundred projects -- under ten requests for most
organizations, against no budget worth tracking. Sharing an abstraction between
the two would mean generalizing over different auth, different pagination and
different error envelopes to save nothing.
"""
