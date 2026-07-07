"""
LLM client for query completion and optimization suggestions.

ClaudeClient talks to the real Anthropic API (needs ANTHROPIC_API_KEY).
MockLLMClient returns canned, deterministic responses so the rest of
the pipeline (validator, optimizer, CLI) can be developed and tested
without any network access or API key.
"""
import json
import os
import re
import urllib.request

DEFAULT_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
API_URL = "https://api.anthropic.com/v1/messages"


SYSTEM_PROMPT = """You are a MySQL query-writing assistant embedded in a developer tool.
You are given a database schema and a request from an engineer, which may be:
 - a natural language description of what they want, or
 - a partial / incomplete SQL query they've started writing.

Write ONE complete, syntactically valid MySQL query that fulfills the request,
using ONLY the tables and columns given in the schema. Never invent a table or
column name that isn't listed.

Respond with ONLY a JSON object, no other text, no markdown fences:
{
  "sql": "<the complete SQL query, ending in a semicolon>",
  "explanation": "<one or two sentences on what the query does and any assumptions you made>"
}
"""

REWRITE_SYSTEM_PROMPT = """You are a MySQL query optimization assistant.
You are given a schema, a working SQL query, and a list of specific problems
found in its EXPLAIN plan (e.g. full table scan, no index used, filesort).

Propose up to 2 rewritten versions of the query that address those specific
problems -- for example by adding a WHERE clause that can use an existing index,
restructuring a join, or replacing a correlated subquery. Do not invent new
indexes that don't exist in the schema; only suggest an index if you explicitly
say it needs to be created.

Respond with ONLY a JSON object, no other text, no markdown fences:
{
  "rewrites": [
    {"sql": "<rewritten query>", "reasoning": "<why this addresses the plan issues>"}
  ]
}
"""


def _extract_json(text):
    """LLMs sometimes wrap JSON in prose or fences despite instructions. Be forgiving."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", text, re.S)
        if brace:
            text = brace.group(0)
    return json.loads(text)


class ClaudeClient:
    def __init__(self, api_key=None, model=None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.model = model or DEFAULT_MODEL
        if not self.api_key:
            raise RuntimeError(
                "No ANTHROPIC_API_KEY found. Set it as an environment variable, "
                "or run with --mock-llm to use the offline mock client instead."
            )

    def _call(self, system, user_content, max_tokens=1024):
        body = json.dumps({
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user_content}],
        }).encode("utf-8")

        req = urllib.request.Request(
            API_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        text_parts = [block["text"] for block in data.get("content", []) if block.get("type") == "text"]
        return "".join(text_parts)

    def complete_query(self, schema_context, user_input, examples=None):
        user_content = f"SCHEMA:\n{schema_context}\n"
        if examples:
            user_content += "\nSIMILAR PAST REQUESTS AND THE QUERIES THAT WORKED FOR THEM " \
                            "(use these as style/pattern guidance, don't just copy them if the current request differs):\n"
            for ex in examples:
                user_content += f"- Request: {ex['user_input']}\n  Query: {ex['sql']}\n"
        user_content += f"\nREQUEST:\n{user_input}"
        raw = self._call(SYSTEM_PROMPT, user_content)
        return _extract_json(raw)

    def propose_rewrites(self, schema_context, sql, findings):
        findings_text = "\n".join(f"- {f}" for f in findings)
        user_content = f"SCHEMA:\n{schema_context}\n\nQUERY:\n{sql}\n\nPLAN ISSUES FOUND:\n{findings_text}"
        raw = self._call(REWRITE_SYSTEM_PROMPT, user_content)
        return _extract_json(raw)


class MockLLMClient:
    """
    Deterministic offline stand-in. Keyword-matches the request against a
    small set of canned answers built against sample_schema.json, purely so
    the validator/optimizer/CLI pipeline can be exercised end to end with
    no network and no API key.
    """

    def complete_query(self, schema_context, user_input, examples=None):
        # If history handed us a close, previously-corrected match, prefer it --
        # this is the mock-mode equivalent of the real client's few-shot behavior.
        if examples:
            return {
                "sql": examples[0]["sql"],
                "explanation": f"Reused a corrected query from a similar past request: \"{examples[0]['user_input']}\"."
            }
        text = user_input.lower()
        if "top" in text and "customer" in text:
            sql = (
                "SELECT c.customer_id, c.name, SUM(oi.quantity * oi.unit_price) AS total_value "
                "FROM customers c "
                "JOIN orders o ON o.customer_id = c.customer_id "
                "JOIN order_items oi ON oi.order_id = o.order_id "
                "GROUP BY c.customer_id, c.name "
                "ORDER BY total_value DESC "
                "LIMIT 5;"
            )
            explanation = "Joins customers through orders to order_items, sums quantity*unit_price per customer, and returns the top 5 by that total."
        elif "order" in text and ("2024" in text or "date" in text or "recent" in text):
            sql = (
                "SELECT * FROM orders WHERE order_date >= '2024-01-01' ORDER BY order_date DESC;"
            )
            explanation = "Filters orders from 2024 onward, newest first. No index exists on order_date in this schema, which the optimizer below will flag."
        else:
            sql = "SELECT * FROM customers ORDER BY created_at DESC LIMIT 20;"
            explanation = "Fallback: most recently created customers, since the request didn't match a known pattern in mock mode."
        return {"sql": sql, "explanation": explanation}

    def propose_rewrites(self, schema_context, sql, findings):
        rewrites = []
        if "SELECT *" in sql or "select *" in sql.lower():
            explicit = re.sub(r"(?i)select \*", "SELECT order_id, customer_id, order_date, status", sql)
            rewrites.append({
                "sql": explicit,
                "reasoning": "Replaced SELECT * with explicit columns so the optimizer isn't forced to fetch and transfer unused columns, and so a future covering index is possible."
            })
        if any("full table scan" in f.lower() or "no index" in f.lower() for f in findings):
            rewrites.append({
                "sql": sql.rstrip(";") + " -- consider: CREATE INDEX idx_orders_date ON orders(order_date);",
                "reasoning": "The plan shows a full table scan on orders because order_date has no index. Adding one lets MySQL seek directly to the qualifying date range instead of scanning every row."
            })
        if not rewrites:
            rewrites.append({"sql": sql, "reasoning": "No specific rewrite needed for the issues found."})
        return {"rewrites": rewrites}
