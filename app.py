"""
SQL Copilot -- Streamlit front-end.

Run:
    pip install streamlit pymysql
    streamlit run app.py

Sidebar lets you pick mock mode or a real MySQL connection, and mock-LLM
vs the real Claude API. Main panel is the same pipeline as cli.py:
complete -> validate -> EXPLAIN -> analyze -> rewrite suggestions,
now with the history panel wired in so past requests inform new ones
and you can give a query a thumbs up/down.
"""
import streamlit as st

from sql_copilot import schema as schema_mod
from sql_copilot import llm as llm_mod
from sql_copilot import validator as validator_mod
from sql_copilot import optimizer as optimizer_mod
from sql_copilot import history as history_mod

st.set_page_config(page_title="SQL Copilot", layout="wide")


# ---------- Sidebar: connection + mode ----------
st.sidebar.title("SQL Copilot")

data_mode = st.sidebar.radio("Schema source", ["Mock (sample schema)", "Real MySQL connection"])
llm_mode = st.sidebar.radio("Query generation", ["Real Claude API", "Offline mock LLM"])

connection = None
if data_mode == "Real MySQL connection":
    with st.sidebar.form("db_form"):
        host = st.text_input("Host", "127.0.0.1")
        port = st.number_input("Port", value=3306)
        user = st.text_input("User", "root")
        password = st.text_input("Password", type="password")
        database = st.text_input("Database")
        connect_clicked = st.form_submit_button("Connect")
    if connect_clicked:
        try:
            import pymysql
            connection = pymysql.connect(host=host, port=int(port), user=user, password=password, database=database)
            st.session_state["connection"] = connection
            st.session_state["schema"] = schema_mod.introspect_mysql(connection)
            st.sidebar.success(f"Connected -- {len(st.session_state['schema'])} tables found.")
        except Exception as e:
            st.sidebar.error(f"Connection failed: {e}")
    connection = st.session_state.get("connection")
    schema = st.session_state.get("schema")
    if schema is None:
        st.info("Fill in the connection form in the sidebar and click Connect to get started.")
        st.stop()
else:
    schema = schema_mod.load_mock_schema()
    st.sidebar.info("Using the bundled sample e-commerce schema (customers, orders, order_items, products).")

schema_context = schema_mod.schema_to_prompt_context(schema)

if llm_mode == "Offline mock LLM":
    client = llm_mod.MockLLMClient()
else:
    api_key = st.sidebar.text_input("ANTHROPIC_API_KEY", type="password", help="Or set the env var and leave this blank.")
    try:
        client = llm_mod.ClaudeClient(api_key=api_key or None)
    except RuntimeError as e:
        st.sidebar.error(str(e))
        st.stop()

store = history_mod.HistoryStore()


# ---------- Sidebar: history ----------
st.sidebar.markdown("---")
st.sidebar.subheader("Recent history")
recent = store.recent(limit=8)
if not recent:
    st.sidebar.caption("No history yet -- generate a query to start building it.")
for entry in reversed(recent):
    mark = "✅" if entry.get("accepted") is True else ("✏️" if entry.get("accepted") == "edited" else ("❌" if entry.get("accepted") is False else "…"))
    st.sidebar.caption(f"{mark} {entry['user_input'][:48]}")


# ---------- Main panel ----------
st.title("Prompt-to-query, with an optimizer built in")
st.caption("Type a natural-language request or a partial SQL query. Generated SQL is checked against your real schema before anything else happens.")

user_input = st.text_area("What do you need?", placeholder="e.g. top 5 customers by total order value", height=90)
go = st.button("Generate", type="primary")

if go and user_input.strip():
    examples = store.relevant_examples(user_input, k=3)
    completion = client.complete_query(schema_context, user_input, examples=examples)
    sql = completion["sql"].strip()
    explanation = completion.get("explanation", "")

    entry_id = store.add_entry(user_input, sql, explanation)
    st.session_state["last_entry_id"] = entry_id
    st.session_state["last_sql"] = sql
    st.session_state["last_schema"] = schema
    st.session_state["last_connection"] = connection
    st.session_state["last_findings"] = None
    st.session_state["last_client"] = client
    st.session_state["last_schema_context"] = schema_context
    st.session_state["last_explanation"] = explanation

if st.session_state.get("last_sql"):
    sql = st.session_state["last_sql"]
    st.subheader("Generated query")
    st.code(sql, language="sql")
    st.caption(st.session_state.get("last_explanation", ""))

    col1, col2, col3 = st.columns(3)
    if col1.button("👍 Looks right"):
        store.mark_feedback(st.session_state["last_entry_id"], accepted=True)
        st.success("Recorded -- this'll be used as a good example for similar future requests.")
    if col2.button("👎 Not right"):
        store.mark_feedback(st.session_state["last_entry_id"], accepted=False)
        st.warning("Recorded -- won't be used as an example going forward.")
    edited = col3.text_input("Or paste your corrected SQL here and press Enter")
    if edited:
        store.mark_feedback(st.session_state["last_entry_id"], accepted="edited", edited_sql=edited)
        st.info("Saved your correction -- future similar requests will see this version instead.")
        sql = edited

    result = validator_mod.validate(sql, st.session_state["last_schema"])

    if result.errors:
        st.subheader("Validation failed")
        for e in result.errors:
            st.error(e)
    else:
        if result.warnings:
            st.subheader("Validation notes")
            for w in result.warnings:
                st.warning(w)

        plan_rows, findings = optimizer_mod.full_report(
            sql, st.session_state["last_schema"], connection=st.session_state.get("last_connection")
        )

        st.subheader("Query plan findings")
        if findings:
            for f in findings:
                st.write(f"⚠️ {f}")
        else:
            st.success("No issues found -- looks efficient as written.")

        with st.expander("Raw EXPLAIN rows"):
            st.json(plan_rows)

        if findings:
            rewrite_result = st.session_state["last_client"].propose_rewrites(
                st.session_state["last_schema_context"], sql, findings
            )
            rewrites = rewrite_result.get("rewrites", [])
            if rewrites:
                st.subheader("Suggested rewrites")
                for i, rw in enumerate(rewrites, 1):
                    st.markdown(f"**Option {i}**")
                    st.code(rw["sql"], language="sql")
                    st.caption(rw["reasoning"])
