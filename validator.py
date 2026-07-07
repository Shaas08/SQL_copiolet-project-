"""
SQL validation: AST-based via sqlglot when available, with a regex-based
fallback so the tool still works if sqlglot isn't installed.

Why both: the regex version (kept as _validate_regex) is a pragmatic
safety net that shipped first and needs zero dependencies. sqlglot gives
a real parse tree, which fixes the regex version's known blind spot --
it couldn't tell a table alias from a real table name, so `oi.quantity`
in a query aliasing order_items as `oi` produced a false-positive warning.
sqlglot resolves aliases properly and also lets us check destructive
statements structurally (is there really a WHERE node) instead of by
keyword-scanning text.

If sqlglot raises on a query it can't parse (rare, but possible on
unusual dialect-specific syntax), we fall back to the regex path rather
than crashing -- a validator that fails open on its own parsing hiccups
is worse than a slightly less precise one.
"""
import re

try:
    import sqlglot
    from sqlglot import exp
    HAVE_SQLGLOT = True
except ImportError:
    HAVE_SQLGLOT = False

DESTRUCTIVE_KEYWORDS = ("DROP", "TRUNCATE", "ALTER")

TABLE_REF_RE = re.compile(r"\b(?:FROM|JOIN)\s+`?([a-zA-Z_][a-zA-Z0-9_]*)`?", re.IGNORECASE)
COLUMN_REF_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_]*)\b")


class ValidationResult:
    def __init__(self):
        self.errors = []
        self.warnings = []

    @property
    def ok(self):
        return len(self.errors) == 0

    def __repr__(self):
        return f"ValidationResult(ok={self.ok}, errors={self.errors}, warnings={self.warnings})"


def validate(sql, schema):
    if HAVE_SQLGLOT:
        try:
            return _validate_sqlglot(sql, schema)
        except Exception:
            # Parsing failed for some reason (unusual syntax, sqlglot version
            # quirk, etc) -- fall back rather than lose validation entirely.
            pass
    return _validate_regex(sql, schema)


def _validate_sqlglot(sql, schema):
    result = ValidationResult()
    known_tables = {t.lower() for t in schema.keys()}
    known_columns_by_table = {
        t.lower(): {c["name"].lower() for c in meta["columns"]}
        for t, meta in schema.items()
    }

    parsed = sqlglot.parse_one(sql, read="mysql")

    stmt_type = parsed.key  # e.g. "select", "delete", "update", "drop", "alter"
    if stmt_type in ("drop", "altertable", "alter"):
        result.errors.append(f"Query is a {stmt_type.upper()} statement, which this tool refuses to auto-run. Review and run it manually if intended.")
    if re.search(r"\bTRUNCATE\b", sql, re.IGNORECASE):
        result.errors.append("Query contains TRUNCATE, which this tool refuses to auto-run. Review and run it manually if intended.")

    if stmt_type in ("delete", "update"):
        has_where = parsed.find(exp.Where) is not None
        if not has_where:
            result.errors.append(f"{stmt_type.upper()} without a WHERE clause would affect the whole table. Refusing to run automatically.")

    # Build alias -> real table name map from every table reference in the query
    # (handles FROM, JOINs, and tables referenced inside subqueries/CTEs).
    alias_to_table = {}
    referenced_tables = set()
    for table_exp in parsed.find_all(exp.Table):
        real_name = table_exp.name.lower()
        alias = (table_exp.alias or table_exp.name).lower()
        alias_to_table[alias] = real_name
        referenced_tables.add(real_name)

    # CTEs define names that aren't real schema tables -- don't flag those.
    cte_names = {cte.alias.lower() for cte in parsed.find_all(exp.CTE)} if hasattr(exp, "CTE") else set()

    unknown_tables = referenced_tables - known_tables - cte_names
    for t in unknown_tables:
        result.errors.append(f"Query references table `{t}`, which isn't in the known schema.")

    # Column checks, using the alias map to resolve qualifiers to real tables.
    for col_exp in parsed.find_all(exp.Column):
        qualifier = col_exp.table.lower() if col_exp.table else None
        col_name = col_exp.name.lower()

        if qualifier:
            real_table = alias_to_table.get(qualifier)
            if real_table is None:
                continue  # qualifier we couldn't resolve -- don't guess, don't false-positive
            if real_table in cte_names:
                continue  # column of a CTE result set, not a real schema table
            if real_table in known_columns_by_table and col_name not in known_columns_by_table[real_table]:
                result.warnings.append(
                    f"`{qualifier}.{col_exp.name}` doesn't match a known column on `{real_table}`."
                )
        else:
            # Unqualified column: check it exists on at least one referenced table.
            candidate_tables = [t for t in referenced_tables if t in known_columns_by_table]
            if candidate_tables and not any(col_name in known_columns_by_table[t] for t in candidate_tables):
                result.warnings.append(
                    f"Column `{col_exp.name}` doesn't match a known column on any of the tables referenced ({', '.join(candidate_tables)})."
                )

    if parsed.find(exp.Star) is not None:
        result.warnings.append("Uses SELECT * -- fine for exploration, but the optimizer will flag this for production use.")

    return result


def _validate_regex(sql, schema):
    """Dependency-free fallback. See module docstring for why this exists alongside the sqlglot path."""
    result = ValidationResult()
    upper = sql.upper()

    for kw in DESTRUCTIVE_KEYWORDS:
        if re.search(rf"\b{kw}\b", upper):
            result.errors.append(f"Query contains {kw}, which this tool refuses to auto-run. Review and run it manually if intended.")

    if re.search(r"\bDELETE\b", upper) and not re.search(r"\bWHERE\b", upper):
        result.errors.append("DELETE without a WHERE clause would affect the whole table. Refusing to run automatically.")
    if re.search(r"\bUPDATE\b", upper) and not re.search(r"\bWHERE\b", upper):
        result.errors.append("UPDATE without a WHERE clause would affect the whole table. Refusing to run automatically.")

    known_tables = {t.lower() for t in schema.keys()}
    referenced_tables = {m.lower() for m in TABLE_REF_RE.findall(sql)}
    unknown_tables = referenced_tables - known_tables
    for t in unknown_tables:
        result.errors.append(f"Query references table `{t}`, which isn't in the known schema.")

    known_columns_by_table = {
        t.lower(): {c["name"].lower() for c in meta["columns"]}
        for t, meta in schema.items()
    }
    for alias_or_table, col in COLUMN_REF_RE.findall(sql):
        table_key = alias_or_table.lower()
        if table_key in known_columns_by_table and col.lower() not in known_columns_by_table[table_key]:
            result.warnings.append(
                f"`{alias_or_table}.{col}` doesn't match a known column on `{alias_or_table}` "
                f"-- this may be a false positive if `{alias_or_table}` is a table alias rather than the table name itself."
            )

    if re.search(r"SELECT\s+\*", sql, re.IGNORECASE):
        result.warnings.append("Uses SELECT * -- fine for exploration, but the optimizer will flag this for production use.")

    return result
