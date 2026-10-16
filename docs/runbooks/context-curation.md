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

`--watch` re-runs the stages only when `runs.jsonl` or the notes inbox changed.

## Notes inbox (test without reels)

Drop `.md` or `.txt` files into `<data-dir>/curation/inbox/`; the next pass collects them like
reels. First line is the title, `- ` lines become facts, `#hashtags` become tags, an optional
`signal: 0.8` line sets usefulness (default 0.6). Editing a note collects a new version, which
is merged into the existing entry or discarded as already known.

```bash
mkdir -p ~/jarvis-data/content/curation/inbox
cat > ~/jarvis-data/content/curation/inbox/ripgrep.md <<'NOTE'
Search code with ripgrep
signal: 0.9
ripgrep is a faster grep for large repositories. #cli
- Install ripgrep
- Replace grep -r with rg in scripts
NOTE
uv run python -m mod_content.curation --data-dir ~/jarvis-data/content run
```

## Review what the pipeline was not sure about

```bash
uv run python -m mod_content.curation --data-dir ~/jarvis-data/content review
uv run python -m mod_content.curation --data-dir ~/jarvis-data/content decide br:3f2a accept
uv run python -m mod_content.curation --data-dir ~/jarvis-data/content decide br:9c1d reject --note "hype"
```

A brief waits for review when it carries injection flags, when its classification is above
what the category allows, or when its confidence is between `auto_discard_below` and
`auto_store_at`. A unique prefix of the id is enough.

## Review in the deck

Start curation with the gateway's boundary directory and the open reviews appear on the deck's
Content page under "Kontext-Freigaben":

```bash
uv run python -m mod_content.curation --data-dir ~/jarvis-data/content run --watch \
  --boundary-dir ~/.local/share/jarvis-deck-local/boundary
```

Each pass first applies the answers given in the deck, then sends reviews the deck has not seen.
Reviews use their own mailbox (`<boundary>/context-review/`), so they never compete with the
reel worker for letters. Only PUBLIC or INTERNAL material is sent; anything stricter stays a
terminal review. If you decide a review in the terminal first, the deck's later answer is
ignored.

## Look at the result

```bash
uv run python -m mod_content.curation --data-dir ~/jarvis-data/content context
uv run python -m mod_content.curation --data-dir ~/jarvis-data/content context --category market-notes
uv run python -m mod_content.curation --data-dir ~/jarvis-data/content report
```

`report` shows counts per outcome and category and whether `curation/audit.jsonl` is intact.

```bash
uv run python -m mod_content.curation --data-dir ~/jarvis-data/content search "qqq breakout"
uv run python -m mod_content.curation --data-dir ~/jarvis-data/content export --category market-notes
```

`search` ranks entries by how much of the question they cover. `export` writes a Markdown
context pack to `curation/packs/<category>.md` for an agent's context window: strongest entries
first, capped by `--max-chars`, headed by a line that marks everything as data, not
instructions.

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
| `curation/inbox/` | your notes (stage 1 source) |
| `curation/packs/` | exported context packs |
| `curation/audit.jsonl` | hash chain of every stage (ids and hashes only) |

Curation keeps its own audit chain because `reel serve` holds the head of `audit.jsonl` in
memory; two writers on one file would break the chain.

## Trading domain

The same four stages run in the trading domain on its own data, over the ideas content proposes
into the trading inbox (`ideas.json`):

```bash
uv run python -m mod_trading.curation --data-dir ~/jarvis-data/trading run
uv run python -m mod_trading.curation --data-dir ~/jarvis-data/trading review
uv run python -m mod_trading.curation --data-dir ~/jarvis-data/trading export --category index-etf
```

Categories: `modules/mod-trading/mod_trading/curation/categories.yaml` (`index-etf` for QQQ, SPY
and IWM, `earnings`, `risk`, `general`). Ideas naming a watchlist instrument (QQQ, SPY, IWM) get
a higher signal. The inbox carries no label, so items fail closed to PRIVATE; trading reviews
stay in the terminal and never reach the deck. Thresholds are high on purpose: nothing from
social media enters trading context without the owner. Curated notes are research context for
trading's agents, never signals or orders.

## Private domain (Life-OS)

Fully local and PRIVATE. Sources are the life tips other domains proposed into the private
inbox (`inbox.json`, tainted, signal 0.5, so they always wait for you) and your own notes in
`<data-dir>/curation/inbox/` (signal 0.8 by default, so clear notes are stored directly):

```bash
uv run python -m mod_private.curation --data-dir ~/jarvis-data/private run
uv run python -m mod_private.curation --data-dir ~/jarvis-data/private review
uv run python -m mod_private.curation --data-dir ~/jarvis-data/private search "sleep"
```

Categories: `modules/mod-private/mod_private/curation/categories.yaml` (`health`, `habits`,
`productivity`, `finance`, `general`). There is no deck bridge and no boundary event: curated
private context never leaves the domain, and no stage calls a model.
