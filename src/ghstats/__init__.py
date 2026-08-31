"""GitHub organization activity reporting.

Two phases that never overlap: `sync` fetches from GitHub into a local SQLite
store, and everything else is a pure function of what that store holds.
"""
