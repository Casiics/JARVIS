# Runbook: context curation (content domain)

The curation pipeline turns reel results into curated, categorised context in four stages:
collect, consolidate (duplicates out, summaries merged), prepare the decision, decide.
Architecture: `docs/architecture.md`, section 6.2a. Engine: `core/jarvis_core/curation/`.
Content adapter and CLI: `modules/mod-content/mod_content/curation/`.

## Run it

Use the same `--data-dir` as the reel worker. Curation reads `runs.jsonl` and writes only to
`<data-dir>/curation/`.

```bash
cd ~/JARVIS
# once, over everything the reel pipeline has produced so far
uv run python -m mod_content.curation --data-dir ~/jarvis-data/content run
# or continuously, next to `mod_content.reel serve`
uv run python -m mod_content.curation --data-dir ~/jarvis-data/content run --watch
```

`run` prints the counts of one pass: `collected`, `duplicates_dropped`, `stored`, `merged`,
`discarded`, `review`. Only reels that finished (`done` or `awaiting_approval`) are collected;
escalated runs have no artifact and are skipped.

## Review what the pipeline was not sure about

```bash
uv run python -m mod_content.curation --data-dir ~/jarvis-data/content review
uv run python -m mod_content.curation --data-dir ~/jarvis-data/content decide br:3f2a accept
uv run python -m mod_content.curation --data-dir ~/jarvis-data/content decide br:9c1d reject --note "hype"
```

A brief waits for review when it carries injection flags, when its classification is above
what the category allows, or when its confidence is between `auto_discard_below` and
`auto_store_at`. A unique prefix of the id is enough.

## Look at the result

```bash
uv run python -m mod_content.curation --data-dir ~/jarvis-data/content context
uv run python -m mod_content.curation --data-dir ~/jarvis-data/content context --category market-notes
uv run python -m mod_content.curation --data-dir ~/jarvis-data/content report
```

`report` shows counts per outcome and category and whether `curation/audit.jsonl` is intact.

## Categories

Categories are configuration: `modules/mod-content/mod_content/curation/categories.yaml`, or
your own file with `--categories`. Per category you set tags (count double), keywords,
`min_signal`, `auto_store_at`, `auto_discard_below` and `max_classification`. The shipped
`market-notes` category stores automatically only at 0.85, because trading claims from social
media are the noisiest material.

## Files

| File | Content |
| --- | --- |
| `curation/items.jsonl` | stage 1, every collected item |
| `curation/decisions.jsonl` | stages 3 and 4, every brief with its outcome; reviews append |
| `curation/context/<category>.jsonl` | the curated context, one entry per topic, versioned |
| `curation/audit.jsonl` | hash chain of every stage (ids and hashes only) |

Curation keeps its own audit chain because `reel serve` holds the head of `audit.jsonl` in
memory; two writers on one file would break the chain.
