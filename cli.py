"""
SQL Copilot -- CLI entrypoint.

Usage:
  Offline demo, no DB, no API key:
      python -m sql_copilot.cli --mock --mock-llm

  Real MySQL, mocked LLM (to test DB wiring without spending API calls):
      python -m sql_copilot.cli --mock-llm --host localhost --user root --password secret --database shop

  Full real mode:
      export ANTHROPIC_API_KEY=sk-ant-...
      python -m sql_copilot.cli --host localhost --user root --password secret --database shop
"""
import argparse
import sys

from . import schema as schema_mod
from . import llm as llm_mod
from . import validator as validator_mod
from . import optimizer as optimizer_mod
from . import history as history_mod


def connect_mysql(host, user, password, database, port):
    try:
        import pymysql
    except ImportError:
        print("PyMySQL isn't installed. Run: pip install pymysql", file=sys.stderr)
        sys.exit(1)
    return pymysql.connect(host=host, user=user, password=password, database=database, port=port)


def print_header(title):
    print("\n" + "=" * len(title))
    print(title)
    print("=" * len(title))


def run(argv=None):
    parser = argparse.ArgumentParser(description="SQL Copilot -- NL/partial-query completion + optimization advisor for MySQL")
    parser.add_argument("--mock", action="store_true", help="Use the bundled sample schema instead of a live database")
    parser.add_argument("--mock-llm", action="store_true", help="Use the offline mock LLM instead of calling the Claude API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password", default="")
    parser.add_argument("--database", default=None)
    args = parser.parse_args(argv)

    connection = None
    if args.mock:
        schema = schema_mod.load_mock_schema()
        print("Running in --mock schema mode (bundled sample e-commerce schema: customers, orders, order_items, products).")
    else:
        if not args.database:
            print("--database is required unless you pass --mock", file=sys.stderr)
            sys.exit(1)
        connection = connect_mysql(args.host, args.user, args.password, args.database, args.port)
        schema = schema_mod.introspect_mysql(connection)
        print(f"Connected to MySQL database `{args.database}` -- found {len(schema)} tables.")

    schema_context = schema_mod.schema_to_prompt_context(schema)

    if args.mock_llm:
        client = llm_mod.MockLLMClient()
        print("Using the offline mock LLM (no API calls will be made).")
    else:
        try:
            client = llm_mod.ClaudeClient()
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)

    store = history_mod.HistoryStore()
    print("\nType a natural-language request or a partial SQL query. Ctrl+C to quit.\n")

    while True:
        try:
            user_input = input("copilot> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break
        if not user_input:
            continue

        examples = store.relevant_examples(user_input, k=3)
        if examples:
            print(f"(using {len(examples)} similar past request(s) as context)")

        completion = client.complete_query(schema_context, user_input, examples=examples)
        sql = completion["sql"].strip()
        entry_id = store.add_entry(user_input, sql, completion.get("explanation", ""))

        print_header("Generated query")
        print(sql)
        print(f"\n({completion.get('explanation', '')})")

        feedback = input("Feedback -- [y]es looks right / [n]o / paste corrected SQL / Enter to skip: ").strip()
        if feedback.lower() == "y":
            store.mark_feedback(entry_id, accepted=True)
        elif feedback.lower() == "n":
            store.mark_feedback(entry_id, accepted=False)
        elif feedback:
            store.mark_feedback(entry_id, accepted="edited", edited_sql=feedback)
            sql = feedback

        result = validator_mod.validate(sql, schema)
        if result.errors:
            print_header("Validation failed -- not running this")
            for e in result.errors:
                print(f"  ✗ {e}")
            continue
        if result.warnings:
            print_header("Validation notes")
            for w in result.warnings:
                print(f"  ! {w}")

        plan_rows, findings = optimizer_mod.full_report(sql, schema, connection=connection)

        print_header("Query plan findings")
        if findings:
            for f in findings:
                print(f"  ! {f}")
        else:
            print("  No issues found -- looks efficient as written.")

        if findings:
            rewrite_result = client.propose_rewrites(schema_context, sql, findings)
            rewrites = rewrite_result.get("rewrites", [])
            if rewrites:
                print_header("Suggested rewrites")
                for i, rw in enumerate(rewrites, 1):
                    print(f"\n[{i}] {rw['sql']}")
                    print(f"    Why: {rw['reasoning']}")

        print()


if __name__ == "__main__":
    run()
