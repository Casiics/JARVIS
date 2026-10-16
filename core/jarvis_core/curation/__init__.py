"""Four-stage context curation: collect, consolidate, prepare decision, decide.

The engine is domain-agnostic and lives in core so every domain can run it on its own data.
It never crosses a domain boundary: a pipeline is bound to one domain, refuses items from any
other domain, and writes only into the store that domain hands it.
"""
