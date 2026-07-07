# SQL Copilot

A prompt-to-query and query-optimization assistant for MySQL. Type a
half-finished query or a plain-English request; it completes the SQL,
checks it against your real schema before anything runs, then runs
`EXPLAIN` and proposes faster rewrites grounded in the actual plan.

## Try it in 30 seconds, no setup

```bash
python3 -m sql_copilot.cli --mock --mock-llm
```

This uses a bundled sample e-commerce schema (customers/orders/order_items/products)
and a canned offline "LLM" so you can see the full pipeline -- completion,
validation, EXPLAIN analysis, rewrite suggestions -- with no database and
no API key. Try typing:

```
top 5 customers by total order value
show recent orders from 2024
```

## Running it for real

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

python3 -m sql_copilot.cli \
  --host localhost --user root --password secret --database your_db
```

Flags:
- `--mock` -- use the bundled sample schema instead of connecting to a real database
- `--mock-llm` -- use the offline canned responder instead of calling the Claude API (useful to test DB wiring without spending API calls)
- `--host / --port / --user / --password / --database` -- standard MySQL connection params

## How it's put together

```
sql_copilot/
  schema.py      Introspects MySQL's INFORMATION_SCHEMA (tables, columns,
                 indexes, foreign keys), or loads the mock schema.
  llm.py         ClaudeClient (real API, plain urllib -- no SDK dependency)
                 and MockLLMClient (offline, deterministic, for testing).
                 Both accept `examples` (from history.py) as few-shot context.
  validator.py   AST-based validation via sqlglot when installed (resolves
                 table aliases properly, checks WHERE structurally), with
                 an automatic regex-based fallback if sqlglot isn't present.
  optimizer.py   Runs EXPLAIN (real or a mocked stand-in), turns the plan
                 into plain-English findings (full scans, missing indexes,
                 filesort, temp tables), plus a few static SQL smells
                 (SELECT *, IN-subquery, unbounded ORDER BY).
  history.py     Persists every request + generated SQL + feedback
                 (accepted/rejected/user-edited) to a local JSON file, and
                 surfaces similar past requests as few-shot examples for
                 new ones -- so a correction you make once keeps being used.
  cli.py         REPL front-end. Asks for y/n/corrected-SQL feedback after
                 each query and feeds that into history.py.
app.py           Streamlit web front-end -- same pipeline, with a sidebar
                 for connection settings and a visible history/feedback panel.
```

## Web UI

```bash
pip install streamlit
streamlit run app.py
```

Same pipeline as the CLI (complete → validate → EXPLAIN → analyze → rewrite),
in a browser: sidebar to pick mock vs. real MySQL and mock vs. real Claude API,
a 👍/👎/corrected-SQL feedback row under each generated query, and a running
history list in the sidebar.

## How history improves completions over time

Every request and its generated SQL is saved. When you give feedback:
- 👍 marks it as a trusted example
- ✏️ (a corrected SQL) replaces the original as the example -- so the *fixed*
  version is what gets reused, not the mistake
- 👎 excludes it from ever being resurfaced

On the next request, `history.relevant_examples()` finds past requests with
word-overlap to the new one (simple Jaccard similarity, no embeddings) and
passes the best matches to the LLM as few-shot examples. This is intentionally
simple rather than a full vector-search setup -- if your history grows into
the hundreds of entries and keyword overlap starts missing clearly-related
phrasings, that's the point to swap in a real embedding-based similarity search.

The flow per request:

1. Schema is rendered into a compact text block and sent to Claude along with your prompt.
2. Claude returns a JSON object: `{sql, explanation}`.
3. `validator.py` checks every referenced table/column against the real schema
   and blocks destructive statements -- this step is deterministic, not LLM-based,
   so a hallucinated column name gets caught before it ever reaches your database.
4. `optimizer.py` runs `EXPLAIN` on the query and turns the plan into findings.
5. If there are findings, they're sent back to Claude with the query, asking
   for up to two rewrites that specifically address those findings (not
   generic advice).

## Known limitations (read before relying on this)

- **The validator's sqlglot path hasn't been runtime-tested in a real environment**
  (this project was built in a sandbox with no network access, so `sqlglot`
  couldn't actually be installed and exercised here -- only syntax-checked).
  The logic is straightforward AST traversal, but install it and run it
  against a few real queries, especially ones with CTEs and subqueries,
  before trusting it on anything important. If it ever raises on a query
  it can't parse, it silently falls back to the regex-based checker rather
  than crashing -- so validation degrades gracefully, but check your logs
  for how often that fallback is actually triggering.
- **The Streamlit app is similarly untested end-to-end** for the same reason
  (no network in the build sandbox to install streamlit). The pipeline code
  it calls (schema/llm/validator/optimizer/history) is the same code the
  CLI uses and *was* tested; what's unverified is Streamlit's own session-state
  wiring in `app.py`. Run it and watch for session-state edge cases,
  particularly around re-running the app after connecting to a real database.
- **History's similarity matching is simple keyword overlap**, not semantic
  search -- "top customers" and "highest spending clients" won't match each
  other even though they mean the same thing. Fine for a single engineer's
  recurring phrasing, weaker for a team with varied vocabulary.
- **This never auto-executes anything.** It generates and analyzes SQL;
  running it against your data is a separate, deliberate step you take.
  Treat every generated query as a draft from a very fast junior engineer,
  not a decision.
- **Large schemas need trimming.** `schema_to_prompt_context()` caps at 40
  tables by default and sends the full column list for each. On a database
  with hundreds of tables, you'll want to either scope it to a subset
  (e.g. only tables the user's request plausibly touches) or summarize
  columns instead of listing every one, or you'll blow through context and
  cost budgets fast.
- **The mock EXPLAIN is a simplified stand-in**, not a real cost-based
  optimizer -- it exists purely so the analyzer logic can be demonstrated
  and tested without a live database. Real mode uses your actual database's
  `EXPLAIN`, which is what matters for real decisions.
- **No index-creation is ever auto-applied.** The optimizer will suggest
  `CREATE INDEX ...` as a comment when it thinks one would help, but creating
  it is left to you -- indexes have write-side costs this tool doesn't model.
