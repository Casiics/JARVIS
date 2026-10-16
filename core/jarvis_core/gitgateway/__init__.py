"""Git gateway: the only path by which agents' code changes reach the repository.

Agents hand in a patch. The gateway applies it to a fresh clone, rejects anything that touches
a protected path, commits it to a new `auto-improve/<timestamp>-<topic>` branch, pushes with a
short-lived GitHub App token and opens a draft PR. It never pushes to any other branch and
never merges; a human does (docs/architecture.md, section 6.5).
"""
