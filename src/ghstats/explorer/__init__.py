"""Interactive exploration of the activity store.

This is how results are read. `ghstats-sync` collects, `ghstats-reindex`
classifies, and everything after that is a question asked of the store: which
commits, whose, in which repository, against which issue -- questions whose
shape is not known until someone asks one.

That is why it is served rather than generated. A pre-rendered page has to
enumerate every slice in advance, and members times repositories times days is
not a set of files anyone can hold on disk. So `queries` holds the slices as
functions over the store and `server` exposes them over loopback HTTP.
"""
