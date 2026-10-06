from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import re
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

try:
    import sqlglot
    from sqlglot import exp, parse_one
    from sqlglot.dialects.oracle import Oracle
    from sqlglot.dialects.postgres import Postgres
    from sqlglot.dialects.duckdb import DuckDB
    from sqlglot.dialects.dialect import unit_to_str
    from sqlglot.errors import ErrorLevel, UnsupportedError
    from sqlglot.tokens import TokenType

    SQLGLOT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without sqlglot
    sqlglot = None  # type: ignore[assignment]
    exp = None  # type: ignore[assignment]
    parse_one = None  # type: ignore[assignment]
    Oracle = object  # type: ignore[assignment,misc]
    Postgres = object  # type: ignore[assignment,misc]
    DuckDB = object  # type: ignore[assignment,misc]
    unit_to_str = None  # type: ignore[assignment]
    TokenType = None  # type: ignore[assignment]
    ErrorLevel = None  # type: ignore[assignment]
    UnsupportedError = Exception  # type: ignore[assignment,misc]
    SQLGLOT_AVAILABLE = False


TOOL_NAME = "migration-transpiler"
TOOL_VERSION = "3.0.0"

#: Oracle SCN bind. The run record supplies the value; nothing is hardcoded.
SCN_BIND = ":run_scn"
SCN_BIND_PATH = "RUN_RECORD.oracle_scn"

#: Watermark bind, used only when ``run.acquisition`` is ``query-incremental``.
WATERMARK_BIND = ":run_watermark"
WATERMARK_BIND_PATH = "RUN_RECORD.watermark"

#: Wildcard identifier used for a computed projection, matching basic mode.
INNER_ALIAS = "__transformed"

#: Prefix for columns that exist only to route rows into a quarantine relation.
QUARANTINE_PREFIX = "__q_"

#: PostgreSQL truncates identifiers at 63 bytes.
PG_MAX_IDENTIFIER_BYTES = 63

#: HOCON triple-quote delimiter, used for multi-line SQL bodies.
TRIPLE_QUOTE = '"' * 3

#: The two artifacts. There are only two, and each is a single file holding
#: everything for its engine, because one file per engine is what makes the whole
#: migration reviewable as one diff.
SEATUNNEL_CONF_NAME = "seatunnel.conf"
DUCKDB_JOBS_NAME = "duckdb.yaml"

#: Variables the generated SeaTunnel config expects the runtime to supply.
SEATUNNEL_VARIABLES = [
    "SOURCE_JDBC_URL",
    "SOURCE_DB_USER",
    "SOURCE_DB_PASSWORD",
    "TARGET_JDBC_URL",
    "TARGET_DB_USER",
    "TARGET_DB_PASSWORD",
]

#: Relations the DuckDB plan binds at run time. A DuckDB job never names the
#: Oracle source directly: the runtime materialises ``__SOURCE__<job>`` from the
#: pinned snapshot and attaches ``__TARGET__<job>`` from the loaded target, and
#: every generated check compares those two. Naming them by convention is what
#: lets one check SQL be identical across every job.
SOURCE_VIEW_PREFIX = "__SOURCE__"
TARGET_VIEW_PREFIX = "__TARGET__"
QUARANTINE_VIEW_PREFIX = "__QUARANTINE__"

#: The specification schema this compiler reads.
SUPPORTED_SCHEMA_VERSIONS = {"5.0.0", "5.1.0"}

#: The project root, derived from this file's location rather than the working
#: directory. The three default paths below name things that belong to the
#: project -- its approved specification, its source catalog, its output folder --
#: so resolving them against the working directory would make the same command
#: behave differently depending on where it was typed. An editor that launches the
#: script with the workspace root as its cwd is the common case, and it is not the
#: directory the script lives in.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SPEC = PROJECT_ROOT / "input" / "hr-spec.yaml"
DEFAULT_CATALOG = PROJECT_ROOT / "input" / "catalog.yaml"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"

SEVERITY_ORDER = {"BLOCK": 0, "GOVERNANCE": 1, "ASSUMPTION": 2, "EDGE": 3, "INFO": 4}


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
#
# These sets and maps decide what this compiler accepts: which schema
# versions, which operations, which join types, which Oracle types have a
# PostgreSQL meaning. They are the **built-in defaults** -- the rulebook a
# deployment ships, `config/rules.yaml`, replaces them at startup, and the
# "Rulebook" section before `main()` explains how that file is found,
# validated and installed. Read sites do not name the file: they read these
# names, and `load_rulebook` points the names at whatever it loaded, so every
# lookup consults the loaded rulebook when the code runs.
#
# Adding an operation therefore still starts here (AGENT.md section 11), and
# then follows into `config/rules.yaml`, which must list it too or a
# deployment shipping that file will keep blocking it.

VALUE_OPS = {
    "cast",
    "trim",
    "substring",
    "mask",
    "default-when-null",
    "date-add",
    "regexp-extract",
    "uppercase",
    "lowercase",
    "replace",
    "round",
    "truncate",
    "date-trunc",
    "null-to-empty",
    "empty-to-null",
    "normalize-unicode",
    "pad",
    "left",
    "right",
    "split-part",
    "coalesce-blank",
}

DERIVED_OPS = {"concat", "coalesce", "case-when", "arithmetic", "cast-as", "md5"}

DROP_OPS = {"drop"}

STRUCTURAL_OPS = {"add-column", "drop-column", "rename-column", "set-default"}

#: Categories the specification defines. Anything else is a compile-time block.
KNOWN_CATEGORIES = {
    "value",
    "derived",
    "structural",
    "escape",
    "row-selection",
    "column-split",
    "lookup",
    "set",
    "cardinality",
    "relational",
    "pivot",
    "change-aware",
    "key-declaration",
}

#: The operation gate: which operations a step may use under each category.
#: `_validate_step` reads this and blocks anything else
#: (STEP_OPERATION_UNKNOWN), because a shape this compiler does not implement
#: must stop there rather than fall through to a compiler that would have to
#: approximate it.
#:
#: This is the built-in default. The `categories` key of `config/rules.yaml`
#: replaces it at startup like every other vocabulary here; it is given a name
#: of its own so that one read site in `_validate_step` can consult the loaded
#: rulebook the same way every other read site does. `value` allows `drop` on
#: top of VALUE_OPS because a value step that drops the column is still a
#: value step -- DROP_OPS is read separately by the projection loop and by the
#: sensitive-handling check, which need to recognise a removal on its own.
CATEGORY_OPERATIONS = {
    "value": VALUE_OPS | DROP_OPS,
    "derived": DERIVED_OPS,
    "structural": STRUCTURAL_OPS,
    "set": {"deduplicate", "distinct", "filter", "top-n"},
    "cardinality": {"merge", "split"},
    "relational": {"denormalise", "normalise"},
}

#: The categories that carry an operations gate -- the keys of the mapping
#: above, taken before any rulebook can replace it. The rulebook loader
#: requires a file to define all of them: removing one would not relax the
#: rule for that category, it would switch the check off, and an unimplemented
#: operation would then reach the compiler instead of stopping at validation.
_GATED_CATEGORIES = tuple(CATEGORY_OPERATIONS)

#: Categories that make a job snapshot-only rather than CDC-capable.
SNAPSHOT_ONLY_CATEGORIES = {"set", "relational", "pivot"}

#: Categories compiled outside the projection pass. They shape the job's source
#: relation, its join or its filter rather than adding a column, so the projection
#: loop skips them instead of rejecting them as unhandled.
ELSEWHERE_HANDLED_CATEGORIES = {
    "row-selection",
    "lookup",
    "set",
    "cardinality",
    "relational",
    "pivot",
}

PREDICATE_OPERATORS = {
    "eq": "=",
    "ne": "<>",
    "gt": ">",
    "ge": ">=",
    "lt": "<",
    "le": "<=",
}

ARITHMETIC_OPERATORS = {
    "add": "+",
    "subtract": "-",
    "multiply": "*",
    "divide": "/",
    "modulo": "%",
}

#: Aggregate functions the pivot compiler accepts.
PIVOT_AGGREGATES = {
    "sum": "SUM",
    "min": "MIN",
    "max": "MAX",
    "count": "COUNT",
    "count-distinct": "COUNT(DISTINCT {expr})",
    "avg": "AVG",
    "avg-distinct": "AVG(DISTINCT {expr})",
    "listagg": "LISTAGG",
    "string-agg": "STRING_AGG",
}

#: Join types the denormalise compiler accepts, mapped to SQL.
JOIN_TYPES = {
    "inner": "INNER JOIN",
    "left": "LEFT JOIN",
    "left-outer": "LEFT JOIN",
    "right": "RIGHT JOIN",
    "right-outer": "RIGHT JOIN",
    "full": "FULL JOIN",
    "full-outer": "FULL JOIN",
    "cross": "CROSS JOIN",
}

#: Oracle -> PostgreSQL base type names. Precision/scale/timezone suffixes are
#: appended by :func:`oracle_type_to_pg` rather than being listed here.
ORACLE_TO_PG = {
    "NUMBER": "numeric",
    "NUMERIC": "numeric",
    "DECIMAL": "numeric",
    "DEC": "numeric",
    "INTEGER": "integer",
    "INT": "integer",
    "SMALLINT": "smallint",
    "FLOAT": "double precision",
    "BINARY_FLOAT": "real",
    "BINARY_DOUBLE": "double precision",
    "DOUBLE PRECISION": "double precision",
    "REAL": "real",
    "VARCHAR": "character varying",
    "VARCHAR2": "character varying",
    "NVARCHAR2": "character varying",
    "STRING": "character varying",
    "CHAR": "character",
    "NCHAR": "character",
    "CHARACTER": "character",
    "CLOB": "text",
    "NCLOB": "text",
    "LONG": "text",
    "LONG RAW": "bytea",
    "RAW": "bytea",
    "BLOB": "bytea",
    "IMAGE": "bytea",
    "DATE": "date",
    "TIMESTAMP": "timestamp",
    "TIMESTAMP WITH TIME ZONE": "timestamptz",
    "TIMESTAMP WITH LOCAL TIME ZONE": "timestamptz",
    "INTERVAL": "interval",
    "INTERVAL YEAR TO MONTH": "interval",
    "INTERVAL DAY TO SECOND": "interval",
    "ROWID": "text",
    "UROWID": "text",
    "XMLTYPE": "xml",
    "JSON": "jsonb",
    "BOOLEAN": "boolean",
}

#: PostgreSQL reserved and special words. ``naming.onReservedOrSpecial: quote``
#: quotes these rather than renaming them, because the approved contract names
#: the object.
PG_RESERVED_WORDS = {
    "all", "analyse", "analyze", "and", "any", "array", "as", "asc", "authorization",
    "between", "binary", "both", "case", "cast", "check", "collate", "column",
    "constraint", "create", "cross", "current_date", "current_role", "current_time",
    "current_timestamp", "current_user", "default", "deferrable", "desc", "distinct",
    "do", "else", "end", "except", "false", "for", "foreign", "freeze", "from", "full",
    "grant", "group", "having", "ilike", "in", "initially", "inner", "intersect",
    "into", "is", "isnull", "join", "lateral", "leading", "left", "like", "limit",
    "localtime", "localtimestamp", "natural", "not", "notnull", "null", "offset",
    "on", "only", "or", "order", "outer", "overlaps", "placing", "primary",
    "references", "returning", "right", "select", "session_user", "similar", "some",
    "symmetric", "table", "tablesample", "then", "to", "trailing", "true", "union",
    "unique", "user", "using", "verbose", "when", "where", "window", "with",
}


# ---------------------------------------------------------------------------
# Project-local SQLGlot dialects
# ---------------------------------------------------------------------------
#
# Both dialects inherit parsing from a real engine and override generation so the
# emitted text stays faithful to the approved specification instead of drifting
# into the source engine's own type names and function spellings.

if SQLGLOT_AVAILABLE:

    class CTunnelFlashback(exp.Expression):
        """Oracle flashback read pinned to the run's captured SCN.

        The clause is parsed immediately after a relation and before its alias,
        which is where Oracle itself places it. ``scn`` is a named bind so the
        SeaTunnel runtime supplies the value; the transpiler never hardcodes one.
        """

        arg_types = {"scn": False}

        def sql(self, dialect=None, **kwargs) -> str:
            scn = self.args.get("scn") or SCN_BIND
            return f"AS OF SCN {scn}"

    class CTunnelParser(Oracle.Parser):
        """Oracle parsing plus the ``AS OF SCN`` clause.

        SQLGlot parses a relation's alias inside ``_parse_table``, so the
        flashback clause is intercepted at ``_parse_table_alias`` -- which runs
        immediately before the alias is read -- and parked on the parser until
        ``_parse_table`` can attach it to the node it just built. Hooking
        ``_parse_table_parts`` instead would run *after* the alias and therefore
        never match when the relation is aliased.
        """

        def _parse_ctunnel_flashback(self) -> Optional["CTunnelFlashback"]:
            index = self._index
            if not self._match_text_seq("AS", "OF", "SCN"):
                self._retreat(index)
                return None

            if self._match(TokenType.COLON):
                name = self._curr.text if self._curr else None
                self._advance()
                if name:
                    return CTunnelFlashback(scn=exp.Placeholder(this=name))
            elif self._match(TokenType.NUMBER):
                return CTunnelFlashback(scn=exp.Literal.number(self._prev.text))

            self._retreat(index)
            return None

        def _parse_table_alias(self, **kwargs):
            flashback = self._parse_ctunnel_flashback()
            if flashback is not None:
                # Saved on the instance; _parse_table restores the outer slot.
                self._ctunnel_flashback = flashback
            return super()._parse_table_alias(**kwargs)

        def _parse_table(self, **kwargs):
            parent = getattr(self, "_ctunnel_flashback", None)
            self._ctunnel_flashback = None
            table = super()._parse_table(**kwargs)
            flashback = self._ctunnel_flashback
            self._ctunnel_flashback = parent
            if flashback is not None and isinstance(table, exp.Table):
                table.set("flashback", flashback)
            return table

    class CTunnelGenerator(Oracle.Generator):
        # The specification states target-engine types. Oracle can express these
        # verbatim, so they are kept as written: `numeric(12,2)` stays NUMERIC
        # and `varchar(160)` stays VARCHAR instead of becoming NUMBER / VARCHAR2
        # and drifting from the approved contract.
        #
        # `timestamptz` is deliberately absent: mapping it to the literal string
        # "TIMESTAMP WITH TIME ZONE" makes Oracle's own generator append the
        # timezone a second time on the next render, so timestamptz round-trips
        # only when it falls through to Oracle's native handling.
        TYPE_MAPPING = {
            **Oracle.Generator.TYPE_MAPPING,
            exp.DataType.Type.TEXT: "STRING",
            exp.DataType.Type.DECIMAL: "NUMERIC",
            exp.DataType.Type.VARCHAR: "VARCHAR",
        }

        TRANSFORMS = {
            **Oracle.Generator.TRANSFORMS,
            CTunnelFlashback: lambda self, e: (
                f"AS OF SCN {self.sql(e, 'scn') if e.args.get('scn') else SCN_BIND}"
            ),
            # SHA2 is Spark syntax. Oracle's equivalent is STANDARD_HASH, and a
            # plain function rename round-trips as a node.
            exp.SHA2: lambda self, e: (
                f"STANDARD_HASH({self.sql(e, 'this')}, 'SHA256')"
            ),
        }

        def table_parts(self, expression: exp.Expression) -> str:
            """Emit the flashback between the relation and its alias."""
            parts = super().table_parts(expression)
            flashback = expression.args.get("flashback")
            if flashback is not None:
                parts = f"{parts} {self.sql(flashback)}"
            return parts

    class CTunnel(Oracle):
        """Oracle-compatible cTunnel baseline.

        Parsing follows Oracle because cTunnel reads the Oracle source, plus the
        flashback clause. Generation keeps the specification's cast type names.
        """

        Parser = CTunnelParser
        Generator = CTunnelGenerator

    class PGContract(Postgres):
        """Target-side dialect for generated PostgreSQL DDL.

        Inherits PostgreSQL parsing so every statement is proven to be legal
        PostgreSQL, and overrides generation so ``numeric(12,2)`` stays NUMERIC
        rather than being rewritten to DECIMAL, matching the approved contract.
        """

        class Generator(Postgres.Generator):
            TYPE_MAPPING = {
                **Postgres.Generator.TYPE_MAPPING,
                exp.DataType.Type.DECIMAL: "NUMERIC",
            }

    class DuckDBStrict(DuckDB):
        """DuckDB, refusing to degrade quietly.

        SQLGlot's default behaviour when the target dialect cannot express a
        construct is to emit a *wrong but valid* statement: ``TO_CHAR(d, 'YYYY')``
        becomes ``CAST(d AS TEXT)``, which loses the formatting it was asked for
        and looks fine to a reader. For a validation plan that is the worst
        possible failure -- it makes a date compare equal to a string, so the
        check passes for the wrong reason.

        Raising instead turns every such construct into a BLOCK here, where the
        message can name it, rather than into a wrong answer at run time.
        """

        class Generator(DuckDB.Generator):
            unsupported_level = ErrorLevel.RAISE

    sqlglot.Dialect.classes["ctunnel"] = CTunnel
    sqlglot.Dialect.classes["pgcontract"] = PGContract
    sqlglot.Dialect.classes["duckdbstrict"] = DuckDBStrict

else:  # pragma: no cover - only used when sqlglot is missing

    class CTunnelFlashback:  # type: ignore[no-redef]
        pass

    class CTunnel(Oracle):  # type: ignore[no-redef]
        pass

    class PGContract(Postgres):  # type: ignore[no-redef]
        pass

    class DuckDBStrict(DuckDB):  # type: ignore[no-redef]
        pass


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

@dataclass
class Diag:
    """One compile-time finding.

    ``severity`` drives the pipeline:

    ``BLOCK``      the job cannot be compiled and must not be sent to SeaTunnel
    ``GOVERNANCE`` the spec's ``governance.requiresApproval`` names this finding
    ``ASSUMPTION`` a deterministic choice was made that a human should confirm
    ``EDGE``       an edge case was handled and is reported for audit
    ``INFO``       neutral provenance
    """

    code: str
    severity: str
    message: str
    rule_id: Optional[str] = None
    table: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"code": self.code, "severity": self.severity, "message": self.message}
        if self.rule_id:
            out["rule_id"] = self.rule_id
        if self.table:
            out["table"] = self.table
        if self.detail:
            out["detail"] = self.detail
        return out


class Diagnostics:
    """Ordered, de-duplicated diagnostic sink shared by every compiler stage."""

    def __init__(self) -> None:
        self.items: List[Diag] = []
        self._seen: set = set()

    def add(
        self,
        code: str,
        severity: str,
        message: str,
        rule_id: Optional[str] = None,
        table: Optional[str] = None,
        **detail: Any,
    ) -> Diag:
        key = (code, severity, message, rule_id, table)
        if key in self._seen:
            for existing in self.items:
                if (
                    existing.code,
                    existing.severity,
                    existing.message,
                    existing.rule_id,
                    existing.table,
                ) == key:
                    return existing
        item = Diag(code, severity, message, rule_id, table, dict(detail))
        self.items.append(item)
        self._seen.add(key)
        return item

    def extend(self, other: Iterable[Diag]) -> None:
        for item in other:
            self.add(
                item.code,
                item.severity,
                item.message,
                item.rule_id,
                item.table,
                **item.detail,
            )

    def of(self, severity: str) -> List[Diag]:
        return [item for item in self.items if item.severity == severity]

    @property
    def blocking(self) -> List[Diag]:
        return self.of("BLOCK")

    def sorted(self) -> List[Diag]:
        return sorted(self.items, key=lambda d: (SEVERITY_ORDER.get(d.severity, 9), d.code, d.message))

    def as_list(self) -> List[Dict[str, Any]]:
        return [item.as_dict() for item in self.sorted()]


class CompileError(Exception):
    """Raised when a rule cannot be compiled into SQL at all."""


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

class SpecReadError(Exception):
    """A specification could not be read as YAML.

    Carries a message written for the person who supplied the file, not for the
    parser. A YAML scanner error names a line and a column but not the cause: the
    reader above it has no idea that `#` was typed as `\\#`, and the most common
    cause of a malformed specification is a copy through a tool that escaped it.
    """


def _describe_yaml_failure(path: Path, text: str, error: "yaml.YAMLError") -> str:
    """Turn a YAML error into something a person can act on.

    Reports the file, the line, the offending line itself, and -- when the shape
    of the text matches a known accident -- what to do about it. The three that
    recur are a tool having escaped the comment marker, non-breaking spaces where
    indentation belongs, and tabs.
    """
    lines = text.splitlines()
    mark = getattr(error, "problem_mark", None) or getattr(error, "context_mark", None)
    where = f"{path}"
    if mark is not None:
        where = f"{path}, line {mark.line + 1}, column {mark.column + 1}"

    lines_out = [f"{where} is not valid YAML.", ""]

    problem = getattr(error, "problem", None) or str(error).splitlines()[0]
    lines_out.append(f"  {problem}")
    context = getattr(error, "context", None)
    if context:
        # PyYAML's context already begins with "while", so it is not prefixed here.
        lines_out.append(f"  {context.rstrip('.')}")

    if mark is not None and 0 <= mark.line < len(lines):
        offending = lines[mark.line].replace("\t", "<TAB>").replace("\u00a0", "<NBSP>")
        lines_out.append("")
        lines_out.append(f"  line {mark.line + 1}: {offending}")
        lines_out.append("  " + " " * mark.column + "^")

        # When the parser gave up on a comment or a blank line, the line it names
        # is not the line that is wrong -- the key above the comment is. PyYAML
        # reports where it stopped, not where it started failing, so showing only
        # that line points a reader at innocent text.
        previous = None
        for index in range(mark.line - 1, -1, -1):
            candidate = lines[index]
            if candidate.strip() and not candidate.lstrip().startswith("#"):
                previous = index
                break
        # Only worth showing when it is plausibly the culprit: a line that is
        # plainly not a key (a list item, say) would only mislead.
        if previous is not None:
            above = lines[previous]
            stripped_above = above.strip()
            looks_like_broken_key = (
                ":" not in stripped_above
                and not stripped_above.startswith("-")
                and "{" not in stripped_above
                and "[" not in stripped_above
            )
            if looks_like_broken_key:
                lines_out.append("")
                lines_out.append("  the line above, which is where a key was expected:")
                lines_out.append(
                    f"  line {previous + 1}: "
                    + above.replace("\t", "<TAB>").replace("\u00a0", "<NBSP>")
                )

    lines_out.append("")
    lines_out.append("  If this file was copied through a tool that reformatted it, check for:")
    lines_out.append("")

    escaped_comments = [i + 1 for i, line in enumerate(lines) if line.lstrip().startswith("\\#")]
    if escaped_comments:
        shown = ", ".join(str(n) for n in escaped_comments[:6])
        more = f" (and {len(escaped_comments) - 6} more)" if len(escaped_comments) > 6 else ""
        lines_out.append(
            f"    - {len(escaped_comments)} line(s) begin with an escaped comment marker "
            f"'\\#' at line {shown}{more}."
        )
        lines_out.append("      A backslash before '#' makes it data, not a comment, so the parser")
        lines_out.append("      reads those lines as a bare scalar with no key. Delete the backslashes.")

    nbsp_lines = [i + 1 for i, line in enumerate(lines) if "\u00a0" in line]
    if nbsp_lines:
        total = text.count("\u00a0")
        shown = ", ".join(str(n) for n in nbsp_lines[:6])
        more = f" (and {len(nbsp_lines) - 6} more)" if len(nbsp_lines) > 6 else ""
        lines_out.append(
            f"    - {total} non-breaking space(s), U+00A0, on line {shown}{more}."
        )
        lines_out.append("      YAML indentation must be ordinary spaces. A non-breaking space is not")
        lines_out.append("      whitespace to the parser, so it becomes part of a key or a value.")
        lines_out.append("      Replace them with ' ' -- in a code editor, find-and-replace on U+00A0.")

    tab_lines = [i + 1 for i, line in enumerate(lines) if "\t" in line]
    if tab_lines:
        shown = ", ".join(str(n) for n in tab_lines[:6])
        lines_out.append(f"    - tab character(s) used for indentation on line {shown}.")
        lines_out.append("      YAML forbids tabs in indentation; use spaces.")

    if not (escaped_comments or nbsp_lines or tab_lines):
        lines_out.append("    - a key on the line above this one is missing its ':', or a value")
        lines_out.append("      starts where a key was expected. Check the indentation of this line")
        lines_out.append("      and the one above it.")

    return "\n".join(lines_out)


def load_yaml(path: Path) -> Dict[str, Any]:
    """Read a specification, or explain precisely why it could not be read.

    The diagnosis runs *before* parsing so that the known accidents are reported
    even when the parser's own message points somewhere unhelpful -- which, for an
    escaped comment marker, is the first such line rather than the real cause.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except UnicodeDecodeError as exc:
        raise SpecReadError(
            f"{path} is not valid UTF-8 ({exc}).\n"
            "  Re-save it as UTF-8; the specification and the files this produces are UTF-8."
        ) from None

    lines = text.splitlines()
    escaped = [i + 1 for i, line in enumerate(lines) if line.lstrip().startswith("\\#")]
    if escaped:
        shown = ", ".join(str(n) for n in escaped[:6])
        more = f" (and {len(escaped) - 6} more)" if len(escaped) > 6 else ""
        raise SpecReadError(
            f"{path} is not valid YAML: {len(escaped)} line(s) begin with an escaped "
            f"comment marker '\\#', at line {shown}{more}.\n"
            "\n"
            "  A backslash before '#' makes it an ordinary character, so the parser reads\n"
            "  those lines as data rather than comments, and then fails on the first of\n"
            "  them with an unrelated-looking message.\n"
            "\n"
            "  Fix: delete the backslashes, so the lines start with '#' again.\n"
            "  This is what happens when a file is copied through a tool that escapes\n"
            "  Markdown punctuation."
        )

    nbsp = text.count("\u00a0")
    if nbsp:
        raise SpecReadError(
            f"{path} is not valid YAML: {nbsp} non-breaking space(s) (U+00A0).\n"
            "\n"
            "  YAML indentation must be ordinary spaces; U+00A0 is not whitespace to the\n"
            "  parser, so it silently becomes part of a key or a value.\n"
            "\n"
            "  Fix: replace every U+00A0 with an ordinary space. In an editor, use\n"
            "  find-and-replace on the character U+00A0 (non-breaking space). A command\n"
            "  line can do it without typing the character:\n"
            "      python -c \"import pathlib,sys;p=pathlib.Path(sys.argv[1]);"
            "p.write_text(p.read_text(encoding='utf-8').replace(chr(0xA0),' '),encoding='utf-8')\" FILE"
        )

    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SpecReadError(_describe_yaml_failure(path, text, exc)) from None

    if not isinstance(value, dict):
        kind = type(value).__name__ if value is not None else "an empty document"
        extra = ""
        if isinstance(value, list) and value:
            first = str(value[0])
            if isinstance(value[0], dict):
                extra = (
                    f"\n  The file looks like a list of sections. A specification is a\n"
                    f"  mapping of section names, so each of these becomes a key:\n\n"
                    f"      {first[:90]}\n"
                )
        elif value is None or (isinstance(value, str) and not value.strip()):
            extra = (
                "\n  The file has no content, or only comments. It needs at least\n"
                "  `schemaVersion: \"5.1.0\"` and the sections the migration names."
            )
        raise SpecReadError(
            f"{path}: expected a YAML mapping at the document root, found {kind}.\n"
            "  A specification is a mapping of named sections, so the file must start\n"
            f"  with a key such as `schemaVersion: \"5.1.0\"` at column zero.{extra}"
        )
    return value


def dump_yaml(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        yaml.safe_dump(
            value,
            handle,
            sort_keys=False,
            allow_unicode=True,
            width=100,
            default_flow_style=False,
        )


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def qident(name: str) -> str:
    """Quote an identifier. cTunnel inherits Oracle, so double quotes delimit."""
    return '"' + str(name).replace('"', '""') + '"'


def pg_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def sql_literal(value: Any) -> str:
    """Render a specification value as a SQL literal."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    return "'" + str(value).replace("'", "''") + "'"


def relation(schema: Optional[str], table: str) -> str:
    parts = [schema, table] if schema else [table]
    return ".".join(qident(part) for part in parts if part)


def relation_parts(value: str) -> Tuple[Optional[str], str]:
    """Split ``SCHEMA.TABLE`` honouring quoted segments."""
    parts = [p.strip() for p in str(value).split(".")]
    if len(parts) == 1:
        return None, parts[0]
    return parts[-2], parts[-1]


def strip_quotes(value: str) -> str:
    text = str(value).strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1].replace('""', '"')
    return text


def normalized_relation_name(schema: str, table: str) -> str:
    return f"{schema}_{table}".replace("-", "_").replace(".", "_").lower()


def sanitize_alias(value: str) -> str:
    """Turn a relation or column name into a safe lowercase SQL alias."""
    alias = re.sub(r"[^0-9a-zA-Z_]+", "_", str(value)).strip("_").lower()
    if not alias:
        alias = "rel"
    if alias[0].isdigit():
        alias = f"t_{alias}"
    return alias


def unique_alias(base: str, taken: Sequence[str]) -> str:
    alias = base
    counter = 1
    while alias in taken:
        counter += 1
        alias = f"{base}{counter}"
    return alias


def regex_escape(value: str) -> str:
    """Escape a literal for use inside an Oracle regular expression."""
    return re.escape(str(value))


def short_hash(value: str, length: int = 8) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:length]


def indent_sql(text: str, prefix: str = "  ") -> str:
    """Indent every non-blank line.

    Deliberately does not strip first: stripping would remove the first line's
    own indentation, so the opening brace of a nested block would land one level
    too shallow while its contents sat at the right depth.
    """
    return textwrap.indent(text, prefix)


# ---------------------------------------------------------------------------
# Source catalog
# ---------------------------------------------------------------------------

@dataclass
class CatalogColumn:
    name: str
    oracle_type: str
    nullable: bool = True
    char_semantics: str = "characters"

    @property
    def is_character(self) -> bool:
        base = self.oracle_type.split("(")[0].strip().upper()
        return base in {"CHAR", "NCHAR", "VARCHAR2", "NVARCHAR2", "VARCHAR", "STRING", "CLOB", "NCLOB", "LONG"}

    @property
    def is_blank_padded(self) -> bool:
        """Oracle CHAR is blank-padded to its declared length; VARCHAR2 is not."""
        return self.oracle_type.split("(")[0].strip().upper() in {"CHAR", "NCHAR", "CHARACTER"}

    @property
    def is_clob(self) -> bool:
        return self.oracle_type.split("(")[0].strip().upper() in {"CLOB", "NCLOB", "LONG"}

    @property
    def is_binary(self) -> bool:
        return self.oracle_type.split("(")[0].strip().upper() in {"RAW", "BLOB", "LONG RAW", "IMAGE"}

    @property
    def is_timestamptz(self) -> bool:
        upper = self.oracle_type.upper()
        return "TIME ZONE" in upper or "LOCAL TIME ZONE" in upper

    @property
    def has_time_component(self) -> bool:
        """Oracle DATE always carries a time component."""
        base = self.oracle_type.split("(")[0].strip().upper()
        if base == "DATE":
            return True
        return base in {"TIMESTAMP", "TIMESTAMP WITH TIME ZONE", "TIMESTAMP WITH LOCAL TIME ZONE"}


@dataclass
class CatalogTable:
    schema: str
    name: str
    columns: List[CatalogColumn]
    primary_key: List[str] = field(default_factory=list)
    unique_indexes: List[List[str]] = field(default_factory=list)
    profile: Dict[str, Any] = field(default_factory=dict)
    # True when the shape came from a specification rule's own `columns:` rather
    # than from a catalog extracted from Oracle. The shape is identical either way,
    # so nothing downstream branches on it -- but a reader of the artifacts does
    # need to know which one they are looking at, because only the second is
    # evidence about the actual database.
    from_spec: bool = False

    def column(self, name: str) -> Optional[CatalogColumn]:
        upper = str(name).upper()
        for column in self.columns:
            if column.name.upper() == upper:
                return column
        return None

    @property
    def key(self) -> Tuple[str, str]:
        return (self.schema.upper(), self.name.upper())

    @property
    def column_names(self) -> List[str]:
        return [column.name for column in self.columns]


@dataclass
class Catalog:
    """Metadata for the Oracle source.

    The specification names several objects by wildcard (``SHOP.*``,
    ``ORDERS_20*``, ``AMOUNT``). Expanding those requires the source catalog, so
    it is an explicit input. Without one, wildcard rules are reported rather
    than guessed.
    """

    tables: List[CatalogTable] = field(default_factory=list)
    source: str = ""

    def __post_init__(self) -> None:
        self._index: Dict[Tuple[str, str], CatalogTable] = {t.key: t for t in self.tables}

    def get(self, schema: str, table: str) -> Optional[CatalogTable]:
        return self._index.get((str(schema).upper(), str(table).upper()))

    def expand(self, schema_pattern: str, name_pattern: str) -> List[CatalogTable]:
        found = [
            item
            for item in self.tables
            if fnmatch.fnmatch(item.schema, schema_pattern)
            and fnmatch.fnmatch(item.name, name_pattern)
        ]
        return sorted(found, key=lambda t: (t.schema, t.name))

    def columns_matching(self, schema_pattern: str, table_pattern: str, column_pattern: str) -> List[Tuple[CatalogTable, CatalogColumn]]:
        found: List[Tuple[CatalogTable, CatalogColumn]] = []
        for item in self.tables:
            if not fnmatch.fnmatch(item.schema, schema_pattern) or not fnmatch.fnmatch(item.name, table_pattern):
                continue
            for column in item.columns:
                if fnmatch.fnmatch(column.name, column_pattern):
                    found.append((item, column))
        return found

    @property
    def empty(self) -> bool:
        return not self.tables


def parse_catalog(spec: Dict[str, Any], catalog: Optional[Dict[str, Any]], source: str) -> Catalog:
    raw_tables: List[Dict[str, Any]] = []
    if catalog:
        raw_tables = list(catalog.get("tables") or [])
    else:
        # `spec.catalog` is either an inline mapping of tables or the name of a
        # file to read, which `_load_optional_catalog` resolves. Only the inline
        # form carries tables here; a string is a path, and treating it as a
        # mapping would fail on the very field that was just made legal.
        inline = spec.get("catalog")
        if isinstance(inline, dict):
            raw_tables = list(inline.get("tables") or [])

    tables: List[CatalogTable] = []
    for raw in raw_tables:
        schema = str(raw.get("schema") or "")
        name = str(raw.get("name") or "")
        if not schema or not name:
            continue
        columns: List[CatalogColumn] = []
        for entry in raw.get("columns") or []:
            if isinstance(entry, str):
                columns.append(CatalogColumn(name=strip_quotes(entry), oracle_type="VARCHAR2(255)"))
                continue
            columns.append(
                CatalogColumn(
                    name=str(entry.get("name")),
                    oracle_type=str(entry.get("type") or "VARCHAR2(255)"),
                    nullable=bool(entry.get("nullable", True)),
                    char_semantics=str(entry.get("charSemantics") or "characters"),
                )
            )
        tables.append(
            CatalogTable(
                schema=schema,
                name=name,
                columns=columns,
                primary_key=[str(v) for v in raw.get("primaryKey") or []],
                unique_indexes=[[str(c) for c in idx] for idx in raw.get("uniqueIndexes") or []],
                profile=dict(raw.get("profile") or {}),
            )
        )
    return Catalog(tables=tables, source=source)


# ---------------------------------------------------------------------------
# Specification validation
# ---------------------------------------------------------------------------
#
# Stage 1 of the orchestration flow. Nothing downstream may run until this
# passes, because every later stage trusts the shape of the specification.

def _require(mapping: Dict[str, Any], key: str, where: str, diags: Diagnostics) -> Any:
    if key not in mapping:
        diags.add(
            "SPEC_FIELD_MISSING",
            "BLOCK",
            f"{where} is missing the required field `{key}`",
            field=key,
        )
        return None
    return mapping[key]


def validate_spec(spec: Dict[str, Any], catalog: Catalog, diags: Diagnostics) -> None:
    """Structural and semantic validation of the specification itself.

    Emits ``BLOCK`` diagnostics for anything that would make later stages
    unsound, and ``EDGE`` diagnostics for choices the specification leaves to
    the compiler.
    """

    schema_version = str(spec.get("schemaVersion") or "")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        diags.add(
            "SPEC_SCHEMA_VERSION_UNSUPPORTED",
            "BLOCK",
            f"schemaVersion {schema_version or '<absent>'} is not one of "
            f"{sorted(SUPPORTED_SCHEMA_VERSIONS)}",
            supported=sorted(SUPPORTED_SCHEMA_VERSIONS),
        )

    engines = spec.get("engines") or {}
    for side in ("source", "target"):
        side_spec = engines.get(side) or {}
        if not side_spec.get("engine"):
            diags.add("SPEC_ENGINE_MISSING", "BLOCK", f"engines.{side}.engine is required", side=side)

    _validate_scope(spec, catalog, diags)
    _validate_rules(spec, diags)
    _validate_run(spec, diags)
    _validate_acceptance(spec, diags)
    _validate_sensitive(spec, diags)
    _validate_object_policies(spec, catalog, diags)
    _validate_catalog_consistency(spec, catalog, diags)


def _validate_scope(spec: Dict[str, Any], catalog: Catalog, diags: Diagnostics) -> None:
    scope = spec.get("scope") or {}
    include = list(scope.get("include") or [])
    if not include:
        diags.add("SCOPE_EMPTY", "BLOCK", "scope.include names no objects")
        return

    for entry in include:
        if not entry.get("objectClass"):
            diags.add("SCOPE_ENTRY_INCOMPLETE", "BLOCK", f"scope entry has no objectClass: {entry}")
        if not entry.get("schema") or not entry.get("name"):
            diags.add("SCOPE_ENTRY_INCOMPLETE", "BLOCK", f"scope entry needs schema and name: {entry}")

    # A wildcard-only include cannot be enumerated without the catalog. Say so
    # once, loudly, rather than silently compiling a subset.
    if catalog.empty:
        wildcards = [e for e in include if "*" in str(e.get("name")) or "*" in str(e.get("schema"))]
        if wildcards:
            diags.add(
                "SCOPE_WILDCARD_NEEDS_CATALOG",
                "BLOCK",
                "scope.include uses wildcards but no source catalog was supplied; "
                "pass --catalog so the tables can be enumerated",
                wildcards=[e.get("name") for e in wildcards],
            )


def _validate_rules(spec: Dict[str, Any], diags: Diagnostics) -> None:
    seen_ids: Dict[str, str] = {}

    for section in ("rules", "overrides"):
        for rule in spec.get(section) or []:
            rule_id = str(rule.get("id") or "<unnamed>")
            where = f"{section}[{rule_id}]"

            if rule.get("id"):
                if rule_id in seen_ids:
                    diags.add(
                        "RULE_ID_DUPLICATE",
                        "BLOCK",
                        f"rule id `{rule_id}` is used more than once "
                        f"(also in {seen_ids[rule_id]})",
                        rule_id=rule_id,
                    )
                else:
                    seen_ids[rule_id] = section
            else:
                diags.add("RULE_ID_MISSING", "BLOCK", f"{where} has no id", rule_id=rule_id)

            match = rule.get("match") or {}
            if not match.get("objectClass"):
                diags.add("RULE_MATCH_INCOMPLETE", "BLOCK", f"{where} has no match.objectClass", rule_id=rule_id)
            if match.get("objectClass") == "column" and not match.get("column"):
                diags.add("RULE_MATCH_INCOMPLETE", "BLOCK", f"{where} matches a column but names no column", rule_id=rule_id)

            steps = rule.get("steps")
            if steps is not None and not isinstance(steps, list):
                diags.add("RULE_STEPS_NOT_A_LIST", "BLOCK", f"{where}.steps is not a list", rule_id=rule_id)
                continue

            for index, step in enumerate(steps or []):
                _validate_step(step, f"{where}.steps[{index}]", rule_id, diags)

            # An override is only meaningful if it names the same thing a rule
            # also names; otherwise it silently applies to nothing.
            if section == "overrides" and match.get("objectClass") == "column":
                matched_any = any(
                    (other.get("match") or {}).get("objectClass") == "column"
                    for other in (spec.get("rules") or [])
                )
                if not matched_any:
                    diags.add(
                        "OVERRIDE_SHADOWS_NOTHING",
                        "EDGE",
                        f"{where} is an override but no column rule exists to override",
                        rule_id=rule_id,
                    )


def _validate_step(step: Dict[str, Any], where: str, rule_id: str, diags: Diagnostics) -> None:
    category = step.get("category")
    if not category:
        diags.add("STEP_CATEGORY_MISSING", "BLOCK", f"{where} has no category", rule_id=rule_id)
        return

    if category not in KNOWN_CATEGORIES:
        diags.add(
            "STEP_CATEGORY_UNKNOWN",
            "BLOCK",
            f"{where} uses category `{category}`, which this compiler does not implement; "
            f"known categories are {sorted(KNOWN_CATEGORIES)}",
            rule_id=rule_id,
            category=category,
        )
        return

    operation = step.get("operation")

    if category == "value" and operation is None and "from" not in step and "input" not in step:
        diags.add("STEP_VALUE_NO_OPERATION", "BLOCK", f"{where} is a value step with no operation", rule_id=rule_id)
    if category == "derived" and operation is None:
        diags.add("STEP_DERIVED_NO_OPERATION", "BLOCK", f"{where} is a derived step with no operation", rule_id=rule_id)

    if operation and category in CATEGORY_OPERATIONS:
        # The rulebook's `categories` key, not a table spelled out here: which
        # operations a category allows is a rule a deployment must be able to
        # change, and an operation missing from the list blocks rather than
        # reaching the compiler (STEP_OPERATION_UNKNOWN).
        allowed = CATEGORY_OPERATIONS[category]
        if operation not in allowed:
            diags.add(
                "STEP_OPERATION_UNKNOWN",
                "BLOCK",
                f"{where} uses operation `{operation}`, which this compiler does not implement "
                f"for category `{category}`",
                rule_id=rule_id,
                category=category,
                operation=operation,
            )

    if category == "derived" and operation in {"concat", "coalesce", "arithmetic", "cast-as"}:
        inputs = step.get("inputs") or []
        if len(inputs) < 2:
            diags.add(
                "STEP_DERIVED_TOO_FEW_INPUTS",
                "BLOCK",
                f"{where} needs at least two inputs, found {len(inputs)}",
                rule_id=rule_id,
            )
        if operation == "arithmetic":
            operator = (step.get("parameters") or {}).get("operator")
            if operator not in ARITHMETIC_OPERATORS:
                diags.add(
                    "STEP_ARITHMETIC_OPERATOR_UNKNOWN",
                    "BLOCK",
                    f"{where} uses arithmetic operator `{operator}`; "
                    f"supported: {sorted(ARITHMETIC_OPERATORS)}",
                    rule_id=rule_id,
                )

    if category == "row-selection":
        _validate_predicate(step.get("predicate"), f"{where}.predicate", rule_id, diags)

    if category == "column-split":
        if not step.get("column"):
            diags.add("STEP_COLUMN_SPLIT_NO_COLUMN", "BLOCK", f"{where} has no `column`", rule_id=rule_id)
        if not step.get("targets"):
            diags.add("STEP_COLUMN_SPLIT_NO_TARGETS", "BLOCK", f"{where} has no `targets`", rule_id=rule_id)
        delimiter = (step.get("parameters") or {}).get("delimiter")
        if delimiter is None or delimiter == "":
            diags.add(
                "STEP_COLUMN_SPLIT_EMPTY_DELIMITER",
                "BLOCK",
                f"{where} has an empty delimiter, which cannot be split on",
                rule_id=rule_id,
            )

    if category == "lookup":
        if not step.get("from"):
            diags.add("STEP_LOOKUP_NO_SOURCE", "BLOCK", f"{where} has no `from` relation", rule_id=rule_id)
        if not step.get("using") or not step.get("onKey"):
            diags.add(
                "STEP_LOOKUP_NO_KEY",
                "BLOCK",
                f"{where} needs both `using` (this side) and `onKey` (the other side)",
                rule_id=rule_id,
            )
        elif len(step["using"]) != len(step["onKey"]):
            diags.add(
                "STEP_LOOKUP_KEY_ARITY",
                "BLOCK",
                f"{where} has {len(step['using'])} `using` columns and {len(step['onKey'])} `onKey` columns",
                rule_id=rule_id,
            )
        cardinality = step.get("cardinality")
        if cardinality and cardinality not in {"at-most-one", "many", "exactly-one"}:
            diags.add(
                "STEP_LOOKUP_CARDINALITY_UNKNOWN",
                "BLOCK",
                f"{where} uses cardinality `{cardinality}`; supported: at-most-one, many, exactly-one",
                rule_id=rule_id,
            )

    if category == "pivot":
        on_column = step.get("on")
        if not on_column:
            diags.add("STEP_PIVOT_NO_ON", "BLOCK", f"{where} has no `on` column", rule_id=rule_id)
        if not step.get("grain"):
            diags.add("STEP_PIVOT_NO_GRAIN", "BLOCK", f"{where} has no `grain`", rule_id=rule_id)
        if not step.get("intoColumns"):
            diags.add("STEP_PIVOT_NO_TARGETS", "BLOCK", f"{where} has no `intoColumns`", rule_id=rule_id)
        aggregate = (step.get("aggregate") or {}).get("operation")
        if aggregate and aggregate not in PIVOT_AGGREGATES:
            diags.add(
                "STEP_PIVOT_AGGREGATE_UNKNOWN",
                "BLOCK",
                f"{where} uses aggregate `{aggregate}`; supported: {sorted(PIVOT_AGGREGATES)}",
                rule_id=rule_id,
            )

    if category == "cardinality":
        operation = step.get("operation")
        if operation == "merge" and not step.get("sources"):
            diags.add("STEP_MERGE_NO_SOURCES", "BLOCK", f"{where} is a merge with no `sources`", rule_id=rule_id)
        if operation == "split" and not step.get("targets"):
            diags.add("STEP_SPLIT_NO_TARGETS", "BLOCK", f"{where} is a split with no `targets`", rule_id=rule_id)

    if category == "relational" and step.get("operation") == "denormalise":
        shape = step.get("from") or {}
        if not shape.get("driving"):
            diags.add("STEP_DENORMALISE_NO_DRIVING", "BLOCK", f"{where} has no `from.driving` relation", rule_id=rule_id)
        if not step.get("columns"):
            diags.add("STEP_DENORMALISE_NO_COLUMNS", "BLOCK", f"{where} has no `columns` mapping", rule_id=rule_id)
        for join in shape.get("joins") or []:
            jtype = join.get("type")
            if jtype and jtype not in JOIN_TYPES:
                diags.add(
                    "STEP_JOIN_TYPE_UNKNOWN",
                    "BLOCK",
                    f"{where} uses join type `{jtype}`; supported: {sorted(JOIN_TYPES)}",
                    rule_id=rule_id,
                )
            if not join.get("joinOn"):
                diags.add(
                    "STEP_JOIN_NO_CONDITION",
                    "BLOCK",
                    f"{where} joins {join.get('relation')} with no joinOn condition, which would be a cartesian product",
                    rule_id=rule_id,
                )

    if category == "escape":
        language = step.get("language")
        if language not in {"sql-scalar", "sql-set"}:
            diags.add(
                "STEP_ESCAPE_LANGUAGE_UNKNOWN",
                "BLOCK",
                f"{where} uses escape language `{language}`; this compiler implements "
                "sql-scalar and sql-set only",
                rule_id=rule_id,
            )
        if not step.get("body"):
            diags.add("STEP_ESCAPE_NO_BODY", "BLOCK", f"{where} has no `body`", rule_id=rule_id)

    if category == "key-declaration" and not step.get("columns"):
        diags.add("STEP_KEY_NO_COLUMNS", "BLOCK", f"{where} declares no key columns", rule_id=rule_id)

    if category == "change-aware" and not step.get("columns"):
        diags.add(
            "STEP_CHANGE_AWARE_NO_COLUMNS",
            "BLOCK",
            f"{where} names no columns to watch",
            rule_id=rule_id,
        )


def _validate_predicate(node: Any, where: str, rule_id: Optional[str], diags: Diagnostics) -> None:
    if not isinstance(node, dict):
        diags.add("PREDICATE_NOT_A_MAPPING", "BLOCK", f"{where} is not a mapping", rule_id=rule_id)
        return

    if "allOf" in node or "anyOf" in node:
        key = "allOf" if "allOf" in node else "anyOf"
        children = node.get(key)
        if not isinstance(children, list) or not children:
            diags.add("PREDICATE_GROUP_EMPTY", "BLOCK", f"{where}.{key} is empty", rule_id=rule_id)
            return
        for index, child in enumerate(children):
            _validate_predicate(child, f"{where}.{key}[{index}]", rule_id, diags)
        return

    if not node.get("column"):
        diags.add("PREDICATE_NO_COLUMN", "BLOCK", f"{where} has no column", rule_id=rule_id)

    comparison = node.get("comparison")
    if comparison == "is-null":
        return
    if comparison not in PREDICATE_OPERATORS:
        diags.add(
            "PREDICATE_COMPARISON_UNKNOWN",
            "BLOCK",
            f"{where} uses comparison `{comparison}`; supported: "
            f"{sorted(PREDICATE_OPERATORS)} plus is-null",
            rule_id=rule_id,
        )
    if not node.get("comparisonColumn") and "literal" not in node:
        diags.add(
            "PREDICATE_NO_RIGHT_HAND_SIDE",
            "BLOCK",
            f"{where} has neither `literal` nor `comparisonColumn`",
            rule_id=rule_id,
        )


def _validate_run(spec: Dict[str, Any], diags: Diagnostics) -> None:
    run = spec.get("run") or {}
    acquisition = run.get("acquisition")
    if acquisition not in {"cdc", "snapshot", "query-incremental"}:
        diags.add(
            "RUN_ACQUISITION_UNKNOWN",
            "BLOCK",
            f"run.acquisition `{acquisition}` is not one of cdc, snapshot, query-incremental",
        )

    capture = run.get("capture")
    watermark = run.get("watermark")
    if acquisition == "query-incremental":
        if not watermark:
            diags.add(
                "RUN_WATERMARK_MISSING",
                "BLOCK",
                "run.acquisition is query-incremental but run.watermark is absent",
            )
        if capture:
            diags.add(
                "RUN_CAPTURE_AND_WATERMARK_BOTH_SET",
                "BLOCK",
                "run.capture and run.watermark are mutually exclusive",
            )
    elif capture and watermark:
        diags.add(
            "RUN_CAPTURE_AND_WATERMARK_BOTH_SET",
            "BLOCK",
            "run.capture and run.watermark are mutually exclusive",
        )

    if capture:
        start = capture.get("startPosition")
        if start == "explicit-scn" and capture.get("scn") is None:
            diags.add(
                "RUN_EXPLICIT_SCN_MISSING",
                "BLOCK",
                "run.capture.startPosition is explicit-scn but no `scn` was given",
            )
        if start and start not in {"before-snapshot", "after-snapshot", "explicit-scn", "latest", "earliest"}:
            diags.add(
                "RUN_START_POSITION_UNKNOWN",
                "BLOCK",
                f"run.capture.startPosition `{start}` is not recognised",
            )

    if watermark:
        if not watermark.get("column"):
            diags.add("RUN_WATERMARK_NO_COLUMN", "BLOCK", "run.watermark has no column")
        strategy = watermark.get("strategy")
        if strategy and strategy not in {"monotonic-key", "timestamp", "full-overlap", "sequence"}:
            diags.add(
                "RUN_WATERMARK_STRATEGY_UNKNOWN",
                "BLOCK",
                f"run.watermark.strategy `{strategy}` is not recognised",
            )
        if watermark.get("detectsDeletes") and strategy == "monotonic-key":
            diags.add(
                "RUN_WATERMARK_CANNOT_DETECT_DELETES",
                "EDGE",
                "run.watermark.detectsDeletes is true with a monotonic-key strategy; "
                "a monotonic key cannot observe a delete, so deletion handling needs a second mechanism",
            )


def _validate_acceptance(spec: Dict[str, Any], diags: Diagnostics) -> None:
    acceptance = spec.get("acceptance") or {}
    checks = list(acceptance.get("checks") or [])
    if not checks:
        diags.add("ACCEPTANCE_NO_CHECKS", "BLOCK", "acceptance.checks is empty, so nothing can be validated")
        return

    known = {
        "row-count-exact",
        "chunk-digest-match",
        "no-orphan-keys",
        "referential-integrity",
        "quarantine-empty",
        "aggregate-match",
        "behaviour-equivalence",
        "column-value-domain",
        "monotonic-key-unique",
    }
    for check in checks:
        if check not in known:
            diags.add(
                "ACCEPTANCE_CHECK_UNKNOWN",
                "BLOCK",
                f"acceptance check `{check}` is not implemented; supported: {sorted(known)}",
                check=check,
            )

    max_quarantined = acceptance.get("maxQuarantinedRows")
    if max_quarantined is not None and "quarantine-empty" in checks:
        diags.add(
            "ACCEPTANCE_QUARANTINE_TOLERANCE",
            "EDGE",
            f"acceptance.maxQuarantinedRows is {max_quarantined}, so quarantine-empty "
            "is really a bounded-tolerance check, not an empty check",
            maxQuarantinedRows=max_quarantined,
        )

    for index, strict in enumerate(acceptance.get("stricter") or []):
        if not strict.get("match"):
            diags.add(
                "ACCEPTANCE_STRICTER_NO_MATCH",
                "BLOCK",
                f"acceptance.stricter[{index}] has no `match`",
            )
        tolerance = (strict.get("aggregateTolerance") or {}).get("value")
        if tolerance not in (None, 0):
            diags.add(
                "GOVERNANCE_TOLERANCE_ABOVE_ZERO",
                "GOVERNANCE",
                f"acceptance.stricter[{index}] allows a tolerance of {tolerance}; "
                "governance.requiresApproval names tolerance-above-zero",
                rule_id=f"acceptance.stricter[{index}]",
                tolerance=tolerance,
            )


def _validate_sensitive(spec: Dict[str, Any], diags: Diagnostics) -> None:
    """Every `handling` must be backed by a rule, or the data leaks or disappears.

    ``sensitive[].handling: mask`` promises masking; only a `mask` value step can
    deliver it. A mismatch is a real finding, not a style issue, so it blocks.
    """
    rules = list(spec.get("rules") or []) + list(spec.get("overrides") or [])
    by_id = {str(r.get("id")): r for r in rules if r.get("id")}

    def rule_for(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        match = entry.get("match") or {}
        for rule in rules:
            rmatch = rule.get("match") or {}
            if rmatch.get("objectClass") != match.get("objectClass"):
                continue
            if rmatch.get("schema") == match.get("schema") and rmatch.get("name") == match.get("name"):
                if rmatch.get("column") == match.get("column"):
                    return rule
        return None

    for entry in spec.get("sensitive") or []:
        match = entry.get("match") or {}
        where = f"sensitive[{match.get('schema')}.{match.get('name')}.{match.get('column')}]"
        handling = entry.get("handling")
        if handling not in {"keep", "mask", "drop", "encrypt", "tokenise", "redact"}:
            diags.add("SENSITIVE_HANDLING_UNKNOWN", "BLOCK", f"{where} uses handling `{handling}`")
            continue
        if not entry.get("class"):
            diags.add("SENSITIVE_NO_CLASS", "EDGE", f"{where} has no classification")

        rule = rule_for(entry)
        if rule is None:
            if handling == "keep":
                continue
            diags.add(
                "SENSITIVE_NO_RULE",
                "BLOCK",
                f"{where} declares handling `{handling}` but no rule matches that column, "
                "so the declared handling cannot be delivered",
                handling=handling,
            )
            continue

        operations = {str(step.get("operation")) for step in rule.get("steps") or []}
        categories = {str(step.get("category")) for step in rule.get("steps") or []}
        if handling == "mask" and "mask" not in operations:
            diags.add(
                "SENSITIVE_MASK_NOT_IMPLEMENTED",
                "BLOCK",
                f"{where} declares handling `mask` but rule `{rule.get('id')}` "
                f"has operations {sorted(operations)}",
                rule_id=str(rule.get("id")),
            )
        if handling == "drop" and not (operations & DROP_OPS) and "drop-column" not in operations:
            diags.add(
                "SENSITIVE_DROP_NOT_IMPLEMENTED",
                "BLOCK",
                f"{where} declares handling `drop` but rule `{rule.get('id')}` "
                f"has operations {sorted(operations)}",
                rule_id=str(rule.get("id")),
            )


def _validate_object_policies(spec: Dict[str, Any], catalog: Catalog, diags: Diagnostics) -> None:
    known_carries = {
        "translate",
        "reimplement-in-target",
        "drop-deliberately",
        "relocate-to-application",
        "blocked-no-path",
    }
    for index, policy in enumerate(spec.get("objectPolicies") or []):
        where = f"objectPolicies[{index}]"
        match = policy.get("match") or {}
        if not match.get("objectClass"):
            diags.add("POLICY_NO_OBJECT_CLASS", "BLOCK", f"{where} has no match.objectClass")
        carry = policy.get("carry")
        if carry not in known_carries:
            diags.add(
                "POLICY_CARRY_UNKNOWN",
                "BLOCK",
                f"{where} uses carry `{carry}`; supported: {sorted(known_carries)}",
            )
        contract = policy.get("behaviourContract") or {}
        if not contract.get("mustPreserve"):
            diags.add("POLICY_NO_MUST_PRESERVE", "EDGE", f"{where} has no mustPreserve list")
        if carry == "relocate-to-application" and not (contract.get("acknowledgedLoss") or {}).get("acknowledgedBy"):
            diags.add(
                "POLICY_LOSS_NOT_ACKNOWLEDGED",
                "BLOCK",
                f"{where} relocates to the application but acknowledges no loss",
            )
        if carry == "blocked-no-path":
            diags.add(
                "OBJECT_BLOCKED_NO_PATH",
                "BLOCK",
                f"{where} declares carry `blocked-no-path` for "
                f"{match.get('objectClass')} {match.get('schema')}.{match.get('name')}; "
                "there is no target equivalent, so this blocks the run",
                object=f"{match.get('objectClass')} {match.get('schema')}.{match.get('name')}",
            )
        for dependency in policy.get("dependsOn") or []:
            if not dependency.get("object"):
                diags.add("POLICY_DEPENDENCY_INCOMPLETE", "BLOCK", f"{where}.dependsOn entry has no object")


def _validate_catalog_consistency(spec: Dict[str, Any], catalog: Catalog, diags: Diagnostics) -> None:
    """Every column the specification names must exist in the catalog."""
    if catalog.empty:
        return

    def resolve_table(schema: str, name: str) -> Optional[CatalogTable]:
        return catalog.get(schema, name)

    for section in ("rules", "overrides"):
        for rule in spec.get(section) or []:
            match = rule.get("match") or {}
            rule_id = str(rule.get("id"))
            schema = str(match.get("schema") or "")
            name = str(match.get("name") or "")
            if match.get("objectClass") not in {"table", "column"}:
                continue
            if "*" in schema or "*" in name:
                continue

            table = resolve_table(schema, name)
            if table is None:
                diags.add(
                    "CATALOG_TABLE_MISSING",
                    "BLOCK",
                    f"rule `{rule_id}` matches {schema}.{name}, which the catalog does not contain",
                    rule_id=rule_id,
                )
                continue

            column_name = match.get("column")
            if column_name and "*" not in str(column_name):
                if table.column(str(column_name)) is None:
                    # A column rule whose steps do not read the matched column is
                    # declaring a *new* column, not transforming an existing one:
                    # `priority-from-amount` computes PRIORITY from AMOUNT and
                    # Oracle has no PRIORITY. That is legal, but only when nothing
                    # actually reads the missing column.
                    if _rule_reads_column(rule, str(column_name)):
                        diags.add(
                            "CATALOG_COLUMN_MISSING",
                            "BLOCK",
                            f"rule `{rule_id}` matches {schema}.{name}.{column_name}, which the "
                            "catalog does not contain and whose steps read it",
                            rule_id=rule_id,
                        )
                    else:
                        diags.add(
                            "CATALOG_COLUMN_PRODUCED",
                            "EDGE",
                            f"rule `{rule_id}` names column `{column_name}`, which does not exist in "
                            f"{schema}.{name}. Its steps do not read it, so the rule declares a new "
                            "target column rather than transforming an existing one",
                            rule_id=rule_id,
                        )

            for step in rule.get("steps") or []:
                for referenced in _referenced_columns(step):
                    if table.column(referenced) is None:
                        diags.add(
                            "CATALOG_COLUMN_MISSING",
                            "EDGE",
                            f"rule `{rule_id}` step references column `{referenced}` "
                            f"on {schema}.{name}, which the catalog does not contain",
                            rule_id=rule_id,
                            column=referenced,
                        )


def _rule_reads_column(rule: Dict[str, Any], column: str) -> bool:
    """Does any step of this rule actually read the column the rule matches?

    This is subtler than it looks, because a step can read the matched column
    *implicitly*. A `value` step with no `input` and no `from` operates on the
    column the rule matched -- that is the whole point of a column recipe. A
    wildcard rule such as `{name: "*", column: AMOUNT}` with a bare `cast` step
    therefore reads AMOUNT on every table, and treating it as a rule that
    *produces* AMOUNT would invent the column on tables that have none.
    """
    target = column
    for step in rule.get("steps") or []:
        category = str(step.get("category"))

        # A bare value or structural step operates on the matched column.
        if category in {"value", "structural"} and not step.get("input") and not step.get("from"):
            return True
        if category == "structural" and step.get("operation") == "drop-column":
            return True

        for referenced in _referenced_columns(step):
            if referenced == target:
                return True
        if any(str(value) == target for value in step.get("declaredInputs") or []):
            return True
        body = str(step.get("body") or "")
        if body and target in body:
            return True
    return False


def _referenced_columns(step: Dict[str, Any]) -> List[str]:
    """Column names a step reads, for catalog cross-checking."""
    names: List[str] = []
    for key in ("column", "input", "from"):
        if isinstance(step.get(key), str):
            names.append(step[key])
    for key in ("inputs", "using", "onKey", "grain", "columns", "orderBy", "tieBreak"):
        for value in step.get(key) or []:
            if isinstance(value, str):
                names.append(value)
    predicate = step.get("predicate")
    if isinstance(predicate, dict):
        names.extend(collect_predicate_columns(predicate))
    aggregate = step.get("aggregate") or {}
    if isinstance(aggregate.get("column"), str):
        names.append(aggregate["column"])
    if isinstance(step.get("on"), str):
        names.append(step["on"])
    return [n for n in names if not str(n).endswith("_offset")]


# ---------------------------------------------------------------------------
# Rule resolution
# ---------------------------------------------------------------------------

def rules(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(spec.get("rules") or [])


def overrides(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(spec.get("overrides") or [])


def specificity(match: Dict[str, Any]) -> int:
    """Rank how precisely a rule names its target.

    Higher wins. An exact table beats a wildcard; an exact column beats a
    wildcard; naming an object class beats not naming one.
    """
    score = 0
    object_class = str(match.get("objectClass") or "")
    score += {"column": 6, "table": 4}.get(object_class, 2)

    for key, weight in (("schema", 4), ("name", 3), ("column", 5)):
        value = match.get(key)
        if value is None:
            continue
        if "*" in str(value):
            score -= weight // 2
        else:
            score += weight

    return score


def ordered_rules(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """All rules in application order: least specific first, overrides last.

    Application order matters because a later step operates on the expression a
    earlier step produced. ``overrides`` come last because the specification
    states they win whatever the specificity.
    """
    indexed: List[Tuple[int, int, int, Dict[str, Any]]] = []
    for position, rule in enumerate(rules(spec)):
        indexed.append((specificity(rule.get("match") or {}), 0, position, rule))
    for position, rule in enumerate(overrides(spec)):
        indexed.append((specificity(rule.get("match") or {}), 1, position, rule))
    indexed.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
    return [entry[3] for entry in indexed]


def matches_table(match: Dict[str, Any], object_class: str, schema: str, table: str) -> bool:
    """Does this rule apply to this *relation*?

    Column rules are matched at relation level too: a column rule is collected
    for a table first and matched against individual columns afterwards. Asking
    "does this column rule apply to SHOP.CUSTOMER" is a table-level question, so
    it must not be answered by comparing `*` against a column pattern -- which is
    how every column recipe in a specification can silently fail to apply.
    """
    if str(match.get("objectClass") or "") != object_class:
        return False
    if not fnmatch.fnmatch(str(schema), str(match.get("schema", "*"))):
        return False
    return bool(fnmatch.fnmatch(str(table), str(match.get("name", "*"))))


def matches_column(match: Dict[str, Any], column: str) -> bool:
    return bool(fnmatch.fnmatch(str(column), str(match.get("column", "*"))))


def matches(
    match: Dict[str, Any],
    object_class: str,
    schema: str,
    table: str,
    column: Optional[str] = None,
) -> bool:
    if not matches_table(match, object_class, schema, table):
        return False
    if object_class == "column":
        return column is not None and matches_column(match, column)
    return True


def split_column_rules(
    column_rules: Sequence[Dict[str, Any]],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Separate column recipes into value transforms and pure renames.

    Two rules matching the same column is the case the specification calls out:
    written as two recipes they "compete for the same column and only one would
    apply". So among *transforms* the highest-precedence rule wins -- which is
    what makes an override replace the rule it overrides instead of stacking on
    top of it.

    A rule with no steps is different. A rename is a statement about the target
    name, not a transformation, so it has to survive alongside the transform on
    the same column: `status-is-renamed` renames STATUS to status_code while
    `normalize-customer-status` rewrites the value, and dropping either one
    silently changes the result.

    Input is already in ascending precedence order, so the last assignment wins.
    """
    transforms: Dict[str, Dict[str, Any]] = {}
    renames: Dict[str, Dict[str, Any]] = {}
    for rule in column_rules:
        column = str((rule.get("match") or {}).get("column") or "")
        if not column:
            continue
        if rule.get("steps"):
            transforms[column.upper()] = rule
        else:
            renames[column.upper()] = rule
    return transforms, renames


def column_rules_for(
    spec: Dict[str, Any],
    schema: str,
    table: str,
    catalog: Optional["Catalog"] = None,
) -> List[Dict[str, Any]]:
    """Column recipes bound to one table, in application order.

    A wildcard column recipe -- ``{name: "*", column: AMOUNT}`` -- matches every
    table in the schema at *relation* level, but it only means anything for a
    table that actually has an AMOUNT column. Without this filter such a rule
    invents an AMOUNT column on every table in the estate, which is how one
    wildcard rule ends up projecting a column that does not exist anywhere.
    """
    found: List[Dict[str, Any]] = []
    catalog_table = catalog.get(schema, table) if catalog is not None else None

    for rule in ordered_rules(spec):
        match = rule.get("match") or {}
        if not matches_table(match, "column", schema, table):
            continue
        column = str(match.get("column") or "")
        if catalog_table is not None and column and "*" not in column:
            if catalog_table.column(column) is None and _rule_reads_column(rule, column):
                # The rule transforms a column this table does not have, so the
                # rule simply does not apply here. A rule that computes its value
                # from other columns is kept: it declares a new target column
                # rather than transforming an existing one.
                continue
        found.append(rule)
    return found


def table_rules_for(spec: Dict[str, Any], schema: str, table: str) -> List[Dict[str, Any]]:
    """Table recipes for one table, in application order."""
    return [
        rule
        for rule in ordered_rules(spec)
        if matches_table(rule.get("match") or {}, "table", schema, table)
    ]


def collect_predicate_columns(node: Any) -> List[str]:
    """Every column a predicate reads, in a stable order."""
    result: List[str] = []
    if not isinstance(node, dict):
        return result

    for key in ("allOf", "anyOf"):
        for child in node.get(key) or []:
            result.extend(collect_predicate_columns(child))

    if node.get("column"):
        result.append(str(node["column"]))
    if node.get("comparisonColumn"):
        result.append(str(node["comparisonColumn"]))

    return list(dict.fromkeys(result))


# ---------------------------------------------------------------------------
# Naming policy
# ---------------------------------------------------------------------------

class Naming:
    """Applies the specification's `naming` block to every identifier.

    ``naming`` is a contract, not a suggestion: a table the specification calls
    ``shop.client`` must arrive as ``shop.client`` whether or not that name was
    legal in the source.
    """

    def __init__(self, spec: Dict[str, Any], diags: Diagnostics) -> None:
        naming = spec.get("naming") or {}
        self.diags = diags
        self.case = str(naming.get("case") or "preserve")
        self.quote_reserved = str(naming.get("onReservedOrSpecial") or "quote") == "quote"
        self.on_too_long = str(naming.get("onTooLong") or "error")
        self.package_prefix = str(naming.get("packageMembers") or "")

        schema_policy = naming.get("schema") if isinstance(naming.get("schema"), dict) else {}
        self.schema_policy = str(schema_policy.get("policy") or "mirror")
        self.flatten_into = schema_policy.get("flattenInto")
        self.schema_map: Dict[str, str] = {}
        for entry in schema_policy.get("map") or []:
            if entry.get("source") is not None:
                self.schema_map[str(entry["source"])] = str(entry.get("target") or entry["source"])

        self.shortened: List[Dict[str, str]] = []
        self.quoted: List[Dict[str, str]] = []

    # -- case ---------------------------------------------------------------

    def apply_case(self, name: str) -> str:
        if self.case == "lower":
            return name.lower()
        if self.case == "upper":
            return name.upper()
        return name

    # -- schema -------------------------------------------------------------

    def schema(self, source_schema: str) -> str:
        if self.schema_policy == "map" and source_schema in self.schema_map:
            mapped = self.schema_map[source_schema]
        elif self.schema_policy == "flatten" and self.flatten_into:
            mapped = str(self.flatten_into)
        else:
            mapped = source_schema
        return self.apply_case(mapped)

    # -- identifiers --------------------------------------------------------

    def identifier(self, name: str, kind: str = "column") -> str:
        """Return the target name for one identifier, applying every policy."""
        result = self.apply_case(str(name))

        needs_quotes = bool(re.fullmatch(r"[a-z_][a-z0-9_$]*", result) is None)
        if not needs_quotes and result.lower() in PG_RESERVED_WORDS:
            needs_quotes = True
        if needs_quotes and self.quote_reserved:
            self.quoted.append({"kind": kind, "name": result})

        result = self._enforce_length(result, kind)
        return result

    def _enforce_length(self, name: str, kind: str) -> str:
        encoded = name.encode("utf-8")
        if len(encoded) <= PG_MAX_IDENTIFIER_BYTES:
            return name

        if self.on_too_long == "shorten-with-hash":
            digest = short_hash(name)
            keep = PG_MAX_IDENTIFIER_BYTES - len(digest) - 1
            shortened = f"{name.encode('utf-8')[:keep].decode('utf-8', 'ignore')}_{digest}"
            self.shortened.append(
                {"kind": kind, "original": name, "shortened": shortened, "bytes": str(len(shortened))}
            )
            return shortened

        if self.on_too_long == "shorten":
            shortened = name.encode("utf-8")[:PG_MAX_IDENTIFIER_BYTES].decode("utf-8", "ignore")
            self.shortened.append(
                {"kind": kind, "original": name, "shortened": shortened, "bytes": str(len(shortened))}
            )
            return shortened

        return name

    def quoted_names(self) -> List[Dict[str, str]]:
        return list(self.quoted)

    def shortened_names(self) -> List[Dict[str, str]]:
        return list(self.shortened)


def split_relation(value: str) -> Tuple[Optional[str], str]:
    schema, table = relation_parts(value)
    return schema, strip_quotes(table)


def merge_source_entry(entry: Any) -> Dict[str, Any]:
    """Normalise one merge source into a mapping.

    The specification allows either ``"SHOP.ORDERS_2023"`` or
    ``{relation: ..., discriminatorValue: ...}``, and a compiler that handles only
    the first silently drops the discriminator of the second.
    """
    if isinstance(entry, str):
        return {"relation": entry}
    if isinstance(entry, dict):
        return dict(entry)
    return {"relation": str(entry)}


# ---------------------------------------------------------------------------
# Type system, defaults and fidelity floor
# ---------------------------------------------------------------------------

def split_oracle_type(text: str) -> Tuple[str, str]:
    """Split an Oracle type into its name and its argument list.

    The arguments are lifted out before the name is matched, because Oracle puts
    them in the middle as well as at the end: ``TIMESTAMP(6) WITH TIME ZONE``
    carries its precision before the timezone qualifier. A regex that only looks
    at the trailing ``(...)`` silently fails to recognise ``VARCHAR2`` too,
    because the digit is not a letter -- which is how a whole column type map
    falls back to text without anybody noticing.
    """
    value = str(text).strip()
    arguments = ""
    match = re.search(r"\(([^()]*)\)", value)
    if match:
        arguments = "(" + match.group(1) + ")"
        value = value[: match.start()] + " " + value[match.end() :]
    name = re.sub(r"\s+", " ", value).strip().upper()
    return name, arguments


def oracle_type_to_pg(oracle_type: str, rule_id: Optional[str] = None, table: Optional[str] = None) -> Tuple[str, List[str]]:
    """Map an Oracle type name onto PostgreSQL, returning the notes taken.

    Precision and scale travel with the type because the fidelity floor depends
    on them. Timezone qualifiers are mapped too: Oracle's LOCAL TIME ZONE and
    WITH TIME ZONE both become ``timestamptz`` in PostgreSQL, which is why the
    specification's ``split-offset`` default exists.
    """
    base, args = split_oracle_type(oracle_type)
    notes: List[str] = []

    if base in {"TIMESTAMP WITH LOCAL TIME ZONE"}:
        notes.append("Oracle TIMESTAMP WITH LOCAL TIME ZONE normalised to PostgreSQL timestamptz")
    if base in {"LONG RAW", "IMAGE"}:
        notes.append(f"Oracle {base} mapped to bytea; binary content is not type-checked by the target")

    target = ORACLE_TO_PG.get(base)
    if target is None:
        # An unrecognised Oracle type must not silently become text.
        notes.append(
            f"Oracle type `{base}` has no mapping in this compiler; it is carried as text "
            "and needs a type mapping before the run can be trusted"
        )
        return "text", notes

    if target == "numeric" and not args:
        notes.append("numeric without precision and scale; precision comes from the source profile")

    if args and target in {"character varying", "character"}:
        # Oracle VARCHAR2(n) already counts characters when NLS_LENGTH_SEMANTICS
        # is CHAR, which defaults.charLengthSemantics states.
        notes.append("character length preserved as declared; Oracle counts characters under CHAR semantics")

    return f"{target}{args}", notes


@dataclass
class TypeResolution:
    """The PostgreSQL type for one target column, plus what it cost."""

    sql: str
    source_type: Optional[str] = None
    fidelity: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    splits_offset: bool = False


class TypeResolver:
    """Resolves target types from the spec's `defaults` and `fidelityFloor`.

    Every branch here corresponds to a real Oracle-to-PostgreSQL difference that
    silently corrupts data if it is ignored, so each one records what it did.
    """

    def __init__(self, spec: Dict[str, Any], catalog: Catalog, diags: Diagnostics) -> None:
        self.spec = spec
        self.catalog = catalog
        self.diags = diags
        defaults = spec.get("defaults") or {}
        self.empty_string_is_null = str(defaults.get("emptyStringIsNull") or "preserve")
        self.char_semantics = str(defaults.get("charLengthSemantics") or "characters")
        self.collation = str(defaults.get("collation") or "binary")
        self.number_without_precision = str(defaults.get("numberWithoutPrecision") or "from-profile")
        self.date_with_time = str(defaults.get("dateWithTimeComponent") or "keep-time")
        self.timestamptz_handling = str(defaults.get("timestampWithTimeZone") or "keep-offset")
        self.fractional_seconds = str(defaults.get("fractionalSecondsBeyondMicroseconds") or "round")
        self.json_target = str(defaults.get("json") or "jsonb")
        self.fidelity_floor = [str(v) for v in spec.get("fidelityFloor") or []]

    # -- declared types from the spec --------------------------------------

    def declared(self, declared: Optional[str], rule_id: Optional[str], table: Optional[str]) -> Optional[TypeResolution]:
        """A type the specification states outright.

        It is normalised through the target dialect rather than copied as text, so
        the reported column type and the emitted DDL agree, and so a type the
        target cannot express is caught here instead of at table-creation time.
        """
        if not declared:
            return None
        text = str(declared)
        if SQLGLOT_AVAILABLE:
            try:
                normalized = exp.DataType.build(text, dialect="pgcontract").sql(dialect="pgcontract")
            except Exception as exc:  # noqa: BLE001
                self.diags.add(
                    "DECLARED_TYPE_INVALID",
                    "BLOCK",
                    f"target type `{text}` is not a valid target type: {exc}",
                    rule_id=rule_id,
                    table=table,
                )
                return TypeResolution(sql="text", source_type=text, assumptions=["declared type rejected"])
            return TypeResolution(
                sql=normalized, source_type=text, fidelity=["declared-by-spec"], assumptions=[]
            )
        return TypeResolution(sql=text, source_type=text, fidelity=["declared-by-spec"])

    # -- types derived from the catalog ------------------------------------

    def from_catalog(self, column: CatalogColumn, table: Optional[str]) -> TypeResolution:
        if not column.oracle_type:
            # The specification named this column but no catalog says what it is.
            # Carrying it as text is the honest answer, and it is reported so the
            # DDL is not mistaken for the final one.
            resolution = TypeResolution(
                sql="text",
                source_type=None,
                assumptions=[
                    f"no source catalog was supplied, so the type of {column.name} is unknown and the "
                    "column is carried as text. Supply --catalog before creating the target table"
                ],
            )
            if self.collation == "binary":
                resolution.fidelity.append('COLLATE "C" is applied so byte ordering matches the source')
            return resolution

        pg_type, notes = oracle_type_to_pg(column.oracle_type)
        resolution = TypeResolution(sql=pg_type, source_type=column.oracle_type, fidelity=list(notes))
        base = split_oracle_type(column.oracle_type)[0]

        if base in {"CLOB", "NCLOB", "LONG"} and self.json_target != "text":
            resolution.sql = self.json_target
            resolution.assumptions.append(
                f"CLOB mapped to {self.json_target} because defaults.json says so; "
                "every row must be valid JSON, which a validation check enforces"
            )

        if base == "DATE" and self.date_with_time == "date-if-profile-proves-no-time":
            # Oracle DATE always stores a time component, so the profile can never
            # prove there is none. Saying otherwise would silently truncate rows.
            resolution.sql = "timestamp(6)"
            resolution.assumptions.append(
                "defaults.dateWithTimeComponent is date-if-profile-proves-no-time but Oracle DATE "
                "always carries a time, so the target column is timestamp(6); a declared `date` in "
                "the spec is still honoured verbatim and flagged"
            )
            self.diags.add(
                "DATE_ALWAYS_HAS_TIME",
                "ASSUMPTION",
                "Oracle DATE always stores a time component, so "
                "defaults.dateWithTimeComponent=date-if-profile-proves-no-time can never be "
                "satisfied; the column is widened to timestamp(6) unless the spec declares a type",
                table=table,
                column=column.name,
            )

        if "HIGH_PRECISION" in self.fidelity_floor and base in {"NUMBER", "NUMERIC", "FLOAT", "BINARY_DOUBLE"}:
            resolution.fidelity.append("numeric precision is carried exactly as declared")

        if column.is_timestamptz and self.timestamptz_handling == "split-offset":
            resolution.splits_offset = True
            resolution.fidelity.append("UTC offset is preserved in a companion <column>_offset column")

        if "SUB_SECOND" in self.fidelity_floor and base.startswith("TIMESTAMP"):
            resolution.sql = "timestamp(6)" if "(" not in resolution.sql else resolution.sql
            if self.fractional_seconds == "round":
                resolution.assumptions.append(
                    "defaults.fractionalSecondsBeyondMicroseconds=round: sub-microsecond digits "
                    "are rounded, not truncated"
                )

        if self.collation == "binary" and resolution.sql.startswith(("character", "text")):
            resolution.fidelity.append("COLLATE \"C\" is applied so byte ordering matches the source")

        if "UNICODE_NORMALISATION" in self.fidelity_floor:
            resolution.assumptions.append(
                "fidelityFloor requires UNICODE_NORMALISATION; PostgreSQL does not normalise "
                "implicitly, so the source profile must prove the data is already in the target form"
            )

        return resolution

    def collate_clause(self, type_sql: str) -> str:
        """Binary collation is applied by policy, not inherited from the source."""
        if self.collation == "binary" and str(type_sql).startswith(("character", "text")):
            return ' COLLATE "C"'
        return ""


# ---------------------------------------------------------------------------
# Expression helpers
# ---------------------------------------------------------------------------

class Ref:
    """A reference to a column of the current source, qualified or not.

    Once a job has joins, an unqualified reference is ambiguous, so every read
    goes through here and the compiler decides the qualification from context
    rather than from call sites.
    """

    def __init__(self, alias: str, qualified: bool) -> None:
        self.alias = alias
        self.qualified = qualified

    def __call__(self, name: str) -> str:
        if self.qualified:
            return f"{qident(self.alias)}.{qident(name)}"
        return qident(name)

    def with_qualification(self, qualified: bool) -> "Ref":
        return Ref(self.alias, qualified)


def wrap_parens(text: str) -> str:
    return f"({text})"


def cast_to(expression: str, type_sql: Optional[str]) -> str:
    if not type_sql:
        return expression
    return f"CAST({expression} AS {type_sql})"


def coalesce_nullif_empty(expression: str, policy: str) -> str:
    """Apply `defaults.emptyStringIsNull` to a character expression.

    Oracle treats the empty string as NULL and PostgreSQL does not, so a plain
    copy changes what a NOT NULL constraint and a COUNT(*) mean on the target.
    """
    if policy != "null":
        return expression
    return f"NULLIF({expression}, '')"


def strip_blank_padding(expression: str) -> str:
    """Oracle CHAR is blank-padded to its declared length; PG CHARACTER is not."""
    return f"TRIM({expression})"


def extract_offset_minutes(expression: str) -> str:
    """Offset in minutes, as a plain arithmetic expression.

    This is the `timestampWithTimeZone: split-offset` companion column. It is
    plain arithmetic rather than a formatting call because the value has to be
    numeric and directly comparable in validation.
    """
    return (
        f"(NVL(EXTRACT(TIMEZONE_HOUR FROM {expression}), 0) * 60"
        f" + NVL(EXTRACT(TIMEZONE_MINUTE FROM {expression}), 0))"
    )


def escape_body_to_expression(body: str, ref: Ref, rule_id: Optional[str], diags: Diagnostics) -> str:
    """Accept an `escape/sql-scalar` body, validating it is a scalar expression.

    The body is author-supplied SQL, so it is parsed before use and must not
    contain a second statement. It is used verbatim because rewriting it would
    defeat the point of an escape.
    """
    text = str(body).strip()
    if not SQLGLOT_AVAILABLE:
        return text

    try:
        parsed = parse_one(text, read="ctunnel")
    except Exception as exc:  # noqa: BLE001
        diags.add(
            "ESCAPE_BODY_UNPARSEABLE",
            "BLOCK",
            f"escape/sql-scalar body does not parse as Oracle SQL: {exc}",
            rule_id=rule_id,
            body=text,
        )
        return text

    if parsed is None:
        diags.add("ESCAPE_BODY_EMPTY", "BLOCK", "escape/sql-scalar body parsed to nothing", rule_id=rule_id)
        return text
    if isinstance(parsed, (exp.Select, exp.Union, exp.Subquery, exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create)):
        diags.add(
            "ESCAPE_BODY_NOT_SCALAR",
            "BLOCK",
            "escape/sql-scalar must be a single scalar expression, not a statement",
            rule_id=rule_id,
            body=text,
        )
        return text

    rendered = parsed.sql(dialect="ctunnel")
    return rendered


# ---------------------------------------------------------------------------
# Value-operation compilers
# ---------------------------------------------------------------------------
#
# Every function here takes the current expression for the column and returns
# the new one. Working-expression threading is what lets two recipes on the same
# column compose instead of competing.

def compile_value_op(
    expression: str,
    step: Dict[str, Any],
    rule_id: Optional[str],
    diags: Diagnostics,
) -> Tuple[str, List[str]]:
    """Compile one `category: value` step. Returns (expression, assumptions)."""
    operation = str(step.get("operation"))
    parameters = dict(step.get("parameters") or {})
    if step.get("targetType"):
        parameters.setdefault("targetType", step["targetType"])

    assumptions: List[str] = []

    if operation == "cast":
        target_type = parameters.get("targetType")
        if not target_type:
            diags.add("VALUE_CAST_NO_TYPE", "BLOCK", "value/cast has no targetType", rule_id=rule_id)
            return expression, assumptions
        overflow = str(step.get("onOverflow") or parameters.get("onOverflow") or "")
        if overflow == "quarantine":
            assumptions.append(
                f"cast to {target_type} with onOverflow=quarantine is enforced by the validation "
                "contract; the transform itself cannot route rows"
            )
        return cast_to(expression, str(target_type)), assumptions

    if operation == "trim":
        side = str(parameters.get("side") or "both")
        function = {"left": "LTRIM", "right": "RTRIM", "both": "TRIM"}.get(side)
        if function is None:
            diags.add(
                "VALUE_TRIM_SIDE_UNKNOWN",
                "BLOCK",
                f"value/trim side `{side}` is not left, right or both",
                rule_id=rule_id,
            )
            return expression, assumptions
        return f"{function}({expression})", assumptions

    if operation == "substring":
        start = int(parameters.get("from", 1))
        length = parameters.get("length")
        if length is not None:
            return f"SUBSTR({expression}, {start}, {int(length)})", assumptions
        return f"SUBSTR({expression}, {start})", assumptions

    if operation == "mask":
        strategy = str(parameters.get("strategy") or "hash")
        if strategy == "hash":
            digest = str(parameters.get("digest") or "SHA-256").replace("-", "").lower()
            assumptions.append(
                f"mask strategy=hash is rendered as Oracle STANDARD_HASH(expr, '{digest.upper()}'); "
                "the digest is not reversible, which is the point of masking but is irreversible"
            )
            return f"STANDARD_HASH({cast_to(expression, 'STRING')}, '{digest.upper()}')", assumptions
        if strategy in {"partial", "keep-prefix", "keep-suffix", "redact"}:
            keep = int(parameters.get("keep") or 4)
            if strategy == "keep-prefix":
                return f"SUBSTR({expression}, 1, {keep})", assumptions
            if strategy == "keep-suffix":
                return f"SUBSTR({expression}, -{keep})", assumptions
            assumptions.append("mask strategy=redact replaces the value with a fixed marker")
            return sql_literal(str(parameters.get("replacement") or "***REDACTED***")), assumptions
        diags.add(
            "VALUE_MASK_STRATEGY_UNKNOWN",
            "BLOCK",
            f"value/mask strategy `{strategy}` is not implemented",
            rule_id=rule_id,
        )
        return expression, assumptions

    if operation == "default-when-null":
        default = parameters.get("value")
        if default is None:
            diags.add("VALUE_DEFAULT_NONE", "BLOCK", "value/default-when-null has no value", rule_id=rule_id)
            return expression, assumptions
        return f"COALESCE({expression}, {sql_literal(default)})", assumptions

    if operation == "date-add":
        days = int(parameters.get("days", 0))
        unit = str(parameters.get("unit") or "DAY").upper()
        if unit != "DAY":
            diags.add(
                "VALUE_DATE_ADD_UNIT_UNKNOWN",
                "BLOCK",
                f"value/date-add unit `{unit}` is not implemented; only DAY is",
                rule_id=rule_id,
            )
            return expression, assumptions
        return f"({expression} + INTERVAL '{days}' DAY)", assumptions

    if operation == "regexp-extract":
        pattern = parameters.get("pattern")
        if not pattern:
            diags.add("VALUE_REGEXP_NO_PATTERN", "BLOCK", "value/regexp-extract has no pattern", rule_id=rule_id)
            return expression, assumptions
        group = int(parameters.get("group") or 0)
        # Oracle's 4-argument REGEXP_SUBSTR is (source, pattern, position, group).
        # The 3-argument REGEXP_EXTRACT spelling that some dialects emit does not
        # exist in Oracle, so the Oracle form is emitted directly.
        assumptions.append(
            "regexp-extract is emitted as Oracle REGEXP_SUBSTR(source, pattern, 1, group); "
            "group 0 returns the whole match"
        )
        return f"REGEXP_SUBSTR({expression}, {sql_literal(pattern)}, 1, {group})", assumptions

    if operation == "uppercase":
        return f"UPPER({expression})", assumptions
    if operation == "lowercase":
        return f"LOWER({expression})", assumptions

    if operation == "replace":
        old = parameters.get("old")
        new = parameters.get("new")
        if old is None:
            diags.add("VALUE_REPLACE_NO_OLD", "BLOCK", "value/replace has no `old`", rule_id=rule_id)
            return expression, assumptions
        if new is None:
            new = ""
        if isinstance(old, (int, float)) and isinstance(new, (int, float)):
            assumptions.append(
                "value/replace with numeric operands compares by string, not by number"
            )
        return f"REPLACE({expression}, {sql_literal(old)}, {sql_literal(new)})", assumptions

    if operation == "round":
        scale = parameters.get("scale")
        if scale is None:
            return f"ROUND({expression})", assumptions
        return f"ROUND({expression}, {int(scale)})", assumptions

    if operation == "truncate":
        length = int(parameters.get("length") or 0)
        return f"TRUNC({expression}, {length})", assumptions

    if operation == "date-trunc":
        unit = str(parameters.get("unit") or "DAY").upper()
        supported = {"DAY", "MONTH", "YEAR", "QUARTER", "WEEK", "HOUR", "MINUTE", "SECOND"}
        if unit not in supported:
            diags.add(
                "VALUE_DATE_TRUNC_UNIT_UNKNOWN",
                "BLOCK",
                f"value/date-trunc unit `{unit}` is not supported; choose from {sorted(supported)}",
                rule_id=rule_id,
            )
            return expression, assumptions
        return f"TRUNC({expression}, '{unit[0]}')", assumptions

    if operation == "null-to-empty":
        return f"NVL({expression}, '')", assumptions
    if operation == "empty-to-null":
        return f"NULLIF({expression}, '')", assumptions
    if operation == "coalesce-blank":
        # An empty string is not blank, and Oracle cannot see the difference
        # either, so this collapses whitespace-only values to the fallback.
        fallback = sql_literal(parameters.get("value", ""))
        return f"COALESCE(NULLIF(TRIM({expression}), ''), {fallback})", assumptions

    if operation == "normalize-unicode":
        form = str(parameters.get("form") or "NFKC").upper()
        assumptions.append(
            f"normalize-unicode form {form} is not applied by the transform; Oracle and PostgreSQL "
            "have no common normalisation function, so this is enforced by the validation contract"
        )
        return expression, assumptions

    if operation == "pad":
        width = int(parameters.get("width") or 0)
        fill = str(parameters.get("fill") or " ")
        side = str(parameters.get("side") or "right")
        function = "RPAD" if side == "right" else "LPAD"
        return f"{function}({expression}, {width}, {sql_literal(fill)})", assumptions

    if operation in {"left", "right"}:
        length = int(parameters.get("length") or 0)
        function = "SUBSTR" if operation == "left" else "SUBSTR"
        start = "1" if operation == "left" else f"-{length}"
        return f"{function}({expression}, {start}, {length})", assumptions

    if operation == "split-part":
        delimiter = str(parameters.get("delimiter") or ",")
        index = int(parameters.get("index") or 1)
        return (
            f"REGEXP_SUBSTR({expression}, "
            f"'{_split_pattern(delimiter, index)}', 1, 1)"
        ), assumptions

    if operation == "drop":
        return expression, assumptions

    diags.add(
        "VALUE_OPERATION_UNIMPLEMENTED",
        "BLOCK",
        f"value/{operation} is not implemented by this compiler",
        rule_id=rule_id,
    )
    return expression, assumptions


def _split_pattern(delimiter: str, index: int) -> str:
    """Regex that captures the `index`-th field of a delimited string.

    Lazy matching keeps the capture bounded by the next delimiter or the end of
    the string, which is the only form that behaves the same for a one-character
    and a multi-character delimiter.
    """
    escaped = regex_escape(delimiter)
    prefix = f"(?:{escaped}){{{index - 1}}}" if index > 1 else ""
    return f"{prefix}(.*?)(?:{escaped}|$)"


# ---------------------------------------------------------------------------
# Derived-operation compilers
# ---------------------------------------------------------------------------

def compile_derived_op(
    step: Dict[str, Any],
    inputs: List[str],
    rule_id: Optional[str],
    diags: Diagnostics,
) -> Tuple[Optional[str], List[str]]:
    """Compile one `category: derived` step. `inputs` are rendered expressions."""
    operation = str(step.get("operation"))
    parameters = dict(step.get("parameters") or {})
    assumptions: List[str] = []

    if operation == "concat":
        separator = sql_literal(parameters.get("separator", ""))
        parts = [inp for inp in inputs if inp]
        if not parts:
            diags.add("DERIVED_CONCAT_NO_INPUTS", "BLOCK", "derived/concat has no inputs", rule_id=rule_id)
            return None, assumptions
        assumptions.append(
            "concat is rendered as Oracle `||`, which treats a NULL operand as an empty string "
            "rather than propagating NULL"
        )
        joined = parts[0]
        for part in parts[1:]:
            joined = f"{joined} || {separator} || {part}"
        return wrap_parens(joined), assumptions

    if operation == "coalesce":
        if not inputs:
            diags.add("DERIVED_COALESCE_NO_INPUTS", "BLOCK", "derived/coalesce has no inputs", rule_id=rule_id)
            return None, assumptions
        return f"COALESCE({', '.join(inputs)})", assumptions

    if operation == "arithmetic":
        operator = str(parameters.get("operator") or "")
        symbol = ARITHMETIC_OPERATORS.get(operator)
        if symbol is None:
            diags.add(
                "DERIVED_ARITHMETIC_UNKNOWN",
                "BLOCK",
                f"derived/arithmetic operator `{operator}` is not supported",
                rule_id=rule_id,
            )
            return None, assumptions
        if len(inputs) < 2:
            diags.add(
                "DERIVED_ARITHMETIC_TOO_FEW_INPUTS",
                "BLOCK",
                "derived/arithmetic needs at least two inputs",
                rule_id=rule_id,
            )
            return None, assumptions
        reduce_symbol = symbol
        if operator == "divide":
            reduce_symbol = "/"
            assumptions.append(
                "derived/arithmetic divide is left-associative; use derived/expression for "
                "an explicit parenthesised grouping"
            )
        expression = f"({f' {reduce_symbol} '.join(inputs)})"
        return expression, assumptions

    if operation == "cast-as":
        target_type = parameters.get("targetType")
        if not target_type:
            diags.add("DERIVED_CAST_NO_TYPE", "BLOCK", "derived/cast-as has no targetType", rule_id=rule_id)
            return None, assumptions
        return cast_to(f"COALESCE({', '.join(inputs)})", str(target_type)), assumptions

    if operation == "md5":
        assumptions.append("derived/md5 renders as Oracle STANDARD_HASH(expr, 'MD5')")
        return f"STANDARD_HASH({cast_to(inputs[0], 'STRING')}, 'MD5')", assumptions

    if operation == "case-when":
        cases = parameters.get("cases") or []
        if not cases:
            diags.add("DERIVED_CASE_NO_CASES", "BLOCK", "derived/case-when has no cases", rule_id=rule_id)
            return None, assumptions
        source = inputs[0]
        fragments: List[str] = []
        for case in cases:
            when = case.get("when")
            if isinstance(when, dict):
                condition = build_predicate(when, ref=None)
            else:
                condition = f"{source} = {sql_literal(when)}"
            fragments.append(f"WHEN {condition} THEN {sql_literal(case.get('then'))}")
        expression = "CASE " + " ".join(fragments)
        if "else" in parameters:
            expression += f" ELSE {sql_literal(parameters.get('else'))}"
        return f"{expression} END", assumptions

    diags.add(
        "DERIVED_OPERATION_UNIMPLEMENTED",
        "BLOCK",
        f"derived/{operation} is not implemented by this compiler",
        rule_id=rule_id,
    )
    return None, assumptions


# ---------------------------------------------------------------------------
# Predicate compiler
# ---------------------------------------------------------------------------

def build_predicate(node: Dict[str, Any], ref: Optional[Ref]) -> str:
    """Render one row-selection predicate.

    `allOf` becomes AND and `anyOf` becomes OR, and the result is parenthesised
    at every node so that mixing the two cannot change the meaning.
    """
    if "allOf" in node:
        children = [build_predicate(child, ref) for child in node.get("allOf") or []]
        return "(" + " AND ".join(children) + ")"
    if "anyOf" in node:
        children = [build_predicate(child, ref) for child in node.get("anyOf") or []]
        return "(" + " OR ".join(children) + ")"

    name = str(node["column"])
    column_sql = ref(name) if ref is not None else qident(name)
    comparison = node.get("comparison")

    if comparison == "is-null":
        return f"{column_sql} IS NULL"
    if comparison == "is-not-null":
        return f"{column_sql} IS NOT NULL"

    operator = PREDICATE_OPERATORS.get(str(comparison))
    if operator is None:
        raise CompileError(f"unsupported predicate comparison: {comparison}")

    if node.get("comparisonColumn"):
        other = str(node["comparisonColumn"])
        other_sql = ref(other) if ref is not None else qident(other)
        return f"{column_sql} {operator} {other_sql}"

    return f"{column_sql} {operator} {sql_literal(node.get('literal'))}"


def combine_predicates(predicates: List[str]) -> Optional[str]:
    """AND every row-selection predicate on a table.

    A table may carry more than one row-selection rule. They intersect, so
    taking only the first -- as basic mode did -- would silently widen the result.
    """
    active = [p for p in predicates if p]
    if not active:
        return None
    if len(active) == 1:
        return active[0]
    return "(" + " AND ".join(active) + ")"


# ---------------------------------------------------------------------------
# Advanced operation compilers
# ---------------------------------------------------------------------------

def flashback_clause() -> str:
    """The Oracle snapshot clause every source read carries."""
    return f"AS OF SCN {SCN_BIND}"


def parse_ctunnel(sql: str) -> Tuple[Optional[Any], Optional[str]]:
    """Parse in the cTunnel dialect. Returns (expression, error message)."""
    if not SQLGLOT_AVAILABLE:
        return None, None
    try:
        return parse_one(sql, read="ctunnel"), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def render_ctunnel(raw_sql: str) -> Tuple[Optional[str], Dict[str, str]]:
    """Parse, generate canonically, then round-trip to prove the output is valid.

    The guarantee this gives is byte-identity across parse -> generate -> parse,
    so a query can never ship in a form that fails to re-parse or that reflows on
    the next pass.
    """
    if not SQLGLOT_AVAILABLE:
        body = raw_sql.strip()
        if not body.endswith(";"):
            body += ";"
        return body, {
            "dialect": "ctunnel",
            "parse": "SKIPPED_SQLGLOT_NOT_INSTALLED",
            "roundtrip": "SKIPPED_SQLGLOT_NOT_INSTALLED",
            "stable": "SKIPPED_SQLGLOT_NOT_INSTALLED",
        }

    parsed, error = parse_ctunnel(raw_sql)
    if parsed is None:
        return None, {
            "dialect": "ctunnel",
            "parse": "FAIL",
            "roundtrip": "NOT_ATTEMPTED",
            "stable": "NOT_ATTEMPTED",
            "error": error or "unknown parse failure",
        }

    normalize_ctunnel_ast(parsed)
    rendered = parsed.sql(dialect="ctunnel", pretty=True)

    reparsed, reparse_error = parse_ctunnel(rendered)
    if reparsed is None:
        return None, {
            "dialect": "ctunnel",
            "parse": "PASS",
            "roundtrip": "FAIL",
            "stable": "NOT_ATTEMPTED",
            "error": reparse_error or "emitted query did not re-parse",
        }

    normalize_ctunnel_ast(reparsed)
    stable = reparsed.sql(dialect="ctunnel", pretty=True) == rendered

    return rendered, {
        "dialect": "ctunnel",
        "parse": "PASS",
        "roundtrip": "PASS",
        "stable": "PASS" if stable else "DRIFT",
    }


def render_pg(raw_sql: str) -> Tuple[Optional[str], Dict[str, str]]:
    """Same round-trip guarantee for target PostgreSQL DDL."""
    if not SQLGLOT_AVAILABLE:
        return raw_sql.strip() + ";", {
            "dialect": "pgcontract",
            "parse": "SKIPPED_SQLGLOT_NOT_INSTALLED",
            "stable": "SKIPPED_SQLGLOT_NOT_INSTALLED",
        }
    try:
        parsed = parse_one(raw_sql, read="pgcontract")
    except Exception as exc:  # noqa: BLE001
        return None, {
            "dialect": "pgcontract",
            "parse": "FAIL",
            "stable": "NOT_ATTEMPTED",
            "error": str(exc),
        }
    rendered = parsed.sql(dialect="pgcontract", pretty=True)
    try:
        reparsed = parse_one(rendered, read="pgcontract")
    except Exception as exc:  # noqa: BLE001
        return None, {
            "dialect": "pgcontract",
            "parse": "PASS",
            "stable": "FAIL",
            "error": f"emitted DDL did not re-parse: {exc}",
        }
    stable = reparsed.sql(dialect="pgcontract", pretty=True) == rendered
    return rendered, {
        "dialect": "pgcontract",
        "parse": "PASS",
        "stable": "PASS" if stable else "DRIFT",
    }


def normalize_ctunnel_ast(parsed: Any) -> Any:
    """Rewrite the vocabulary into nodes cTunnel emits natively.

    This runs on the AST rather than inside a generator transform. A transform can
    only return text, so hand-written parentheses in that text come back as real
    `exp.Paren` nodes on the next parse and the pretty-printer reflows the query.
    Rewriting the tree keeps parse -> generate -> parse byte-identical.
    """
    if not SQLGLOT_AVAILABLE:
        return parsed

    # CONCAT_WS(sep, a, b) -> (a || sep || b). Oracle has no CONCAT_WS, and `||`
    # treats NULL operands as empty, which matches separator-join semantics.
    # CONCAT_WS stores the separator as expressions[0].
    for node in list(parsed.find_all(exp.ConcatWs)):
        parts = list(node.expressions)
        if len(parts) < 2:
            continue
        separator, values = parts[0], parts[1:]
        result = values[0]
        for value in values[1:]:
            result = exp.DPipe(this=result, expression=separator.copy())
            result = exp.DPipe(this=result, expression=value)
        node.replace(exp.Paren(this=result))

    # Oracle has no DATE_ADD function; it uses interval arithmetic.
    for node in list(list(parsed.find_all(exp.DateAdd))):
        base = node.args.get("this")
        amount = node.args.get("expression")
        unit = unit_to_str(node)
        unit_name = unit.name if isinstance(unit, exp.Literal) else unit
        amount_text = amount.name if isinstance(amount, exp.Literal) else str(amount)
        interval = exp.Interval(
            this=exp.Literal.string(amount_text),
            unit=exp.Var(this=unit_name) if unit_name else None,
        )
        node.replace(exp.Paren(this=exp.Add(this=base, expression=interval)))

    return parsed


def stamp_flashback(sql: str, diags: Diagnostics, rule_id: Optional[str], where: str) -> Optional[str]:
    """Pin author-supplied SQL to the run SCN by stamping its table nodes.

    An `escape/sql-set` body or a `set/deduplicate` `sql` block is written by a
    person, so it is parsed rather than rewritten -- but its relations still have
    to be read at the same snapshot as everything else, or the job silently mixes
    two points in time.
    """
    parsed, error = parse_ctunnel(sql)
    if parsed is None:
        diags.add(
            "AUTHORED_SQL_UNPARSEABLE",
            "BLOCK",
            f"{where} does not parse as Oracle SQL: {error}",
            rule_id=rule_id,
            sql=sql,
        )
        return None

    tables = list(parsed.find_all(exp.Table))
    if not tables:
        diags.add("AUTHORED_SQL_NO_RELATION", "BLOCK", f"{where} references no relation", rule_id=rule_id)
        return None

    if not SQLGLOT_AVAILABLE:
        return parsed.sql(dialect="ctunnel")

    for table in tables:
        if table.args.get("flashback") is None:
            table.set("flashback", CTunnelFlashback(scn=exp.Placeholder(this="run_scn")))

    rendered = parsed.sql(dialect="ctunnel")
    diags.add(
        "AUTHORED_SQL_FLASHBACK_STAMPED",
        "EDGE",
        f"{where} is author-supplied SQL; {len(tables)} relation(s) in it were pinned to {SCN_BIND} "
        "so the job reads one snapshot rather than two",
        rule_id=rule_id,
        relations=[f"{t.db}.{t.name}" for t in tables],
    )
    return rendered


# -- lookup ------------------------------------------------------------------

def compile_lookup(
    step: Dict[str, Any],
    ref: Ref,
    diags: Diagnostics,
    rule_id: Optional[str],
    index: int,
) -> Tuple[Optional[str], List[Tuple[str, str, Optional[str]]], List[str]]:
    """Compile a `lookup` step into a fan-out-free join plus taken columns.

    A plain LEFT JOIN would multiply the driving rows by the number of matching
    lookup rows, which silently corrupts the migration. The lookup relation is
    therefore pre-aggregated by its key and carries a match count, so the driving
    row count is preserved and a cardinality violation is detectable instead of
    being averaged away.
    """
    assumptions: List[str] = []
    source_schema, source_table = split_relation(str(step.get("from")))
    if not source_table:
        diags.add("LOOKUP_NO_SOURCE", "BLOCK", f"lookup {index} has no usable `from`", rule_id=rule_id)
        return None, [], assumptions

    using = [str(v) for v in step.get("using") or []]
    on_key = [str(v) for v in step.get("onKey") or []]
    take = list(step.get("take") or [])
    cardinality = str(step.get("cardinality") or "many")

    lookup_alias = f"lk{index}"
    key_alias = f"__lk{index}_k"
    count_alias = f"__lk{index}_n"

    key_projections = [f"{qident(key)} AS {qident(f'{key_alias}{i}')}" for i, key in enumerate(on_key)]
    value_projections = [
        f"MAX({qident(str(entry.get('column')))}) AS {qident(f'__lk{index}_v{i}')}"
        for i, entry in enumerate(take)
    ]
    if not value_projections:
        diags.add(
            "LOOKUP_TAKE_NONE",
            "BLOCK",
            f"lookup {index} takes no columns, so it cannot change the result",
            rule_id=rule_id,
        )
        return None, [], assumptions

    subquery = (
        f"SELECT {', '.join(key_projections + value_projections)}, "
        f"COUNT(*) AS {qident(count_alias)} "
        f"FROM {relation(source_schema, source_table)} {flashback_clause()} "
        f"GROUP BY {', '.join(qident(k) for k in on_key)}"
    )

    conditions = [
        f"{ref(name)} = {qident(lookup_alias)}.{qident(f'{key_alias}{i}')}"
        for i, name in enumerate(using)
    ]
    join_sql = f"LEFT JOIN ({subquery}) AS {qident(lookup_alias)} ON {' AND '.join(conditions)}"

    max_rows = step.get("maxLookupRows")
    if max_rows is not None:
        assumptions.append(
            f"lookup {index} declares maxLookupRows={max_rows}; the join cannot enforce a row cap, "
            "so the limit is a capacity assertion checked by the validation contract"
        )
    volatility = str(step.get("volatility") or "static")
    if volatility != "static":
        assumptions.append(
            f"lookup {index} declares volatility={volatility}; the join is evaluated per row and "
            "a changing lookup side gives no stable snapshot"
        )

    on_miss = step.get("onMiss") or {}
    behaviour = str(on_miss.get("behaviour") or "null")
    miss_expression = _on_miss_expression(behaviour, on_miss, diags, rule_id, index)

    taken: List[Tuple[str, str, Optional[str]]] = []
    sentinel: Optional[str] = None
    sentinel_expression: Optional[str] = None
    for position, entry in enumerate(take):
        column = str(entry.get("column"))
        alias = str(entry.get("as") or column)
        value_ref = f"{qident(lookup_alias)}.{qident(f'__lk{index}_v{position}')}"
        count_ref = f"{qident(lookup_alias)}.{qident(count_alias)}"

        if cardinality in {"at-most-one", "exactly-one"}:
            violation = f"{count_ref} > 1" if cardinality == "at-most-one" else f"{count_ref} <> 1"
            expression = f"CASE WHEN {violation} THEN {miss_expression} ELSE COALESCE({value_ref}, {miss_expression}) END"
        else:
            expression = f"COALESCE({value_ref}, {miss_expression})"
            assumptions.append(
                f"lookup {index} declares cardinality=many; duplicate lookup rows are collapsed "
                "with MAX() and the first value only is carried, so the validation contract "
                "reports any key with more than one match"
            )

        if entry.get("targetType"):
            expression = cast_to(expression, str(entry["targetType"]))

        taken.append((alias, expression, str(entry.get("targetType")) if entry.get("targetType") else None))

        if behaviour == "quarantine":
            # The sink has to be able to see which rows missed, so the miss is
            # projected as a real column and filtered by the quarantine sink.
            sentinel = f"{QUARANTINE_PREFIX}{rule_id or 'lookup'}_{index}"
            sentinel_expression = f"CASE WHEN {count_ref} IS NULL OR {count_ref} > 1 THEN 1 ELSE NULL END"

    return join_sql, taken, assumptions, sentinel, sentinel_expression


def _on_miss_expression(
    behaviour: str,
    on_miss: Dict[str, Any],
    diags: Diagnostics,
    rule_id: Optional[str],
    index: int,
) -> str:
    if behaviour == "default":
        if "value" not in on_miss:
            diags.add(
                "LOOKUP_ON_MISS_DEFAULT_NO_VALUE",
                "BLOCK",
                f"lookup {index} says onMiss.behaviour=default but gives no value",
                rule_id=rule_id,
            )
            return "NULL"
        return sql_literal(on_miss["value"])
    if behaviour in {"null", "keep"}:
        return "NULL"
    if behaviour in {"fail-run", "error", "reject"}:
        diags.add(
            "LOOKUP_ON_MISS_FAIL_RUN",
            "ASSUMPTION",
            f"lookup {index} says onMiss.behaviour={behaviour}; a SQL projection cannot raise a "
            "runtime error, so the value is NULL and a blocking validation check asserts that no "
            "lookup misses occurred. A miss therefore fails the run at validation, not at extract",
            rule_id=rule_id,
        )
        return "NULL"
    if behaviour == "quarantine":
        diags.add(
            "LOOKUP_ON_MISS_QUARANTINE",
            "EDGE",
            f"lookup {index} quarantines misses; a __q_ sentinel column is added so the quarantine "
            "sink can filter on it",
            rule_id=rule_id,
        )
        return "NULL"

    diags.add(
        "LOOKUP_ON_MISS_UNKNOWN",
        "BLOCK",
        f"lookup {index} uses onMiss.behaviour `{behaviour}`",
        rule_id=rule_id,
    )
    return "NULL"


# -- deduplicate -------------------------------------------------------------

def compile_dedupe(
    step: Dict[str, Any],
    diags: Diagnostics,
    rule_id: Optional[str],
    table_name: str,
    declared_key: List[str],
) -> Tuple[Optional[str], List[str]]:
    """Compile a `set/deduplicate` step into a source subquery.

    Either the spec supplies the SQL, or it is generated from `orderSensitive`.
    In both cases the result is a relation rather than a projection, because
    choosing one row per key changes the row set before anything else runs.
    """
    assumptions: List[str] = []
    authored = step.get("sql")

    if authored:
        stamped = stamp_flashback(str(authored), diags, rule_id, f"set/deduplicate sql for {table_name}")
        if stamped is None:
            return None, assumptions
        return stamped, assumptions

    order = step.get("orderSensitive") or {}
    order_by = [str(v) for v in order.get("orderBy") or []]
    tie_break = [str(v) for v in order.get("tieBreak") or []]
    partition_by = [str(v) for v in (step.get("partitionBy") or step.get("key") or [])]

    if not partition_by:
        partition_by = list(declared_key)
    if not partition_by:
        diags.add(
            "DEDUPE_NO_PARTITION_KEY",
            "BLOCK",
            f"set/deduplicate for {table_name} has neither `sql`, `partitionBy` nor a declared key, "
            "so there is nothing to deduplicate on",
            rule_id=rule_id,
        )
        return None, assumptions
    if not order_by:
        diags.add(
            "DEDUPE_NO_ORDER",
            "BLOCK",
            f"set/deduplicate for {table_name} has no orderSensitive.orderBy, so which row survives "
            "would be arbitrary",
            rule_id=rule_id,
        )
        return None, assumptions

    def direction(value: Any) -> str:
        return "DESC" if str(value).upper() in {"DESC", "DESCENDING", "LAST", "LATEST", "MAX"} else "ASC"

    window_parts = [
        f"{qident(column)} {direction(order.get(f'{column}Direction') or order.get('direction') or 'DESC')}"
        for column in order_by
    ]
    window_parts += [
        f"{qident(column)} {direction(order.get(f'{column}Direction') or 'ASC')}" for column in tie_break
    ]
    window = ", ".join(window_parts)

    sql = (
        f"SELECT * FROM (SELECT t0.*, ROW_NUMBER() OVER ("
        f"PARTITION BY {', '.join(qident(c) for c in partition_by)} "
        f"ORDER BY {window}) AS {qident('__dedupe_rn')} "
        f"FROM {relation(None, table_name)}) "
        f"WHERE {qident('__dedupe_rn')} = 1"
    )
    assumptions.append(
        "deduplicate keeps one row per key using ROW_NUMBER(); rows beyond the tie-break are dropped, "
        "which is a lossy answer to a set question and is covered by a duplicate-count check"
    )
    return sql, assumptions


# -- merge -------------------------------------------------------------------

def compile_merge(
    step: Dict[str, Any],
    sources: List[Dict[str, Any]],
    column_order: List[str],
    diags: Diagnostics,
    rule_id: Optional[str],
) -> Tuple[Optional[str], List[str]]:
    """Compile `cardinality/merge` into a UNION ALL of one branch per source."""
    assumptions: List[str] = []
    correspondence = step.get("correspondence") or {}
    discriminator = correspondence.get("discriminator")
    generated = str(correspondence.get("generated") or "none")

    if len(sources) < 2:
        diags.add(
            "MERGE_SINGLE_SOURCE",
            "BLOCK",
            f"cardinality/merge needs at least two sources, found {len(sources)}",
            rule_id=rule_id,
        )
        return None, assumptions

    branches: List[str] = []
    for position, entry in enumerate(sources):
        schema, table = split_relation(str(entry.get("relation") or entry.get("from")))
        if not table:
            diags.add(
                "MERGE_SOURCE_UNPARSEABLE",
                "BLOCK",
                f"merge source {position} is `{entry.get('relation')}`",
                rule_id=rule_id,
            )
            return None, assumptions

        projections: List[str] = [qident(name) for name in column_order]

        if discriminator and str(discriminator).upper() not in {c.upper() for c in column_order}:
            if generated == "none":
                # The discriminator already exists on every source; projecting it
                # is the whole job, but its absence on any branch would break the
                # union, so it is checked here.
                projections.append(qident(str(discriminator)))
            elif generated == "literal":
                value = entry.get("discriminatorValue")
                if value is None:
                    diags.add(
                        "MERGE_DISCRIMINATOR_NO_VALUE",
                        "BLOCK",
                        f"merge source {schema}.{table} generates a literal discriminator but gives no value",
                        rule_id=rule_id,
                    )
                    return None, assumptions
                projections.append(f"{sql_literal(value)} AS {qident(str(discriminator))}")
            elif generated == "sequence":
                sequence = entry.get("sequence") or step.get("sequence")
                if not sequence:
                    diags.add(
                        "MERGE_SEQUENCE_MISSING",
                        "BLOCK",
                        "merge generates a sequence discriminator but names no sequence",
                        rule_id=rule_id,
                    )
                    return None, assumptions
                sequence_schema, sequence_name = split_relation(str(sequence))
                projections.append(
                    f"{relation(sequence_schema, sequence_name)}.NEXTVAL AS {qident(str(discriminator))}"
                )
            else:
                diags.add(
                    "MERGE_GENERATED_UNKNOWN",
                    "BLOCK",
                    f"merge correspondence.generated `{generated}` is not one of none, literal, sequence",
                    rule_id=rule_id,
                )
                return None, assumptions

        branches.append(
            f"SELECT {', '.join(projections)} FROM {relation(schema, table)} {flashback_clause()}"
        )

    assumptions.append(
        "merge is UNION ALL of one branch per source, so duplicate keys across years are kept; "
        "the correspondence key is validated rather than enforced"
    )
    return " UNION ALL ".join(branches), assumptions


# -- split -------------------------------------------------------------------

def split_targets(step: Dict[str, Any], base_table: str, diags: Diagnostics, rule_id: Optional[str]) -> List[Tuple[str, str, str]]:
    """Return (target name, target table, discriminator value) for each branch."""
    correspondence = step.get("correspondence") or {}
    discriminator = correspondence.get("discriminator")
    explicit = step.get("discriminatorValues") or {}

    result: List[Tuple[str, str, str]] = []
    for target in step.get("targets") or []:
        name = str(target)
        value = explicit.get(name, str(name).upper())
        table = f"{base_table}_{sanitize_alias(name)}"
        result.append((name, table, str(value)))

    if not result:
        diags.add("SPLIT_NO_TARGETS", "BLOCK", "cardinality/split names no targets", rule_id=rule_id)

    if discriminator and all(value.upper() == name.upper() for name, _, value in result):
        diags.add(
            "SPLIT_DISCRIMINATOR_DERIVED",
            "ASSUMPTION",
            f"cardinality/split has no discriminatorValues, so each branch value is the upper-cased "
            f"target name ({[v for _, _, v in result]}). If the source stores different literals the "
            "split silently loads nothing; a validation check asserts each branch is non-empty",
            rule_id=rule_id,
        )

    return result


# -- denormalise -------------------------------------------------------------

def compile_denormalise(
    step: Dict[str, Any],
    diags: Diagnostics,
    rule_id: Optional[str],
) -> Tuple[List[str], Dict[str, str], List[Tuple[str, str, Optional[str]]], List[str]]:
    """Compile `relational/denormalise` into joins plus an explicit column list."""
    assumptions: List[str] = []
    shape = step.get("from") or {}
    driving = str(shape.get("driving") or "")
    driving_schema, driving_table = split_relation(driving)

    aliases: Dict[str, str] = {}
    taken: List[str] = []
    alias = sanitize_alias(driving_table or "drv")
    aliases[driving.upper()] = alias
    joins: List[str] = []

    for join in shape.get("joins") or []:
        relation_text = str(join.get("relation") or "")
        join_schema, join_table = split_relation(relation_text)
        join_alias = unique_alias(sanitize_alias(join_table or "j"), taken + [alias])
        aliases[relation_text.upper()] = join_alias
        taken.append(join_alias)

        join_type = JOIN_TYPES.get(str(join.get("type") or "inner"))
        if join_type is None:
            diags.add(
                "DENORMALISE_JOIN_TYPE_UNKNOWN",
                "BLOCK",
                f"join type `{join.get('type')}` is not supported",
                rule_id=rule_id,
            )
            return [], {}, [], assumptions

        conditions: List[str] = []
        for condition in join.get("joinOn") or []:
            conditions.append(
                f"{qident(alias)}.{qident(str(condition.get('drivingColumn')))} = "
                f"{qident(join_alias)}.{qident(str(condition.get('relationColumn')))}"
            )

        if join_type != "CROSS JOIN" and not conditions:
            diags.add(
                "DENORMALISE_JOIN_NO_CONDITION",
                "BLOCK",
                f"join to {relation_text} has no joinOn, which would be a cartesian product",
                rule_id=rule_id,
            )
            return [], {}, [], assumptions

        join_sql = f"{join_type} {relation(join_schema, join_table)} {flashback_clause()} AS {qident(join_alias)}"
        if conditions:
            join_sql += " ON " + " AND ".join(conditions)
        joins.append(join_sql)

    columns: List[Tuple[str, str, Optional[str]]] = []
    for entry in step.get("columns") or []:
        from_relation = str(entry.get("fromRelation") or driving)
        from_column = str(entry.get("fromColumn"))
        to_column = str(entry.get("toColumn") or from_column)
        relation_alias = aliases.get(from_relation.upper())
        if relation_alias is None:
            diags.add(
                "DENORMALISE_COLUMN_UNKNOWN_RELATION",
                "BLOCK",
                f"column `{from_column}` comes from {from_relation}, which is not in the `from` shape",
                rule_id=rule_id,
            )
            return [], {}, [], assumptions
        expression = f"{qident(relation_alias)}.{qident(from_column)}"
        target_type = str(entry["targetType"]) if entry.get("targetType") else None
        if target_type:
            expression = cast_to(expression, target_type)
        columns.append((to_column, expression, target_type))

    grain = step.get("grain") or {}
    if str(grain.get("uniqueness") or "") == "asserted":
        assumptions.append(
            "grain.uniqueness=asserted is not provable without the source profile; a duplicate-grain "
            "check in the validation contract is what makes the assertion true"
        )

    dropped = [str(v) for v in step.get("droppedFromSource") or []]
    if dropped:
        assumptions.append(
            f"droppedFromSource {dropped} are omitted from the explicit column list, so the target "
            "table does not carry them even though the join still reads them"
        )

    fan_out = step.get("cdcFanOut") or {}
    if str(fan_out.get("handling") or "") == "snapshot-only":
        assumptions.append(
            "cdcFanOut.handling=snapshot-only: a change to one joined row rewrites many target rows, "
            "and there is no per-row CDC form for a join, so this job is snapshot-only"
        )

    if str(step.get("referentialIntegrity") or "") == "abandoned-by-design":
        assumptions.append(
            "referentialIntegrity=abandoned-by-design: orphan detection is expected to fail, so the "
            "orphan check is reported as informational rather than as a failure"
        )

    if not columns:
        diags.add("DENORMALISE_NO_COLUMNS", "BLOCK", "relational/denormalise maps no columns", rule_id=rule_id)

    return joins, aliases, columns, assumptions


# -- pivot -------------------------------------------------------------------

def compile_pivot(
    step: Dict[str, Any],
    ref: Ref,
    diags: Diagnostics,
    rule_id: Optional[str],
) -> Tuple[List[Tuple[str, str, Optional[str]]], List[str], Optional[str], Optional[str], List[str]]:
    """Compile `pivot` into grouped conditional aggregation.

    Returns (columns, group_by, unlisted_sentinel_name, unlisted_expression,
    assumptions). An unlisted source value would otherwise vanish silently, so it
    is counted into a sentinel column the quarantine sink can filter on.
    """
    assumptions: List[str] = []
    on_column = str(step.get("on"))
    grain = [str(v) for v in step.get("grain") or []]
    aggregate = step.get("aggregate") or {}
    aggregate_operation = str(aggregate.get("operation") or "sum")
    aggregate_column = str(aggregate.get("column") or "")

    function = PIVOT_AGGREGATES.get(aggregate_operation)
    if function is None:
        diags.add(
            "PIVOT_AGGREGATE_UNKNOWN",
            "BLOCK",
            f"pivot aggregate `{aggregate_operation}` is not supported",
            rule_id=rule_id,
        )
        return [], [], None, None, assumptions

    into_columns = list(step.get("intoColumns") or [])
    if not into_columns:
        diags.add("PIVOT_NO_TARGETS", "BLOCK", "pivot names no intoColumns", rule_id=rule_id)
        return [], [], None, None, assumptions

    source_values: List[str] = []
    columns: List[Tuple[str, str, Optional[str]]] = []
    for position, entry in enumerate(into_columns):
        source_value = entry.get("sourceValue")
        source_values.append(str(source_value))
        target_column = str(entry.get("targetColumn") or f"v{position}")
        target_type = str(entry["targetType"]) if entry.get("targetType") else None

        argument = ref(aggregate_column) if aggregate_column else "1"
        aggregate_call = function.format(expr=argument)
        if aggregate_call == "COUNT":
            aggregate_call = f"COUNT({argument})"

        condition = f"{ref(on_column)} = {sql_literal(source_value)}"
        expression = f"{aggregate_call}(CASE WHEN {condition} THEN {argument} END)"

        if target_type:
            expression = cast_to(expression, target_type)
        columns.append((target_column, expression, target_type))

    sentinel_name: Optional[str] = None
    sentinel_expression: Optional[str] = None
    on_unlisted = str(step.get("onUnlistedValue") or "")
    if on_unlisted in {"quarantine", "fail-run", "error"}:
        literal_list = ", ".join(sql_literal(v) for v in source_values)
        sentinel_name = f"{QUARANTINE_PREFIX}{rule_id or 'pivot'}_unlisted"
        sentinel_expression = (
            f"SUM(CASE WHEN {ref(on_column)} NOT IN ({literal_list}) THEN 1 ELSE 0 END)"
        )
        if on_unlisted == "quarantine":
            assumptions.append(
                "pivot onUnlistedValue=quarantine: source values outside intoColumns are counted into "
                f"a {sentinel_name} column, and the quarantine sink filters on it"
            )
        else:
            diags.add(
                "PIVOT_UNLISTED_FAIL_RUN",
                "ASSUMPTION",
                f"pivot onUnlistedValue={on_unlisted} cannot raise inside a projection, so unlisted "
                f"source values are counted into {sentinel_name} and a validation check fails the run "
                "if the count is not zero",
                rule_id=rule_id,
            )

    assumptions.append(
        "pivot groups by the declared grain and aggregates the declared column; rows whose "
        f"{on_column} is NULL aggregate as NULL into every target column rather than being dropped"
    )

    return columns, grain, sentinel_name, sentinel_expression, assumptions


# -- column split ------------------------------------------------------------

def compile_column_split(
    step: Dict[str, Any],
    ref: Ref,
    diags: Diagnostics,
    rule_id: Optional[str],
) -> Tuple[List[Tuple[str, str, Optional[str]]], Optional[str], Optional[str], List[str]]:
    """Compile `column-split` into one REGEXP_SUBSTR per target part."""
    assumptions: List[str] = []
    column = str(step.get("column"))
    parameters = step.get("parameters") or {}
    delimiter = str(parameters.get("delimiter") or ",")
    targets = list(step.get("targets") or [])

    if not targets:
        diags.add("COLUMN_SPLIT_NO_TARGETS", "BLOCK", "column-split names no targets", rule_id=rule_id)
        return [], None, None, assumptions

    source = ref(column)
    columns: List[Tuple[str, str, Optional[str]]] = []
    for position, entry in enumerate(targets):
        name = str(entry.get("name") or f"part{position + 1}")
        target_type = str(entry["targetType"]) if entry.get("targetType") else None
        # The `index`-th field, bounded by the next delimiter or the end of the
        # string, so the expression is identical for any delimiter length.
        expression = f"REGEXP_SUBSTR({source}, {sql_literal(_split_pattern(delimiter, position + 1))}, 1, 1)"
        if target_type:
            expression = cast_to(expression, target_type)
        columns.append((name, expression, target_type))

    sentinel_name: Optional[str] = None
    sentinel_expression: Optional[str] = None
    on_extra = str(parameters.get("onExtraParts") or "")
    if on_extra in {"quarantine", "fail-run", "error"}:
        sentinel_name = f"{QUARANTINE_PREFIX}{rule_id or 'split'}_extra_parts"
        part_count = (
            f"(TRUNC((LENGTH({source}) - LENGTH(REPLACE({source}, {sql_literal(delimiter)}, '')))"
            f" / {len(delimiter)}) + 1)"
        )
        sentinel_expression = f"CASE WHEN {part_count} > {len(targets)} THEN 1 ELSE NULL END"
        assumptions.append(
            f"column-split onExtraParts={on_extra}: a row with more than {len(targets)} delimited "
            f"parts raises {sentinel_name}; the extra parts have no target column so they would "
            "otherwise disappear silently"
        )

    assumptions.append(
        f"column-split on {column} is deterministic for the first {len(targets)} parts; parts beyond "
        "the declared targets have nowhere to go, which is why onExtraParts exists"
    )

    return columns, sentinel_name, sentinel_expression, assumptions


# ---------------------------------------------------------------------------
# Job planning
# ---------------------------------------------------------------------------

@dataclass
class JobPlan:
    """One compiled unit of work: N source relations -> 1 target relation."""

    job_id: str
    kind: str
    sources: List[Tuple[str, str]]
    target_schema: str
    target_table: str
    table_rules: List[Dict[str, Any]] = field(default_factory=list)
    column_rules: List[Dict[str, Any]] = field(default_factory=list)
    discriminator: Optional[Tuple[str, str]] = None
    merge_sources: List[Dict[str, Any]] = field(default_factory=list)
    merge_step: Optional[Dict[str, Any]] = None
    label: str = ""
    part: Optional[str] = None


@dataclass
class TargetColumn:
    """One column of the target relation."""

    name: str
    type_sql: str
    nullable: bool = True
    is_pk: bool = False
    expression: str = ""
    origin: str = ""
    note: str = ""
    collation: str = ""
    #: The source column this came from, when there is one. Kept separately from
    #: `origin` because origin is a rule id for a recipe and a relation for a
    #: passthrough, and the two are needed for different lookups.
    source_name: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "type": self.type_sql,
            "nullable": self.nullable,
        }
        if self.is_pk:
            out["primary_key"] = True
        if self.expression:
            out["expression"] = self.expression
        if self.origin:
            out["origin"] = self.origin
        if self.source_name:
            out["source_column"] = self.source_name
        if self.collation:
            out["collation"] = self.collation
        if self.note:
            out["note"] = self.note
        return out


class Chain:
    """A chain of nested SELECT stages.

    Stages are built from the inside out. `source` is either a base relation (with
    its snapshot clause) or the text of an inner SELECT that later stages read as
    a derived table. Wrapping is what lets a later stage -- a filter, a dedupe, a
    pivot -- reference a column an earlier stage computed, which is impossible in
    a single flat SELECT.
    """

    def __init__(self, source_text: str, alias: str, kind: str = "relation") -> None:
        self.source_text = source_text
        self.alias = alias
        self.kind = kind
        self.levels: List[str] = []

    @classmethod
    def relation(cls, schema: str, table: str, alias: str) -> "Chain":
        return cls(f"{relation(schema, table)} {flashback_clause()} AS {qident(alias)}", alias, "relation")

    @property
    def ref(self) -> Ref:
        # A base relation is the only stage that can be read unqualified, and
        # only while no join is present.
        return Ref(self.alias, self.kind == "derived")

    @property
    def is_derived(self) -> bool:
        return self.kind == "derived"

    def set_source(self, select_text: str, alias: str) -> None:
        self.source_text = select_text
        self.alias = alias
        self.kind = "derived"
        self.levels.append(f"derived:{alias}")

    def from_sql(self, joins: Sequence[str] = ()) -> str:
        if self.kind == "relation":
            base = self.source_text
        else:
            base = f"({self.source_text}) AS {qident(self.alias)}"
        return base + (" " + " ".join(joins) if joins else "")


def plan_jobs(spec: Dict[str, Any], catalog: Catalog, naming: Naming, diags: Diagnostics) -> List[JobPlan]:
    """Decide which jobs exist, before any SQL is written.

    A table can be consumed by a merge, so it must not also become a standalone
    job; a split produces several; a wildcard scope can only be enumerated with a
    catalog. All of that is settled here, where it is cheap to be wrong.
    """
    plans: List[JobPlan] = []
    consumed: Dict[Tuple[str, str], str] = {}

    # Tables a merge consumes are not migrated on their own.
    for rule in ordered_rules(spec):
        for step in rule.get("steps") or []:
            if step.get("category") == "cardinality" and step.get("operation") == "merge":
                for raw in step.get("sources") or []:
                    entry = merge_source_entry(raw)
                    schema, table = split_relation(str(entry.get("relation") or entry.get("from")))
                    consumed[(schema.upper(), table.upper())] = str(rule.get("id"))

    # Tables the scope names, plus tables the rules name exactly. Rules are the
    # authoritative source of what moves, so a named table is never dropped just
    # because the scope wildcard did not reach it.
    candidates: List[Tuple[str, str]] = []
    seen: set = set()
    #: target relation -> the source that claimed it, for collision detection.
    targets_claimed: Dict[Tuple[str, str], str] = {}
    #: candidates dropped by scope.exclude, counted for the empty-plan finding.
    excluded_keys: set = set()

    def add(schema: str, table: str) -> None:
        key = (schema.upper(), table.upper())
        if key in seen:
            return
        seen.add(key)
        candidates.append((schema, table))

    scope = spec.get("scope") or {}
    includes = list(scope.get("include") or [])
    excludes = list(scope.get("exclude") or [])

    if not catalog.empty:
        for entry in includes:
            if entry.get("objectClass") != "table":
                continue
            for table in catalog.expand(str(entry.get("schema") or "*"), str(entry.get("name") or "*")):
                add(table.schema, table.name)
    else:
        for entry in includes:
            name = str(entry.get("name") or "*")
            if "*" not in str(entry.get("schema") or "") and "*" not in name:
                add(str(entry.get("schema")), name)

    for rule in ordered_rules(spec):
        match = rule.get("match") or {}
        if match.get("objectClass") not in {"table", "column"}:
            continue
        schema, name = match.get("schema"), match.get("name")
        if not schema or not name or "*" in str(schema) or "*" in str(name):
            continue
        add(str(schema), str(name))

    # A rule names exactly what it applies to, so an exact match is authoritative
    # even when the catalog does not list the relation. That is what lets a
    # specification migrate a table the catalog has no metadata for -- the columns
    # become assumptions, recorded in the file, rather than the whole table
    # disappearing from the migration.
    #
    # It also means a rule naming a typo produces a job. That is the correct
    # trade: the specification is the source of truth, and the missing catalog
    # entry is reported as an assumption on that job, not silently used as a reason
    # to drop what was asked for.

    for schema, table in sorted(candidates):
        excluded = any(
            entry.get("objectClass") == "table"
            and fnmatch.fnmatch(schema, str(entry.get("schema") or "*"))
            and fnmatch.fnmatch(table, str(entry.get("name") or "*"))
            for entry in excludes
        )
        if excluded:
            excluded_keys.add((schema, table))
            diags.add(
                "TABLE_EXCLUDED_BY_SCOPE",
                "EDGE",
                f"{schema}.{table} matches a scope.exclude entry, so it is not migrated",
                table=f"{schema}.{table}",
            )
            continue

        if (schema.upper(), table.upper()) in consumed:
            diags.add(
                "TABLE_CONSUMED_BY_MERGE",
                "EDGE",
                f"{schema}.{table} is a source of merge `{consumed[(schema.upper(), table.upper())]}`, "
                "so it is loaded only through the merged target and not also on its own",
                table=f"{schema}.{table}",
                rule_id=consumed[(schema.upper(), table.upper())],
            )
            continue

        target_schema, target_table = resolve_target(spec, naming, schema, table)
        table_rules = table_rules_for(spec, schema, table)
        base = f"{schema}_{table}".replace("-", "_").replace(".", "_").lower()

        # Two sources resolving to one target is a collision, not a merge. A merge
        # is declared as a `cardinality/merge` step naming its sources; a wildcard
        # scope that maps several relations onto the same `target.table` is not one,
        # and without this check the last job planned silently overwrites the
        # earlier ones in the same .conf -- three sources, one target, one load.
        claimed = targets_claimed.get((target_schema.upper(), target_table.upper()))
        if claimed is not None:
            diags.add(
                "TARGET_COLLISION",
                "BLOCK",
                f"{schema}.{table} resolves to {target_schema}.{target_table}, which "
                f"{claimed} also resolves to. Two relations cannot become one target: one of them "
                "would overwrite the other on load. Either give each a distinct `target.table`, or "
                "declare a `cardinality/merge` step naming both sources, which is how several "
                "relations are meant to land in one target",
                table=f"{schema}.{table}",
            )
        else:
            targets_claimed[(target_schema.upper(), target_table.upper())] = f"{schema}.{table}"

        split_step = _find_step(table_rules, "cardinality", "split")
        if split_step is not None:
            parts = split_targets(split_step, target_table, diags, _rule_id_of(table_rules, split_step))
            for name, split_table, value in parts:
                discriminator = _find_step(table_rules, "cardinality", "split")
                plans.append(
                    JobPlan(
                        job_id=f"JOB-{normalized_relation_name(schema, table)}--{sanitize_alias(name)}",
                        kind="split",
                        sources=[(schema, table)],
                        target_schema=target_schema,
                        target_table=split_table,
                        table_rules=table_rules,
                        column_rules=column_rules_for(spec, schema, table, catalog),
                        discriminator=(
                            ((discriminator or {}).get("correspondence") or {}).get("discriminator"),
                            value,
                        ),
                        part=name,
                        label=f"split part {name}",
                    )
                )
            continue

        merge_steps = [
            step
            for step in _all_steps(table_rules)
            if step.get("category") == "cardinality" and step.get("operation") == "merge"
        ]
        if merge_steps:
            merge_step = merge_steps[0]
            plans.append(
                JobPlan(
                    job_id=f"JOB-{normalized_relation_name(schema, table)}--merged",
                    kind="merge",
                    sources=[(schema, table)],
                    target_schema=target_schema,
                    target_table=target_table,
                    table_rules=table_rules,
                    column_rules=column_rules_for(spec, schema, table, catalog),
                    merge_sources=[
                        merge_source_entry(raw) for raw in (merge_step.get("sources") or [])
                    ],
                    merge_step=merge_step,
                    label="merge of several sources",
                )
            )
            continue

        plans.append(
            JobPlan(
                job_id=f"JOB-{normalized_relation_name(schema, table)}",
                kind=_classify(table_rules),
                sources=[(schema, table)],
                target_schema=target_schema,
                target_table=target_table,
                table_rules=table_rules,
                column_rules=column_rules_for(spec, schema, table, catalog),
                label="single source",
            )
        )

    # A merge has no driving table of its own, so no candidate table ever produces
    # it: the merge rule matches the sources, which are then excluded as consumed.
    # It has to be planned explicitly, or the most complex job in the set is the
    # one that silently disappears.
    for rule in ordered_rules(spec):
        match = rule.get("match") or {}
        if match.get("objectClass") != "table":
            continue
        merge_steps = [
            step
            for step in rule.get("steps") or []
            if step.get("category") == "cardinality" and step.get("operation") == "merge"
        ]
        if not merge_steps:
            continue
        merge_step = merge_steps[0]
        sources = [merge_source_entry(raw) for raw in (merge_step.get("sources") or [])]
        if not sources:
            continue

        first_schema, first_table = split_relation(str(sources[0].get("relation") or sources[0].get("from")))
        target_schema = naming.schema(first_schema or str(match.get("schema") or ""))
        rule_target = rule.get("target") or {}
        if rule_target.get("schema"):
            target_schema = naming.schema(str(rule_target["schema"]))
        target_table = naming.identifier(
            str(rule_target.get("table") or match.get("name") or first_table or ""),
            "table",
        )

        # Sources named by wildcard but absent from `sources` are a mismatch worth
        # reporting: the specification matched them and then did not list them.
        declared_pattern = str(match.get("name") or "*")
        if not catalog.empty:
            matched = catalog.expand(str(match.get("schema") or "*"), declared_pattern)
            listed = {
                (split_relation(str(s.get("relation") or s.get("from")))[0] or "").upper() + "."
                + (split_relation(str(s.get("relation") or s.get("from")))[1] or "").upper()
                for s in sources
            }
            for extra in matched:
                key = f"{extra.schema.upper()}.{extra.name.upper()}"
                if key not in listed:
                    diags.add(
                        "MERGE_SOURCE_NOT_LISTED",
                        "BLOCK",
                        f"rule `{rule.get('id')}` matches {key} but does not list it in `sources`, so "
                        f"its rows would be dropped by the merge into {target_schema}.{target_table}",
                        rule_id=str(rule.get("id")),
                        table=key,
                    )

        plans.append(
            JobPlan(
                job_id=f"JOB-{normalized_relation_name(first_schema or '', first_table or '')}--merged",
                kind="merge",
                sources=[(first_schema or "", first_table or "")],
                target_schema=target_schema,
                target_table=target_table,
                table_rules=[rule],
                column_rules=column_rules_for(spec, first_schema or "", first_table or "", catalog),
                merge_sources=sources,
                merge_step=merge_step,
                label="merge of several sources",
            )
        )

    # ------------------------------------------------------------------------
    # An empty plan is either correct or a bug, and the two must be told apart.
    #
    # `jobs: 0` is the worst output this compiler can produce: it looks like a
    # migration that has nothing to do, rather than a migration that could not be
    # resolved. So when the scope and the rules *did* name objects and no job
    # appeared, that is a defect in the planner and is reported as one.
    #
    # The test is deliberately narrow: something the specification named, or the
    # catalog resolved, produced no job. A specification that genuinely migrates
    # nothing is not an error, and must stay silent.
    # ------------------------------------------------------------------------

    if not plans and candidates:
        # Candidates exist and every one was dropped. Each drop already reported
        # itself -- TABLE_EXCLUDED_BY_SCOPE or TABLE_CONSUMED_BY_MERGE -- so this is
        # not an unexplained loss, and `jobs: 0` is the truthful answer. What is
        # still missing is the *net* statement: a reader of two empty files cannot
        # tell "nothing matched" from "everything matched and was then discarded",
        # and those need different responses.
        excluded_n = len(excluded_keys)
        consumed_n = len(consumed)
        lost = ", ".join(f"{schema}.{table}" for schema, table in sorted(candidates)[:10])
        more = (
            f" (+{len(candidates) - 10} more)" if len(candidates) > 10 else ""
        )
        diags.add(
            "PLANNER_EMPTY_RESULT",
            "BLOCK",
            f"scope and rules resolved {len(candidates)} relation(s) -- {lost}{more} -- but "
            f"plan_jobs produced no job for any of them"
            + (f": {excluded_n} excluded by scope" if excluded_n else "")
            + (f", {consumed_n} consumed as merge sources" if consumed_n else "")
            + (". Nothing was migrated, and the two empty files on their own say nothing "
               "about why. Each relation is named in the finding above it"
               if (excluded_n or consumed_n) else
               ". No relation was excluded or consumed, so they were dropped for a reason "
               "this planner does not record. That is a defect in the planner, not a "
               "specification with nothing to do"),
        )

    return plans


def _all_steps(table_rules: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    steps: List[Dict[str, Any]] = []
    for rule in table_rules:
        steps.extend(rule.get("steps") or [])
    return steps


def _find_step(
    table_rules: Sequence[Dict[str, Any]], category: str, operation: Optional[str]
) -> Optional[Dict[str, Any]]:
    for rule in table_rules:
        for step in rule.get("steps") or []:
            if step.get("category") != category:
                continue
            if operation is None or step.get("operation") == operation:
                return step
    return None


def _rule_id_of(table_rules: Sequence[Dict[str, Any]], step: Dict[str, Any]) -> Optional[str]:
    for rule in table_rules:
        if step in (rule.get("steps") or []):
            return str(rule.get("id"))
    return None


def _classify(table_rules: Sequence[Dict[str, Any]]) -> str:
    """Name the dominant operation so the report reads as a summary."""
    steps = _all_steps(table_rules)
    for step in steps:
        category = str(step.get("category"))
        if category in SNAPSHOT_ONLY_CATEGORIES:
            return category
    if any(step.get("category") == "escape" and step.get("language") == "sql-set" for step in steps):
        return "escape-sql-set"
    if any(step.get("category") == "relational" for step in steps):
        return "denormalise"
    return "transform"


def resolve_target(spec: Dict[str, Any], naming: Naming, schema: str, table: str) -> Tuple[str, str]:
    """Apply `naming` plus any table rule target to get the target relation."""
    target_schema = naming.schema(schema)
    target_table = naming.identifier(table, "table")

    table_rules = table_rules_for(spec, schema, table)
    for rule in table_rules:
        target = rule.get("target") or {}
        if target.get("schema"):
            target_schema = naming.schema(str(target["schema"]))
        if target.get("table"):
            target_table = naming.identifier(str(target["table"]), "table")

    return target_schema, target_table


# ---------------------------------------------------------------------------
# Job compilation
# ---------------------------------------------------------------------------

@dataclass
class CompiledJob:
    """One job, fully compiled: SQL, DDL, and the metadata both need."""

    plan: JobPlan
    job_id: str
    kind: str
    sources: List[Dict[str, Any]]
    target: Dict[str, Any]
    columns: List[TargetColumn] = field(default_factory=list)
    query: Optional[str] = None
    sqlglot: Dict[str, str] = field(default_factory=dict)
    ddl: Dict[str, Any] = field(default_factory=dict)
    status: str = "SUPPORTED"
    blocked_by: List[Dict[str, Any]] = field(default_factory=list)
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    assumptions: List[str] = field(default_factory=list)
    snapshot_only: bool = False
    upsert: bool = False
    primary_keys: List[str] = field(default_factory=list)
    quarantine_filters: List[Dict[str, str]] = field(default_factory=list)
    binds: List[Dict[str, Any]] = field(default_factory=list)
    sources_named: List[str] = field(default_factory=list)
    operations: List[Dict[str, Any]] = field(default_factory=list)
    validation_strategies: List[Dict[str, Any]] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)


def compile_job(
    spec: Dict[str, Any],
    plan: JobPlan,
    catalog: Catalog,
    naming: Naming,
    types: TypeResolver,
    base_diags: Diagnostics,
) -> CompiledJob:
    """Compile one plan into SQL, target columns, and target DDL."""
    diags = Diagnostics()
    assumptions: List[str] = []
    operations: List[Dict[str, Any]] = []

    engines = spec.get("engines") or {}
    source_engine = str((engines.get("source") or {}).get("engine") or "oracle")
    target_engine = str((engines.get("target") or {}).get("engine") or "postgresql")

    job = CompiledJob(
        plan=plan,
        job_id=plan.job_id,
        kind=plan.kind,
        sources=[],
        target={
            "engine": target_engine,
            "schema": plan.target_schema,
            "table": plan.target_table,
            "relation": relation(plan.target_schema, plan.target_table),
        },
        diagnostics=diags,
    )

    source_entries: List[Dict[str, Any]] = []
    for schema, table in plan.sources:
        source_entries.append(
            {
                "engine": source_engine,
                "schema": schema,
                "table": table,
                "relation": relation(schema, table),
                "catalog_present": catalog.get(schema, table) is not None,
            }
        )
    job.sources = source_entries

    # ------------------------------------------------------------------
    # Which stage set the job needs. This is decided before any SQL exists,
    # because the shape decides which stages are legal.
    # ------------------------------------------------------------------
    steps = _all_steps(plan.table_rules)
    all_column_steps = [
        step
        for rule in plan.column_rules
        for step in rule.get("steps") or []
    ]

    dedupe_step = _find_step(plan.table_rules, "set", "deduplicate")
    set_sql_step = next(
        (
            step
            for step in steps
            if step.get("category") == "escape" and step.get("language") == "sql-set"
        ),
        None,
    )
    denormalise_step = _find_step(plan.table_rules, "relational", "denormalise")
    pivot_step = _find_step(plan.table_rules, "pivot", None)

    for step in steps + all_column_steps:
        category = str(step.get("category"))
        if category in SNAPSHOT_ONLY_CATEGORIES:
            job.snapshot_only = True
        if category == "change-aware":
            job.upsert = True

    if dedupe_step is not None:
        job.snapshot_only = True

    # ------------------------------------------------------------------
    # Base source.
    # ------------------------------------------------------------------
    primary_schema, primary_table = plan.sources[0]

    # The table describing the source relation, whether that came from a catalog
    # file or from the rule's own `columns:` list -- `merge_inline_source_tables`
    # has already put both in the same place, so this one lookup answers either.
    # `None` is still legitimate here: a rule that names no column at all leaves
    # the projection below with nothing to work from, and it is there that the
    # difference between "the rules named the columns" and "nothing named them"
    # can actually be told apart.
    primary_table_meta: Optional[CatalogTable] = catalog.get(primary_schema, primary_table)

    if primary_table_meta is not None and primary_table_meta.from_spec:
        note = (
            f"the column list for {primary_schema}.{primary_table} comes from this specification's "
            "own `columns:`, not from Oracle. It is what the migration is written against, so a "
            "column added to or dropped from the source table after this was written will not be "
            "reflected here; extract a catalog and recompile to re-read the real shape"
        )
        diags.add(
            "SOURCE_SHAPE_FROM_SPEC",
            "ASSUMPTION",
            note,
            rule_id=_rule_id_of(plan.table_rules, None),
            table=f"{primary_schema}.{primary_table}",
        )
        assumptions.append(note)

    declared_key = _declared_key(plan, diags)

    if set_sql_step is not None:
        # An escape/sql-set body is an arbitrary query, typically an aggregate.
        # Its result is only correct over the whole relation, so it is always
        # extracted as a snapshot no matter what run.acquisition says.
        job.snapshot_only = True
        body = str(set_sql_step.get("body") or "")
        stamped = stamp_flashback(body, diags, _rule_id_of(plan.table_rules, set_sql_step), "escape/sql-set body")
        if stamped is None:
            job.status = "BLOCKED"
            return job
        chain = Chain(stamped, "s0", "derived")
        job.sources_named = _relations_in(stamped)
        operations.append({"operation": "escape/sql-set", "rule_id": _rule_id_of(plan.table_rules, set_sql_step)})
    elif dedupe_step is not None:
        dedupe_sql, dedupe_assumptions = compile_dedupe(
            dedupe_step, diags, _rule_id_of(plan.table_rules, dedupe_step), primary_table, declared_key
        )
        assumptions.extend(dedupe_assumptions)
        if dedupe_sql is None:
            job.status = "BLOCKED"
            return job
        chain = Chain(dedupe_sql, "d0", "derived")
        job.sources_named = _relations_in(dedupe_sql)
        operations.append({"operation": "set/deduplicate", "rule_id": _rule_id_of(plan.table_rules, dedupe_step)})
    elif plan.kind == "merge" and plan.merge_step is not None:
        column_order = _merge_column_order(plan, catalog, diags)
        merge_sql, merge_assumptions = compile_merge(
            plan.merge_step, plan.merge_sources, column_order, diags, _rule_id_of(plan.table_rules, plan.merge_step)
        )
        assumptions.extend(merge_assumptions)
        if merge_sql is None:
            job.status = "BLOCKED"
            return job
        chain = Chain(merge_sql, "u0", "derived")
        job.sources_named = _relations_in(merge_sql)
        operations.append({"operation": "cardinality/merge", "rule_id": _rule_id_of(plan.table_rules, plan.merge_step)})
    else:
        chain = Chain.relation(primary_schema, primary_table, "t0")

    ref = chain.ref

    # ------------------------------------------------------------------
    # From clause: denormalise joins first, then lookups.
    # ------------------------------------------------------------------
    joins: List[str] = []
    explicit_columns: Optional[List[Tuple[str, str, Optional[str]]]] = None
    join_aliases: Dict[str, str] = {}

    if denormalise_step is not None:
        dn_joins, join_aliases, dn_columns, dn_assumptions = compile_denormalise(
            denormalise_step, diags, _rule_id_of(plan.table_rules, denormalise_step)
        )
        assumptions.extend(dn_assumptions)
        if not dn_columns:
            job.status = "BLOCKED"
            return job
        joins.extend(dn_joins)
        explicit_columns = dn_columns
        job.sources_named = sorted({rel for rel in job.sources_named if rel} | set(job.sources_named))
        job.operations.append(
            {"operation": "relational/denormalise", "rule_id": _rule_id_of(plan.table_rules, denormalise_step)}
        )
        # A denormalised job reads the driving relation, not the rule's own table.
        # The alias has to be the one the join conditions were built against,
        # otherwise the ON clause references a relation the FROM clause does not
        # name -- valid-looking SQL that fails the moment it runs.
        driving_relation = str((denormalise_step.get("from") or {}).get("driving") or "")
        driving_schema, driving_table = split_relation(driving_relation)
        driving_alias = join_aliases.get(driving_relation.upper()) or "t0"
        chain = Chain.relation(driving_schema, driving_table, driving_alias)

    taken_lookups: List[Tuple[str, str, Optional[str]]] = []
    lookup_index = 0
    for rule in plan.table_rules:
        rule_id = str(rule.get("id"))
        for step in rule.get("steps") or []:
            if step.get("category") != "lookup":
                continue
            lookup_index += 1
            join_sql, taken, lk_assumptions, sentinel, sentinel_expression = compile_lookup(
                step, ref, diags, rule_id, lookup_index
            )
            assumptions.extend(lk_assumptions)
            if join_sql is None:
                job.status = "BLOCKED"
                return job
            joins.append(join_sql)
            taken_lookups.extend(taken)
            if sentinel and sentinel_expression:
                taken_lookups.append((sentinel, sentinel_expression, "integer"))
            operations.append({"operation": "lookup", "rule_id": rule_id})

    # ------------------------------------------------------------------
    # Projection.
    # ------------------------------------------------------------------
    type_resolution: Dict[str, TypeResolution] = {}
    computed_names: set = set()
    #: Target names that a table-level step produced rather than passing through.
    #: A row-selection predicate can only be folded into the projection when it
    #: reads plain columns, so this set -- not the full column list -- is what
    #: decides whether the query needs a wrapper stage.
    produced_names: set = set()
    #: Source columns whose value a recipe rewrote, so a filter on them has to
    #: read the rewritten value and therefore sits above the projection.
    rewritten_sources: set = set()

    def register(
        name: str,
        expression: str,
        type_sql: Optional[str],
        rule_id: Optional[str],
        origin: str,
        source_column: Optional[CatalogColumn],
        note: str = "",
        source_name: Optional[str] = None,
        produced: bool = False,
    ) -> None:
        split_offset: Optional[Tuple[str, str]] = None
        computed_names.add(name)
        if produced:
            produced_names.add(name)
        nullable = True
        if rule_id:
            for rule in plan.column_rules + plan.table_rules:
                if str(rule.get("id")) != rule_id:
                    continue
                target = rule.get("target") or {}
                if target.get("nullable") is False:
                    nullable = False

        resolution: Optional[TypeResolution] = None
        if type_sql:
            resolution = types.declared(str(type_sql), rule_id, None)
        elif source_column is not None:
            resolution = types.from_catalog(source_column, f"{primary_schema}.{primary_table}")
        else:
            resolution = TypeResolution(sql="text", assumptions=["type is unknown without a source catalog"])

        if resolution is not None:
            if resolution.splits_offset:
                # Emitted after the parent column so the projection reads in the
                # order a reader expects: value, then its offset companion.
                offset_name = f"{name}_offset"
                offset_expression = extract_offset_minutes(expression)
                type_resolution[name] = resolution
                split_offset = (offset_name, offset_expression)
            else:
                split_offset = None

            for note_text in resolution.assumptions:
                diags.add(
                    "TYPE_ASSUMPTION",
                    "ASSUMPTION",
                    note_text,
                    rule_id=rule_id,
                    table=f"{primary_schema}.{primary_table}",
                    column=name,
                )
            for fidelity in resolution.fidelity:
                if fidelity.startswith("Oracle") or fidelity.startswith("numeric without"):
                    diags.add(
                        "TYPE_FIDELITY",
                        "ASSUMPTION",
                        fidelity,
                        rule_id=rule_id,
                        table=f"{primary_schema}.{primary_table}",
                        column=name,
                    )

        column = TargetColumn(
            name=name,
            type_sql=resolution.sql if resolution else "text",
            nullable=nullable,
            expression=expression,
            origin=rule_id or origin,
            note=note,
            source_name=source_name,
        )
        if resolution and types.collate_clause(resolution.sql):
            column.collation = "C"
        if source_column is not None:
            source_type = split_oracle_type(source_column.oracle_type)[0]
            if source_type in {"CHAR", "NCHAR", "CHARACTER"} and types.char_semantics == "characters":
                diags.add(
                    "CHAR_BLANK_PADDING_STRIPPED",
                    "EDGE",
                    f"{name} comes from an Oracle CHAR column, which is blank-padded to its declared "
                    "length. The value is carried through unchanged, so a target CHARACTER(n) value "
                    "may carry trailing spaces Oracle would have hidden",
                    rule_id=rule_id,
                    table=f"{primary_schema}.{primary_table}",
                    column=name,
                )
        job.columns.append(column)

        if split_offset:
            offset_name, offset_expression = split_offset
            job.columns.append(
                TargetColumn(
                    name=offset_name,
                    type_sql="smallint",
                    nullable=False,
                    expression=offset_expression,
                    origin=rule_id or origin,
                    note="UTC offset in minutes, per defaults.timestampWithTimeZone=split-offset",
                )
            )
            computed_names.add(offset_name)

    # -- base columns ------------------------------------------------------

    # Rewritten working values, keyed by source column. A recipe publishes here
    # so a later derived column reads the transformed value rather than the raw
    # one, which is what makes the order between a column recipe and a derived
    # recipe on the same column irrelevant rather than load-bearing.
    working: Dict[str, str] = {}

    # Columns that a column recipe owns. The base pass must skip them, or a rename
    # would emit the source column *and* the renamed one and the target relation
    # would have two columns carrying the same value.
    column_transforms, column_renames = split_column_rules(plan.column_rules)
    recipe_columns = set(column_transforms) | set(column_renames)

    if explicit_columns is not None:
        for name, expression, type_sql in explicit_columns:
            register(naming.identifier(name, "column"), expression, type_sql, None, "denormalise", None)
    elif pivot_step is not None:
        pass  # built below
    else:
        # Two ways to know the source's columns, in the order they are trusted.
        #
        # A catalog is authoritative. A table rule's own `columns:` is the next best
        # thing, being the spec stating the shape outright. Failing both, the only
        # columns there are to project are the ones the spec's own steps name --
        # enough for a rule-driven table, and never enough for a plain one, which is
        # why that case is blocked below rather than quietly narrowed.
        if primary_table_meta is not None:
            base_columns = list(primary_table_meta.columns)
        else:
            named = _spec_named_columns(spec, plan)
            base_columns = [CatalogColumn(name=name, oracle_type="") for name in named]
            if not base_columns:
                diags.add(
                    "SOURCE_SHAPE_UNKNOWN",
                    "BLOCK",
                    f"nothing describes the columns of {primary_schema}.{primary_table}, so there "
                    "is no projection to compile and this job would load nothing. Either give the "
                    "table rule a `columns:` list, or supply a catalog for the source schema -- "
                    "see need_catalog.md for which to choose",
                    rule_id=_rule_id_of(plan.table_rules, None),
                    table=f"{primary_schema}.{primary_table}",
                )
                job.status = "BLOCKED"
                return job
            shape_note = (
                f"no catalog describes {primary_schema}.{primary_table}, so the projection covers "
                f"only the {len(base_columns)} column(s) this specification's own rules name "
                f"({sorted(c.name for c in base_columns)}). Any column the rules never mention is "
                "not migrated, and no type is known for these, so the target column type comes "
                "from the rules that touch it"
            )
            diags.add(
                "SOURCE_SHAPE_FROM_RULES",
                "ASSUMPTION",
                shape_note,
                rule_id=_rule_id_of(plan.table_rules, None),
                table=f"{primary_schema}.{primary_table}",
            )
            # Into the artifacts, not just the diagnostic list. A reader of
            # seatunnel.conf or duckdb.yaml is the person who needs to know a
            # projection is narrower than the table, and that reader only has the
            # file.
            assumptions.append(shape_note)

        for catalog_column in base_columns:
            if str(catalog_column.name).upper() in recipe_columns:
                continue
            expression = ref(catalog_column.name)
            if types.empty_string_is_null == "null" and catalog_column.is_character:
                expression = coalesce_nullif_empty(expression, types.empty_string_is_null)
            register(
                naming.identifier(catalog_column.name, "column"),
                expression,
                None,
                None,
                f"{primary_schema}.{primary_table}",
                catalog_column,
                source_name=catalog_column.name,
            )

    # -- per-column recipes -------------------------------------------------
    #
    # One transform per column, decided by precedence, plus any pure rename on
    # top of it. The recipe's own `target` supplies the declared type and
    # nullability, so a rename-only rule still decides the target type.

    def target_name_for(source_name: str, target: Dict[str, Any]) -> str:
        """The target column name a recipe decides on, given its merged target."""
        if target.get("column"):
            return naming.identifier(str(target["column"]), "column")
        return naming.identifier(source_name, "column")

    # For each column, exactly one transform wins. `plan.column_rules` is in
    # ascending precedence order, so the *last* transform for a column is the one
    # that applies -- which is what makes an override replace the rule it
    # overrides rather than being discarded by it.
    winning_transform: Dict[str, int] = {}
    for index, rule in enumerate(plan.column_rules):
        column = str((rule.get("match") or {}).get("column") or "").upper()
        if not column:
            continue
        if not rule.get("steps"):
            continue
        if column in winning_transform:
            loser = plan.column_rules[winning_transform[column]]
            diags.add(
                "COLUMN_RULE_SUPERSEDED",
                "EDGE",
                f"rule `{loser.get('id')}` is superseded for column `{column}` by "
                f"`{rule.get('id')}`. Written as two recipes they compete for the same column and "
                "only one applies; the winner is decided by specificity, with overrides winning",
                rule_id=str(loser.get("id")),
                table=f"{primary_schema}.{primary_table}",
            )
        winning_transform[column] = index

    # A pure rename has no steps, so it cannot compete with a transform: it is a
    # statement about the target name and is folded into whatever transform owns
    # the column, or emitted on its own when nothing does.
    winning_rename: Dict[str, int] = {}
    for index, rule in enumerate(plan.column_rules):
        column = str((rule.get("match") or {}).get("column") or "").upper()
        if column and not rule.get("steps"):
            winning_rename[column] = index

    ordered_recipes: List[Dict[str, Any]] = []
    for index, rule in enumerate(plan.column_rules):
        column = str((rule.get("match") or {}).get("column") or "").upper()
        if not column:
            continue
        if rule.get("steps"):
            if winning_transform.get(column) == index:
                ordered_recipes.append(rule)
            continue
        if column in winning_transform:
            # Folded into the transform below; emitting it too would produce two
            # columns carrying one source value.
            continue
        if winning_rename.get(column) == index:
            ordered_recipes.append(rule)

    for rule in ordered_recipes:
        rule_id = str(rule.get("id"))
        match = rule.get("match") or {}
        source_column_name = str(match.get("column"))
        target = rule.get("target") or {}
        catalog_column = primary_table_meta.column(source_column_name) if primary_table_meta else None

        # A rename-only rule and a transform rule can both name this column; the
        # rename contributes the target name and type, the transform the value.
        rename = column_renames.get(source_column_name.upper())
        if rename is not None and rename is not rule:
            rename_target = rename.get("target") or {}
            if rename_target.get("column") and not target.get("column"):
                target = dict(rename_target)
            elif rename_target.get("type") and not target.get("type"):
                target = {**target, "type": rename_target["type"]}
            elif rename_target.get("nullable") is not None and target.get("nullable") is None:
                target = {**target, "nullable": rename_target["nullable"]}

        target_name = target_name_for(source_column_name, target)

        if not rule.get("steps"):
            # A pure rename still has to reach the target, but only if no
            # transform owns the column. Its declared type is a conversion the
            # target needs, not just a label, so the cast is applied here too.
            expression = ref(source_column_name)
            if target.get("type"):
                expression = cast_to(expression, str(target["type"]))
            register(
                target_name,
                expression,
                target.get("type"),
                rule_id,
                f"{primary_schema}.{primary_table}.{source_column_name}",
                catalog_column,
                source_name=source_column_name,
            )
            continue

        column_expression = ref(source_column_name)
        dropped = False
        reads_source = catalog_column is not None
        rule_type: Optional[str] = target.get("type")
        explicit_type: Optional[str] = None

        for step in rule.get("steps") or []:
            category = str(step.get("category"))
            operation = step.get("operation")

            if category == "value":
                if operation in DROP_OPS:
                    dropped = True
                    operations.append({"operation": "value/drop", "rule_id": rule_id})
                    continue
                column_expression, step_assumptions = compile_value_op(column_expression, step, rule_id, diags)
                assumptions.extend(step_assumptions)
                if step.get("targetType"):
                    rule_type = rule_type or str(step["targetType"])
            elif category == "structural" and operation == "drop-column":
                dropped = True
                operations.append({"operation": "structural/drop-column", "rule_id": rule_id})
            elif category == "structural" and operation == "set-default":
                explicit_type = explicit_type or target.get("type")
            elif category == "escape" and step.get("language") == "sql-scalar":
                # An escape body computes its own value, so the matched column may
                # not exist at all -- `priority-from-amount` builds PRIORITY from
                # AMOUNT and Oracle has no PRIORITY.
                column_expression = escape_body_to_expression(str(step.get("body")), ref, rule_id, diags)
                reads_source = False
                operations.append({"operation": "escape/sql-scalar", "rule_id": rule_id})
            else:
                diags.add(
                    "COLUMN_STEP_NOT_APPLICABLE",
                    "BLOCK",
                    f"rule `{rule_id}` has a {category}/{operation} step that cannot apply to a single "
                    "column; use a table rule instead",
                    rule_id=rule_id,
                )
                dropped = True

        if dropped:
            continue

        if catalog_column is None and not reads_source:
            diags.add(
                "COLUMN_PRODUCED_NOT_TRANSFORMED",
                "EDGE",
                f"rule `{rule_id}` declares target column `{target_name}` from "
                f"{rule.get('steps') or []} rather than transforming {source_column_name}, which the "
                "source does not have",
                rule_id=rule_id,
                table=f"{primary_schema}.{primary_table}",
            )

        if types.empty_string_is_null == "null" and catalog_column is not None and catalog_column.is_character:
            # Applied after the recipe so the recipe sees the raw value, except
            # for trim, which is defined on the raw value anyway.
            column_expression = coalesce_nullif_empty(column_expression, types.empty_string_is_null)

        if target.get("type") and not _has_cast(rule):
            column_expression = cast_to(column_expression, str(target["type"]))

        # Publish the transformed value so a later derived column reads it.
        working[source_column_name] = column_expression
        rewritten_sources.add(source_column_name)

        register(
            target_name,
            column_expression,
            rule_type,
            rule_id,
            f"{primary_schema}.{primary_table}.{source_column_name}",
            catalog_column,
            source_name=source_column_name,
        )

    # -- table-level derived columns ---------------------------------------

    def working_ref(name: str) -> str:
        """Read a column, preferring a value a recipe has already rewritten.

        A derived column must see the transformed value, not the raw one, or
        `status-label` would classify a status that `normalize-customer-status`
        has already rewritten. Column recipes publish into the same working set
        as `input` steps, which is what makes the order between them irrelevant.
        """
        return working.get(name) or ref(name)

    for rule in plan.table_rules:
        rule_id = str(rule.get("id"))
        target = rule.get("target") or {}
        target_column = target.get("column")
        for step in rule.get("steps") or []:
            category = str(step.get("category"))
            operation = step.get("operation")

            if category == "value" and operation in VALUE_OPS and not step.get("input"):
                continue

            if category == "value":
                if operation in DROP_OPS:
                    continue
                # `input` rewrites a working column in place; the recipe's own
                # target column is written by a later derived step.
                if step.get("input"):
                    name = str(step["input"])
                    current = working.get(name) or ref(name)
                    if types.empty_string_is_null == "null":
                        current = coalesce_nullif_empty(current, types.empty_string_is_null)
                    updated, step_assumptions = compile_value_op(current, step, rule_id, diags)
                    assumptions.extend(step_assumptions)
                    working[name] = updated
                    rewritten_sources.add(name)
                    operations.append({"operation": f"value/{operation}", "rule_id": rule_id})
                elif step.get("from") and target_column:
                    name = str(step["from"])
                    current = working.get(name) or ref(name)
                    expression, step_assumptions = compile_value_op(current, step, rule_id, diags)
                    assumptions.extend(step_assumptions)
                    if target.get("type"):
                        expression = cast_to(expression, str(target["type"]))
                    register(
                        naming.identifier(str(target_column), "column"),
                        expression,
                        target.get("type"),
                        rule_id,
                        f"{primary_schema}.{primary_table}.{name}",
                        primary_table_meta.column(name) if primary_table_meta else None,
                    )
                    operations.append({"operation": f"value/{operation}", "rule_id": rule_id})
                else:
                    diags.add(
                        "TABLE_VALUE_STEP_NEEDS_INPUT",
                        "BLOCK",
                        f"rule `{rule_id}` has a value step with neither `input` nor `from` plus a "
                        "recipe target column",
                        rule_id=rule_id,
                    )
                    job.status = "BLOCKED"
                    return job

            elif category == "derived" and target_column:
                names = [str(v) for v in step.get("inputs") or []]
                inputs = [working_ref(name) for name in names]
                expression, step_assumptions = compile_derived_op(step, inputs, rule_id, diags)
                assumptions.extend(step_assumptions)
                if expression is None:
                    job.status = "BLOCKED"
                    return job
                if target.get("type"):
                    expression = cast_to(expression, str(target["type"]))
                register(
                    naming.identifier(str(target_column), "column"),
                    expression,
                    target.get("type") or _infer_derived_type(operation, names, primary_table_meta, rule_id, diags),
                    rule_id,
                    f"{primary_schema}.{primary_table} derived",
                    None,
                    produced=True,
                )
                operations.append({"operation": f"derived/{operation}", "rule_id": rule_id})

            elif category == "structural" and operation == "add-column" and target_column:
                value_source = step.get("valueSource") or {}
                if "constant" in value_source:
                    expression = sql_literal(value_source["constant"])
                elif "generated" in value_source:
                    generated = str(value_source["generated"])
                    expression = {
                        "loaded-at": "CURRENT_TIMESTAMP",
                        "now": "CURRENT_TIMESTAMP",
                        "run-start": f"{SCN_BIND}",
                        "source-scn": f"{SCN_BIND}",
                    }.get(generated)
                    if expression is None:
                        diags.add(
                            "ADD_COLUMN_GENERATED_UNKNOWN",
                            "BLOCK",
                            f"add-column valueSource.generated `{generated}` is not implemented",
                            rule_id=rule_id,
                        )
                        job.status = "BLOCKED"
                        return job
                    if generated == "run-start" or generated == "source-scn":
                        expression = f"TO_TIMESTAMP({SCN_BIND.lstrip(':')})"
                else:
                    diags.add(
                        "ADD_COLUMN_NO_VALUE_SOURCE",
                        "BLOCK",
                        f"rule `{rule_id}` add-column has no constant or generated valueSource",
                        rule_id=rule_id,
                    )
                    job.status = "BLOCKED"
                    return job
                if target.get("type"):
                    expression = cast_to(expression, str(target["type"]))
                register(
                    naming.identifier(str(target_column), "column"),
                    expression,
                    target.get("type"),
                    rule_id,
                    f"{primary_schema}.{primary_table} constant",
                    None,
                    produced=True,
                )
                operations.append({"operation": "structural/add-column", "rule_id": rule_id})

            elif category == "column-split":
                split_columns, sentinel, sentinel_expression, split_assumptions = compile_column_split(
                    step, ref, diags, rule_id
                )
                assumptions.extend(split_assumptions)
                if sentinel and sentinel_expression:
                    split_columns.append((sentinel, sentinel_expression, "integer"))
                for name, expression, type_sql in split_columns:
                    register(
                        naming.identifier(name, "column"),
                        expression,
                        type_sql,
                        rule_id,
                        f"{primary_schema}.{primary_table}.{step.get('column')} split",
                        None,
                        produced=True,
                    )
                operations.append({"operation": "column-split", "rule_id": rule_id})

            elif category == "change-aware":
                job.upsert = True
                watched = [str(v) for v in step.get("columns") or []]
                operations.append({"operation": "change-aware/propagate-if-changed", "rule_id": rule_id})
                job.validation_strategies.append(
                    {
                        "check": "change-aware-propagation",
                        "rule_id": rule_id,
                        "columns": watched,
                        "strategy": "sink-upsert",
                        "note": (
                            "change-aware is enforced by writing with upsert on the target key and "
                            "excluding these columns from the reconcile digest, not by a projection "
                            "expression; a projection cannot see the previous image"
                        ),
                    }
                )
                diags.add(
                    "CHANGE_AWARE_SINK_STRATEGY",
                    "EDGE",
                    f"change-aware on {watched} is applied as an upsert write on the target key; "
                    "a projection cannot compare against the previous row image",
                    rule_id=rule_id,
                )

            elif category == "key-declaration":
                operations.append({"operation": "key-declaration", "rule_id": rule_id})
                origin = str(step.get("origin") or "")
                stability = str(step.get("stability") or "")
                if stability != "immutable":
                    diags.add(
                        "KEY_NOT_IMMUTABLE",
                        "GOVERNANCE",
                        f"key-declaration stability is `{stability}`; a mutable key makes upsert "
                        "and CDC restart unsafe",
                        rule_id=rule_id,
                    )
                diags.add(
                    "KEY_ORIGIN",
                    "ASSUMPTION",
                    f"key-declaration origin={origin}; the target primary key is created from the "
                    "declared columns without re-proving uniqueness in the source",
                    rule_id=rule_id,
                )

            elif category == "escape" and step.get("language") == "sql-set":
                # The body became the job's source relation, so there is no
                # projection contribution here. `escape/sql-scalar` is the branch
                # above: it does produce a column.
                continue

            elif category in ELSEWHERE_HANDLED_CATEGORIES:
                # These shape the job rather than the projection: row-selection
                # becomes the filter, lookup the join, set/cardinality/relational/
                # pivot the source relation. They are compiled elsewhere.
                continue

            else:
                # Regression guard: an unrecognised category used to fall through
                # every branch and be dropped, so the rule silently did nothing.
                # A specification written against a newer schema must fail loudly.
                diags.add(
                    "TABLE_STEP_CATEGORY_UNHANDLED",
                    "BLOCK",
                    f"rule `{rule_id}` has a step of category `{category}` / operation "
                    f"`{operation or '-'}`, which this compiler has no rendering for. Nothing is "
                    "guessed: the job is blocked so the rule cannot be silently ignored",
                    rule_id=rule_id,
                    table=f"{primary_schema}.{primary_table}",
                    category=category,
                )
                job.status = "BLOCKED"
                return job

    # -- lookup columns -----------------------------------------------------

    for name, expression, type_sql in taken_lookups:
            is_sentinel = name.startswith(QUARANTINE_PREFIX)
            if is_sentinel:
                job.quarantine_filters.append(
                    {"column": naming.identifier(name, "column"), "expression": expression}
                )
            register(
                naming.identifier(name, "column"),
                expression,
                type_sql,
                None,
                "lookup",
                None,
                produced=True,
            )

    # -- pivot --------------------------------------------------------------

    group_by: List[str] = []
    if pivot_step is not None:
        pivot_columns, grain, sentinel, sentinel_expression, pivot_assumptions = compile_pivot(
            pivot_step, ref, diags, _rule_id_of(plan.table_rules, pivot_step)
        )
        assumptions.extend(pivot_assumptions)
        pivot_rule_id = _rule_id_of(plan.table_rules, pivot_step)
        # The grain is registered first so the projection reads key-then-measure,
        # which is the order a reader of a pivoted table expects.
        for column in grain:
            register(
                naming.identifier(column, "column"),
                ref(column),
                None,
                None,
                "pivot-grain",
                primary_table_meta.column(column) if primary_table_meta else None,
            )
            group_by.append(ref(column))
        for name, expression, type_sql in pivot_columns:
            register(
                naming.identifier(name, "column"),
                expression,
                type_sql,
                pivot_rule_id,
                "pivot",
                None,
                produced=True,
            )
        if sentinel and sentinel_expression:
            register(
                naming.identifier(sentinel, "column"),
                sentinel_expression,
                "integer",
                pivot_rule_id,
                "pivot-unlisted",
                None,
                produced=True,
            )
            job.quarantine_filters.append(
                {"column": naming.identifier(sentinel, "column"), "expression": sentinel_expression}
            )
        job.snapshot_only = True
        operations.append({"operation": "pivot", "rule_id": pivot_rule_id})

    # ------------------------------------------------------------------
    # Assemble the query.
    # ------------------------------------------------------------------
    if not job.columns:
        diags.add(
            "JOB_NO_COLUMNS",
            "BLOCK",
            f"{plan.job_id} resolves to no columns; a query with no projection cannot be validated "
            "against a target relation",
            rule_id=plan.job_id,
        )
        job.status = "BLOCKED"
        return job

    # Two columns with the same name would make the target table unrecreatable and
    # would silently drop one of them on load, so it blocks rather than warns.
    seen_names: Dict[str, int] = {}
    for column in job.columns:
        seen_names[column.name] = seen_names.get(column.name, 0) + 1
    duplicates = sorted(name for name, count in seen_names.items() if count > 1)
    if duplicates:
        diags.add(
            "DUPLICATE_TARGET_COLUMN",
            "BLOCK",
            f"{plan.job_id} projects {duplicates} more than once. Two recipes claim the same target "
            "name, so the target relation cannot be created and one value would be dropped on load",
            rule_id=plan.job_id,
            columns=duplicates,
        )
        job.status = "BLOCKED"
        return job

    # Primary keys.
    job.primary_keys = _primary_keys(spec, plan, naming, job, declared_key, catalog, diags)
    for column in job.columns:
        if column.name in job.primary_keys:
            column.is_pk = True
            column.nullable = False

    projections = [f"{column.expression} AS {qident(column.name)}" for column in job.columns]
    select_sql = f"SELECT {', '.join(projections)} FROM {chain.from_sql(joins)}"
    if group_by:
        select_sql += f" GROUP BY {', '.join(group_by)}"

    body = select_sql

    # Row selection: intersect every predicate on the table. A filter that reads a
    # computed column has to sit above the projection, which is what the wrap is
    # for.
    predicates: List[str] = []
    predicate_columns: List[str] = []
    for rule in plan.table_rules:
        for step in rule.get("steps") or []:
            if step.get("category") != "row-selection" or not step.get("predicate"):
                continue
            try:
                predicates.append(build_predicate(step["predicate"], ref))
            except CompileError as exc:
                # Spec validation already reported the cause; this makes sure the
                # job does not compile a partial query on top of a bad predicate.
                diags.add(
                    "PREDICATE_UNCOMPILABLE",
                    "BLOCK",
                    f"rule `{rule.get('id')}` has an unusable row-selection predicate: {exc}",
                    rule_id=str(rule.get("id")),
                    table=f"{primary_schema}.{primary_table}",
                )
                job.status = "BLOCKED"
                return job
            predicate_columns.extend(collect_predicate_columns(step["predicate"]))

    if plan.discriminator:
        discriminator, value = plan.discriminator
        predicates.append(f"{ref(discriminator)} = {sql_literal(value)}")
        predicate_columns.append(discriminator)
        operations.append({"operation": "cardinality/split", "value": value})

    # A watermark predicate, when the run is query-incremental.
    watermark = _watermark_predicate(spec, ref, primary_table_meta, diags, plan)
    if watermark:
        predicates.append(watermark)
        watermark_column = str((spec.get("run") or {}).get("watermark", {}).get("column") or "")
        predicate_columns.append(watermark_column)

    combined = combine_predicates(predicates)
    if combined:
        # A filter above the projection reads projected *names*, not source names.
        # Building the map from the compiled columns is the only reliable way to
        # know what a source column was renamed to.
        source_to_target: Dict[str, str] = {}
        for column in job.columns:
            if column.source_name:
                source_to_target[column.source_name.upper()] = column.name

        # A predicate can only be folded into the projection when every column it
        # reads is a plain pass-through. Two things make that untrue: the column
        # is *produced* by a derived, structural, lookup, pivot or split step, so
        # it does not exist below the projection; or the column was *rewritten* by
        # a recipe, so the filter has to compare the transformed value rather than
        # the raw one.
        needs_wrap = group_by or chain.is_derived
        for column in predicate_columns:
            if naming.identifier(column, "column") in produced_names:
                needs_wrap = True
                break
            if column in rewritten_sources:
                needs_wrap = True
                break
            target = source_to_target.get(column.upper())
            if target and target in produced_names:
                needs_wrap = True
                break

        if needs_wrap:
            body = f"SELECT * FROM ({select_sql}) AS {qident(INNER_ALIAS)}"
            combined = _requalify_predicate(
                combined, ref, Ref(INNER_ALIAS, True), source_to_target, produced_names
            )
        body += f" WHERE {combined}"

    query, glot = render_ctunnel(body)

    job.query = query
    job.sqlglot = glot
    job.assumptions = sorted(set(assumptions))
    job.operations = operations

    if query is None:
        job.status = "BLOCKED"
        diags.add(
            "QUERY_RENDER_FAILED",
            "BLOCK",
            f"{plan.job_id} could not be rendered: {glot.get('error', 'unknown')}",
            rule_id=plan.job_id,
        )

    if job.sqlglot.get("stable") == "DRIFT":
        diags.add(
            "QUERY_UNSTABLE",
            "BLOCK",
            f"{plan.job_id} does not round-trip byte-identically, so it would reflow between passes",
            rule_id=plan.job_id,
        )
        job.status = "BLOCKED"

    job.sources_named = sorted(set(job.sources_named) | {f"{primary_schema}.{primary_table}"})

    # Sensitivity cross-check: a masked column must actually be masked.
    _check_sensitive_delivery(spec, plan, job, diags)

    job.binds = _binds(spec)
    job.ddl = build_target_ddl(spec, job, types, naming, catalog, diags)
    return job


def _requalify_predicate(
    predicate: str,
    inner_ref: Ref,
    outer_ref: Ref,
    source_to_target: Dict[str, str],
    produced_names: Optional[set] = None,
) -> str:
    """Re-point a predicate at the wrapping relation.

    The predicate was rendered against the source alias, so it names source
    columns. Once it sits above the projection it must name the *projected*
    columns instead -- which are not the same thing, because a rule may have
    renamed them. Substituting through the compiled source-to-target map is what
    keeps `WHERE created >= ...` from referring to a column the projection calls
    `created_on`, which is a runtime error the compiler can and must catch.

    A column a table-level step *produced* -- `line_total` -- has no source name
    at all, so it is qualified by target name instead of left bare. Leaving it
    bare happens to work over a single derived table, but it stops working the
    moment anything is joined above the wrapper.
    """
    produced_names = produced_names or set()

    if not source_to_target and not produced_names:
        return predicate

    # Longest source name first, so CREATED cannot rewrite inside CREATED_AT.
    ordered = sorted(source_to_target.items(), key=lambda entry: -len(entry[0]))

    result = predicate
    for source_name, target_name in ordered:
        for before, after in (
            (inner_ref(source_name), outer_ref(target_name)),
            (f'"{source_name}"', outer_ref(target_name)),
            (source_name, target_name),
        ):
            if before and before in result:
                result = result.replace(before, after)
                break

    # Qualify any bare reference to a produced column with the wrapper alias.
    for name in sorted(produced_names, key=len, reverse=True):
        bare = f'"{name}"'
        if f"{outer_ref(name)}" in result:
            continue
        if re.search(rf'(?<![\w."]){re.escape(bare)}(?!")', result):
            result = re.sub(rf'(?<![\w."]){re.escape(bare)}(?!")', outer_ref(name), result)

    return result


def _infer_derived_type(
    operation: str,
    inputs: List[str],
    catalog: Optional[CatalogTable],
    rule_id: Optional[str],
    diags: Diagnostics,
) -> Optional[str]:
    """Type a derived column from its inputs when the rule declares no type.

    Only the operations whose result type *is* their inputs' type can be typed
    this way. A `case-when` over a code column produces a label, not the code's
    type, so guessing there would be worse than admitting the type is unknown.
    """
    if catalog is None or operation not in {"coalesce", "concat"}:
        return None

    resolved: List[str] = []
    for name in inputs:
        column = catalog.column(name)
        if column is None:
            return None
        resolved.append(split_oracle_type(column.oracle_type)[0])

    if not resolved:
        return None

    if len(set(resolved)) > 1:
        diags.add(
            "DERIVED_INPUT_TYPES_DIFFER",
            "ASSUMPTION",
            f"rule `{rule_id}` derives a column from inputs of differing types {sorted(set(resolved))} "
            "and declares no target type, so the result is carried as text",
            rule_id=rule_id,
        )
        return None

    base = resolved[0]
    if base in {"NUMBER", "NUMERIC", "DECIMAL"} and operation == "coalesce":
        arguments = split_oracle_type(catalog.column(inputs[0]).oracle_type)[1]
        return f"numeric{arguments}"
    if base in {"VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "VARCHAR", "STRING"} and operation == "coalesce":
        length = _varchar_length(split_oracle_type(catalog.column(inputs[0]).oracle_type)[1])
        # The declared length is the *source* length. A coalesce of two columns of
        # equal length still needs headroom once the values are concatenated
        # upstream, so the widest declared length is carried and the value check
        # in the validation contract is what catches a genuine overflow.
        return "text" if length <= 0 else f"character varying({length})"

    return None


def _varchar_length(arguments: str) -> int:
    match = re.search(r"\d+", arguments or "")
    return int(match.group(0)) if match else 0


def _has_cast(rule: Dict[str, Any]) -> bool:
    return any(step.get("operation") == "cast" for step in rule.get("steps") or [])


def _declared_key(plan: JobPlan, diags: Diagnostics) -> List[str]:
    keys: List[str] = []
    for rule in plan.table_rules:
        for step in rule.get("steps") or []:
            if step.get("category") != "key-declaration":
                continue
            for column in step.get("columns") or []:
                if str(column) not in keys:
                    keys.append(str(column))
    return keys


def _inline_source_tables(spec: Dict[str, Any]) -> List[CatalogTable]:
    """Every source relation whose shape a table rule declares inline.

    A table rule may state what its own relation contains:

        rules:
          - id: orders-table
            match:   { objectClass: table, schema: HR, name: ORDERS }
            target:  { schema: public, table: orders }
            columns:
              - { name: ORDER_ID, type: "NUMBER(10)", nullable: false }
            primaryKey: [ORDER_ID]

    That is the specification being self-sufficient: the shape the compiler needs is
    right here, so no catalog is needed. The shape is identical to a catalog entry,
    so the two are interchangeable and a rule reads the same either way.

    `columns:` describes the relation being migrated **from** -- the rule's `match`,
    not the target it lands in. A rule matching `HR.ORDERS` says what HR.ORDERS
    contains, whatever the target is called.

    Several rules may match one table; their column lists concatenate in rule order
    and de-duplicate by name, because a later rule declaring a column the earlier one
    already declared is refining it, not adding a second one. `primaryKey:` entries
    accumulate the same way.

    A rule matching a wildcard schema is skipped: it describes every table it
    matched, so its columns cannot be attributed to one relation without a catalog
    to say what that relation is.
    """
    by_relation: Dict[Tuple[str, str], CatalogTable] = {}
    order: List[Tuple[str, str]] = []

    for rule in ordered_rules(spec):
        match = rule.get("match") or {}
        if str(match.get("objectClass") or "") != "table":
            continue
        schema = str(match.get("schema") or "")
        name = str(match.get("name") or "")
        if not schema or not name or "*" in schema or "*" in name:
            continue

        declared = rule.get("columns") or []
        keys = [str(k) for k in rule.get("primaryKey") or []]
        if not declared and not keys:
            continue

        relation = (schema.upper(), name.upper())
        table = by_relation.get(relation)
        if table is None:
            table = CatalogTable(schema=schema, name=name, columns=[], from_spec=True)
            by_relation[relation] = table
            order.append(relation)

        seen = {c.name.upper() for c in table.columns}
        for entry in declared:
            if isinstance(entry, str):
                column = CatalogColumn(name=entry)
            elif isinstance(entry, dict):
                column = CatalogColumn(
                    name=str(entry.get("name") or ""),
                    oracle_type=str(entry.get("type") or entry.get("oracleType") or ""),
                    nullable=bool(entry.get("nullable", True)),
                    char_semantics=str(entry.get("charSemantics") or "characters"),
                )
            else:
                continue
            if not column.name or column.name.upper() in seen:
                continue
            seen.add(column.name.upper())
            table.columns.append(column)

        known = {k.upper() for k in table.primary_key}
        for key in keys:
            if key and key.upper() not in known:
                known.add(key.upper())
                table.primary_key.append(key)

    return [by_relation[relation] for relation in order if by_relation[relation].columns]


def merge_inline_source_tables(catalog: Catalog, spec: Dict[str, Any]) -> Catalog:
    """Fold each table rule's own `columns:` list into the catalog.

    This is what lets a specification be compiled with no catalog file at all: the
    shapes it needs are stated in the specification, and from here on nothing
    downstream can tell the difference -- the key check, the watermark and the type
    inference all read the same catalog either way. The synthesised tables are
    marked `from_spec`, which is how a job knows to say where its shape came from.

    The catalog wins per relation. A catalog is extracted from Oracle and is
    therefore authoritative; the inline list exists so a specification can be
    compiled without one, not so it can overrule one. A stale hand-written list
    silently governing a migration is worse than a redundant one.
    """
    inline = _inline_source_tables(spec)
    if not inline:
        return catalog

    merged = list(catalog.tables)
    for table in inline:
        if catalog.get(table.schema, table.name) is not None:
            continue
        merged.append(table)

    return Catalog(tables=merged, source=catalog.source)


def _spec_named_columns(spec: Dict[str, Any], plan: JobPlan) -> List[str]:
    """Columns the specification names, used when there is no catalog."""
    found: List[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value and value not in found:
            found.append(value)

    # Names the specification's own recipes produce. Column recipes run first
    # (ADR-0098), so a row-selection reading one of these -- `line_total` from
    # a derived arithmetic step -- reads the recipe's output, not a source
    # column. Adding it to the projection anyway would register it twice, once
    # as a base column and once as the recipe's own, and the job would block as
    # DUPLICATE_TARGET_COLUMN: a job the artifacts then lack, for a filter the
    # specification is entitled to write. The catalog path never had this
    # problem -- a computed name is simply absent from what Oracle reports --
    # so the floor has to say the same thing the catalog would.
    produced = {
        str((rule.get("target") or {}).get("column")).upper()
        for rule in plan.column_rules + plan.table_rules
        if (rule.get("target") or {}).get("column")
    }

    for rule in plan.column_rules:
        match = rule.get("match") or {}
        add(match.get("column"))
        for step in rule.get("steps") or []:
            add(step.get("input"))
            add(step.get("from"))

    for rule in plan.table_rules:
        for step in rule.get("steps") or []:
            add(step.get("input"))
            add(step.get("from"))
            add(step.get("column"))
            for value in step.get("inputs") or []:
                add(value)
            for value in step.get("using") or []:
                add(value)
            for value in step.get("columns") or []:
                add(value)
            if isinstance(step.get("on"), str):
                add(step.get("on"))
            aggregate = step.get("aggregate") or {}
            if isinstance(aggregate.get("column"), str):
                add(aggregate.get("column"))
            for value in collect_predicate_columns(step.get("predicate")):
                # A predicate on a produced name reads the recipe's value
                # (ADR-0098), so it is not naming a source column here.
                if str(value).upper() not in produced:
                    add(value)
            order = step.get("orderSensitive") or {}
            for value in order.get("orderBy") or []:
                add(value)

    return found


def _merge_column_order(plan: JobPlan, catalog: Catalog, diags: Diagnostics) -> List[str]:
    """Column order for every branch of a UNION ALL.

    A UNION requires the same column list in the same order in every branch, and
    the projection is generated once, so the order has to be computed from all
    sources rather than from the first one.
    """
    order: List[str] = []
    tables: List[CatalogTable] = []
    for raw in plan.merge_sources:
        entry = merge_source_entry(raw)
        schema, table = split_relation(str(entry.get("relation") or entry.get("from")))
        catalog_table = catalog.get(schema or "", table or "")
        if catalog_table is None:
            diags.add(
                "MERGE_SOURCE_NOT_IN_CATALOG",
                "ASSUMPTION",
                f"merge source {schema}.{table} is not in the catalog, so its column list cannot be "
                "read; the merge projects only the columns the specification names",
            )
            continue
        tables.append(catalog_table)
        for column in catalog_table.column_names:
            if column not in order:
                order.append(column)

    if tables:
        # Every branch must supply every projected column or the union fails at
        # run time, so a column missing from any source is reported now.
        for table in tables[1:]:
            for column in order:
                if table.column(column) is None:
                    diags.add(
                        "MERGE_COLUMN_MISSING_IN_SOURCE",
                        "BLOCK",
                        f"merge source {table.schema}.{table.name} has no column `{column}`, so the "
                        "UNION ALL would fail at run time",
                        table=f"{table.schema}.{table.name}",
                        column=column,
                    )
        return order

    # No catalog: project only what the specification names, so the union at
    # least has the same shape on both sides.
    for rule in plan.column_rules + plan.table_rules:
        match = rule.get("match") or {}
        if match.get("column") and str(match["column"]) not in order:
            order.append(str(match["column"]))
        for step in rule.get("steps") or []:
            for value in step.get("inputs") or []:
                if str(value) not in order:
                    order.append(str(value))
    return order


def _primary_keys(
    spec: Dict[str, Any],
    plan: JobPlan,
    naming: Naming,
    job: CompiledJob,
    declared_key: List[str],
    catalog: Catalog,
    diags: Diagnostics,
) -> List[str]:
    """Choose the target primary key.

    The shape of the job decides where the key comes from, because the shapes
    genuinely define different keys:

    * a pivot redefines the key as its declared grain -- a stock pivot has one
      row per product, so the stock row's own key cannot be the target's key;
    * a declared key-declaration wins over the catalog;
    * otherwise the source catalog's key is carried across, renamed, and dropped
      from the result if a recipe removed it, which is a blocking finding because
      an unkeyed upsert silently duplicates rows.
    """
    by_source: Dict[str, str] = {}
    for column in job.columns:
        if column.source_name:
            by_source[column.source_name.upper()] = column.name
        elif column.origin:
            by_source[column.origin.split(".")[-1].upper()] = column.name

    target_names = {column.name for column in job.columns}

    def mapped(column: str) -> Optional[str]:
        target_name = by_source.get(column.upper(), naming.identifier(column, "column"))
        return target_name if target_name in target_names else None

    # A pivot's grain is its key.
    pivot_step = _find_step(plan.table_rules, "pivot", None)
    if pivot_step is not None:
        grain = [str(v) for v in pivot_step.get("grain") or []]
        keys = [key for key in (mapped(column) for column in grain) if key]
        if keys:
            diags.add(
                "KEY_FROM_PIVOT_GRAIN",
                "ASSUMPTION",
                f"{plan.target_table} is a pivot, so its key is the declared grain {grain} rather than "
                "the source row's own key. A duplicate grain would collapse rows through the "
                "aggregate, which is why the grain-uniqueness check is blocking.",
            )
        return keys

    if declared_key:
        diags.add(
            "GOVERNANCE_DECLARED_KEY",
            "GOVERNANCE",
            f"a key is declared for {plan.target_table} on {declared_key}; "
            "governance.requiresApproval names declared-key",
        )
        return [mapped(column) or naming.identifier(column, "column") for column in declared_key]

    source = job.sources[0] if job.sources else None
    if source is None:  # pragma: no cover - defensive
        return []

    catalog_table = catalog.get(source["schema"], source["table"])
    if catalog_table is None or not catalog_table.primary_key:
        diags.add(
            "NO_PRIMARY_KEY",
            "ASSUMPTION",
            f"{source['schema']}.{source['table']} has no declared or catalog key, so the target "
            f"{plan.target_table} is created without a primary key. An upsert write needs one, so any "
            "change-aware rule on this table cannot be honoured",
            table=f"{source['schema']}.{source['table']}",
        )
        return []

    keys: List[str] = []
    for column in catalog_table.primary_key:
        target_name = mapped(column)
        if target_name:
            keys.append(target_name)
        else:
            diags.add(
                "KEY_COLUMN_DROPPED",
                "BLOCK",
                f"primary key column `{column}` of {source['schema']}.{source['table']} does not reach "
                f"the target, so {plan.target_table} cannot be keyed on it",
                table=f"{source['schema']}.{source['table']}",
                column=column,
            )

    if keys:
        diags.add(
            "KEY_FROM_CATALOG",
            "ASSUMPTION",
            f"the primary key of {source['schema']}.{source['table']} is carried from the source "
            "catalog; uniqueness in the target is proven by the duplicate-key check, not here",
            table=f"{source['schema']}.{source['table']}",
        )

    return keys


def _watermark_predicate(
    spec: Dict[str, Any],
    ref: Ref,
    catalog_table: Optional[CatalogTable],
    diags: Diagnostics,
    plan: JobPlan,
) -> Optional[str]:
    """The incremental-read predicate, when this table actually carries the watermark.

    ``run.watermark`` names one column for the whole run, so it cannot be applied
    to every table: a lookup dimension with no such column would be filtered on a
    column it does not have and the job would fail. It is therefore applied only
    where the catalog shows the column, and the tables that receive it are
    reported so the assumption is visible rather than buried.
    """
    run = spec.get("run") or {}
    watermark = run.get("watermark") or {}
    if run.get("acquisition") != "query-incremental" or not watermark.get("column"):
        return None

    column = str(watermark["column"])

    if catalog_table is None:
        diags.add(
            "WATERMARK_NO_CATALOG",
            "ASSUMPTION",
            f"run.watermark.column is {column} but no source catalog was supplied, so this job cannot "
            "tell whether the column exists. The predicate is applied and the job must be checked "
            "against the source before it runs",
            rule_id=plan.job_id,
        )
        return f"{ref(column)} > {WATERMARK_BIND}"

    if catalog_table.column(column) is None:
        diags.add(
            "WATERMARK_COLUMN_NOT_ON_TABLE",
            "EDGE",
            f"run.watermark.column is {column}, which {catalog_table.schema}.{catalog_table.name} "
            "does not have, so this table is read in full. A dimension table is normally read in full; "
            "if it is large enough to matter, give it its own incremental declaration",
            rule_id=plan.job_id,
            table=f"{catalog_table.schema}.{catalog_table.name}",
        )
        return None

    strategy = str(watermark.get("strategy") or "monotonic-key")
    if strategy not in {"monotonic-key", "timestamp", "sequence"}:
        diags.add(
            "WATERMARK_STRATEGY_NOT_INCREMENTAL",
            "BLOCK",
            f"run.watermark.strategy `{strategy}` cannot be expressed as a query-incremental predicate",
        )
        return None

    late = watermark.get("lateArrival") or {}
    if str(late.get("handling")) == "overlap-window":
        window = int(late.get("windowKeys") or 0)
        diags.add(
            "WATERMARK_OVERLAP_WINDOW",
            "EDGE",
            f"lateArrival.handling=overlap-window with windowKeys={window} needs a runtime overlap of "
            f"that many keys for {column}. The compiled query binds the last committed watermark and "
            "the SeaTunnel runner holds the overlap, because a fixed literal would silently drop late rows",
            rule_id=plan.job_id,
        )

    diags.add(
        "WATERMARK_APPLIED",
        "EDGE",
        f"{catalog_table.schema}.{catalog_table.name} is read incrementally on {column} "
        f"(strategy={strategy})",
        rule_id=plan.job_id,
        table=f"{catalog_table.schema}.{catalog_table.name}",
    )

    return f"{ref(column)} > {WATERMARK_BIND}"


def _binds(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    binds = [
        {
            "name": SCN_BIND.lstrip(":"),
            "placeholder": SCN_BIND,
            "from": SCN_BIND_PATH,
            "type": "number",
            "description": "Oracle SCN captured before the run reads the source",
        }
    ]
    run = spec.get("run") or {}
    if run.get("acquisition") == "query-incremental":
        binds.append(
            {
                "name": WATERMARK_BIND.lstrip(":"),
                "placeholder": WATERMARK_BIND,
                "from": WATERMARK_BIND_PATH,
                "type": "number",
                "description": "Last committed watermark; the overlap window is applied at runtime",
            }
        )
    return binds


def _relations_in(sql: str) -> List[str]:
    parsed, _ = parse_ctunnel(sql)
    if parsed is None:
        return []
    found: List[str] = []
    for table in parsed.find_all(exp.Table):
        name = ".".join(part for part in (table.db, table.name) if part)
        if name:
            found.append(strip_quotes(name))
    return found


def _check_sensitive_delivery(
    spec: Dict[str, Any], plan: JobPlan, job: CompiledJob, diags: Diagnostics
) -> None:
    """A declared `handling` must be visible in the compiled query."""
    for entry in spec.get("sensitive") or []:
        match = entry.get("match") or {}
        if not fnmatch.fnmatch(plan.sources[0][0], str(match.get("schema") or "*")):
            continue
        if not fnmatch.fnmatch(plan.sources[0][1], str(match.get("name") or "*")):
            continue
        column = match.get("column")
        if not column:
            continue
        handling = str(entry.get("handling") or "")
        target_name = naming_identifier_for(job, str(column))
        present = {c.name for c in job.columns}
        if handling == "drop":
            if target_name in present:
                diags.add(
                    "SENSITIVE_DROP_NOT_APPLIED",
                    "BLOCK",
                    f"{match.get('schema')}.{match.get('name')}.{column} is declared handling=drop but "
                    "still reaches the target",
                    table=f"{plan.sources[0][0]}.{plan.sources[0][1]}",
                )
            continue
        if handling == "mask":
            compiled = next((c for c in job.columns if c.name == target_name), None)
            if compiled is None:
                diags.add(
                    "SENSITIVE_MASK_COLUMN_MISSING",
                    "BLOCK",
                    f"{column} is declared handling=mask but is not on the target at all. Target "
                    f"columns are {sorted(present)}",
                    table=f"{plan.sources[0][0]}.{plan.sources[0][1]}",
                )
            elif not _looks_masked(compiled.expression):
                diags.add(
                    "SENSITIVE_MASK_NOT_IN_EXPRESSION",
                    "BLOCK",
                    f"{column} is declared handling=mask but its compiled expression "
                    f"`{compiled.expression}` is not a masking expression",
                    table=f"{plan.sources[0][0]}.{plan.sources[0][1]}",
                )


def _looks_masked(expression: str) -> bool:
    """Is this expression a masking expression rather than a pass-through?"""
    upper = str(expression).upper()
    return any(
        marker in upper
        for marker in ("STANDARD_HASH", "SUBSTR", "MD5", "SHA2", "REDACTED", "'***'")
    )


def naming_identifier_for(job: CompiledJob, source_name: str) -> str:
    """The target name a source column arrived under, or the source name."""
    for column in job.columns:
        if column.source_name and column.source_name.upper() == source_name.upper():
            return column.name
    return source_name


# ---------------------------------------------------------------------------
# Target PostgreSQL DDL
# ---------------------------------------------------------------------------

def _physical_for(spec: Dict[str, Any], plan: JobPlan) -> Dict[str, Any]:
    for entry in spec.get("targetPhysical") or []:
        match = entry.get("match") or {}
        if fnmatch.fnmatch(plan.sources[0][0], str(match.get("schema") or "*")) and fnmatch.fnmatch(
            plan.sources[0][1], str(match.get("name") or "*")
        ):
            return entry
    return {}


def build_target_ddl(
    spec: Dict[str, Any],
    job: CompiledJob,
    types: TypeResolver,
    naming: Naming,
    catalog: Catalog,
    diags: Diagnostics,
) -> Dict[str, Any]:
    """Build the statements that create the target relation.

    The DDL is emitted as text and then proven by parse -> generate -> parse in the
    ``pgcontract`` dialect, so a table that cannot be created is never handed to
    SeaTunnel.
    """
    target_schema = job.target["schema"]
    target_table = job.target["table"]
    physical = _physical_for(spec, job.plan)
    partition = physical.get("partition") or {}

    statements: List[Tuple[str, str]] = []
    needs_catalog = False

    for column in job.columns:
        if column.type_sql == "text" and column.origin and "catalog" not in column.origin:
            if not column.note:
                needs_catalog = True

    column_lines: List[str] = []
    for column in job.columns:
        line = f"{qident(column.name)} {column.type_sql}"
        if column.collation:
            line += f' COLLATE "{column.collation}"'
        if not column.nullable:
            line += " NOT NULL"
        column_lines.append(line)

    if job.primary_keys:
        constraint = f"pk_{normalized_relation_name(target_schema, target_table)}"
        columns = ", ".join(qident(name) for name in job.primary_keys)
        column_lines.append(f"CONSTRAINT {qident(constraint)} PRIMARY KEY ({columns})")

    create = (
        f"CREATE TABLE IF NOT EXISTS {pg_ident(target_schema)}.{pg_ident(target_table)} "
        f"({', '.join(column_lines)})"
    )

    if partition.get("strategy"):
        strategy = str(partition["strategy"])
        partition_columns = ", ".join(
            pg_ident(naming.identifier(str(c), "column")) for c in partition.get("columns") or []
        )
        if strategy == "range" and partition_columns:
            create += f" PARTITION BY RANGE ({partition_columns})"
            partition_name = f"{target_table}_p_default"
            statements.append(
                (
                    "partition-default",
                    f"CREATE TABLE IF NOT EXISTS {pg_ident(target_schema)}.{pg_ident(partition_name)} "
                    f"PARTITION OF {pg_ident(target_schema)}.{pg_ident(target_table)} DEFAULT",
                )
            )
            source_strategy = str(partition.get("sourceStrategy") or "")
            diags.add(
                "PARTITION_BOUNDS_REQUIRE_PROFILE",
                "ASSUMPTION",
                f"{target_schema}.{target_table} is range-partitioned on {partition_columns} from a "
                f"{source_strategy} source strategy. Only a DEFAULT partition is created: real "
                "partition bounds come from the source interval layout, which is not an input here, "
                "and inventing bounds would put rows in the wrong partition",
                rule_id=None,
            )
        else:
            diags.add(
                "PARTITION_STRATEGY_NOT_IMPLEMENTED",
                "BLOCK",
                f"{target_schema}.{target_table} declares partition strategy `{strategy}`, which this "
                "compiler does not implement; only range partitioning on declared columns is built",
            )
    statements.insert(0, ("create-table", create))

    # A tablespace is set with ALTER, because PostgreSQL does not accept
    # TABLESPACE inside CREATE TABLE for a partitioned table and sqlglot parses
    # the ALTER form cleanly.
    if physical.get("tablespace"):
        tablespace = str(physical["tablespace"])
        statements.append(
            (
                "tablespace",
                f"ALTER TABLE {pg_ident(target_schema)}.{pg_ident(target_table)} "
                f"SET TABLESPACE {pg_ident(tablespace)}",
            )
        )
        diags.add(
            "TABLESPACE_APPLIED",
            "EDGE",
            f"{target_schema}.{target_table} is placed in tablespace `{tablespace}`",
        )

    constraint_policy = physical.get("constraint") or {}
    rendered: List[Dict[str, Any]] = []
    for name, sql in statements:
        text, glot = render_pg(sql)
        if text is None:
            diags.add(
                "DDL_RENDER_FAILED",
                "BLOCK",
                f"{name} for {target_schema}.{target_table} did not render: {glot.get('error')}",
                sql=sql,
            )
            continue
        rendered.append({"name": name, "sql": text, "sqlglot": glot})

    ddl_block = ""
    if constraint_policy:
        # `constraint` in targetPhysical describes the table's constraint
        # behaviour, not a table option, so it is reported as a constraint
        # instruction rather than silently dropped.
        deferrable = bool(constraint_policy.get("deferrable"))
        initially_deferred = bool(constraint_policy.get("initiallyDeferred"))
        diags.add(
            "CONSTRAINT_DEFERRABILITY",
            "EDGE",
            f"{target_schema}.{target_table} declares deferrable={deferrable} "
            f"initiallyDeferred={initially_deferred}. PostgreSQL applies this per constraint, so it is "
            "carried as an attribute to apply to each FOREIGN KEY the runtime discovers from the "
            "source catalog; it is not a table-level option",
        )

    if needs_catalog:
        diags.add(
            "DDL_NEEDS_CATALOG",
            "ASSUMPTION",
            f"{target_schema}.{target_table} has columns whose type could not be read because no source "
            "catalog was supplied; those columns default to text. Supply --catalog so the DDL matches "
            "the source",
        )

    return {
        "schema": target_schema,
        "table": target_table,
        "relation": pg_ident(target_schema) + "." + pg_ident(target_table),
        "columns": [column.as_dict() for column in job.columns],
        "primary_key": list(job.primary_keys),
        "statements": rendered,
        "ddl": ddl_block,
        "partition": partition,
        "tablespace": physical.get("tablespace"),
        "deferrable": bool((constraint_policy or {}).get("deferrable")),
        "initially_deferred": bool((constraint_policy or {}).get("initiallyDeferred")),
    }


# ---------------------------------------------------------------------------
# Apache SeaTunnel job configuration
# ---------------------------------------------------------------------------
#
# This is the artifact that is actually sent to SeaTunnel. Two jobs per target
# relation: a DDL job that creates the table, and a data job that loads it.
#
# HOCON is emitted directly rather than through a template engine so the
# generated file is inspectable, diffable and re-parseable. The same writer emits
# every file, so there is one place where a quoting bug could live.

def hocon_value(value: Any) -> str:
    """Render a Python value as a HOCON value."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if "\n" in text:
        # Triple quotes keep a multi-line SQL body readable and let the reader
        # take it verbatim.
        return '"""\n' + text.rstrip() + '\n"""'
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def hocon_block(key: str, entries: Sequence[Any], indent: str = "") -> str:
    """Render a HOCON block: ``key { ... }``.

    ``indent`` is the indentation of the block's own header line, and its
    contents sit one level deeper. Getting this wrong produces a file that
    parses and reads as correct but nests a plugin's settings at the wrong depth,
    which is exactly the sort of defect that only appears once SeaTunnel loads
    it.

    An entry value that is a list of ``(name, value)`` pairs is a nested block;
    a list of plain values is a HOCON array. SeaTunnel expects ``Jdbc { ... }``,
    and rendering it as an array configures nothing.
    """
    lines = [f"{indent}{key} {{"]
    inner = indent + "  "

    for name, value in entries:
        if isinstance(value, list) and value and all(
            isinstance(item, tuple) and len(item) == 2 for item in value
        ):
            lines.append(hocon_block(str(name), value, indent=inner))
            continue
        if isinstance(value, dict):
            lines.append(hocon_block(str(name), list(value.items()), indent=inner))
            continue
        if isinstance(value, list):
            if not value:
                lines.append(f"{inner}{name} = []")
            else:
                lines.append(f"{inner}{name} = [")
                for item in value:
                    lines.append(f"{inner}  {hocon_value(item)},")
                lines.append(f"{inner}]")
            continue
        lines.append(f"{inner}{name} = {hocon_value(value)}")

    lines.append(f"{indent}}}")
    return "\n".join(lines)


HEADER = "# " + "=" * 74


def _banner(job: CompiledJob, role: str) -> List[str]:
    plan = job.plan
    bar = "=" * 74
    lines = [
        bar,
        f"Generated by {TOOL_NAME} {TOOL_VERSION} from the approved migration specification.",
        f"role           : {role}",
        f"job_id         : {job.job_id}",
        f"job kind       : {plan.kind} ({plan.label})",
        f"source         : {', '.join(s['relation'] for s in job.sources)}",
        f"target         : {job.target['schema']}.{job.target['table']}",
        "dialect        : cTunnel reads the source, PostgreSQL receives the result",
    ]
    if job.primary_keys:
        lines.append(f"primary key     : {', '.join(job.primary_keys)}")
    if job.assumptions:
        lines.append("accepted assumptions:")
        for assumption in job.assumptions[:12]:
            lines.append(f"  - {assumption}")
        if len(job.assumptions) > 12:
            lines.append(f"  ... and {len(job.assumptions) - 12} more; they are printed after the file is written")
    return lines


def ddl_statements(job: CompiledJob) -> List[Dict[str, str]]:
    """The DDL commands that create one target relation, in execution order.

    These are the same statements the sink would have inferred from a zero-row
    read, written out instead of implied. Emitting them matters because the
    inferred form is silent: a target table created with the wrong shape produces
    no error at creation, only a load that writes the wrong columns. Written out,
    the CREATE TABLE is reviewable on its own, and the column list in it can be
    compared with the column list the projection produces.
    """
    statements = job.ddl.get("statements") or []
    return [
        {"name": str(entry.get("name")), "sql": str(entry.get("sql"))}
        for entry in statements
        if entry.get("sql")
    ]


def dml_statement(job: CompiledJob, source_sql: str) -> str:
    """The DML command that writes one target relation.

    An ``INSERT`` naming the target columns in compiled order and reading the same
    query the sink is fed, which already emits those columns in exactly that
    order.

    ``source_sql`` is passed in rather than read from ``job.query`` because on a
    CDC run the two differ: the transform has been rewritten onto the change
    stream, so ``job.query`` still names the Oracle base relation and writing it
    here would describe a read the job does not perform.

    It names an Oracle relation on the right of the SELECT and a PostgreSQL
    relation on the left, so it is not a statement either engine can run on its
    own -- it is the write the SeaTunnel job performs, written down. That is the
    point: the sink block states *how* the rows move, this states *what* the row
    is, and a reviewer can check the two against each other.

    A change-aware job writes with an upsert instead, because that is what the
    sink does; emitting a plain INSERT here would describe a write the job does
    not perform.
    """
    target = pg_ident(job.target["schema"]) + "." + pg_ident(job.target["table"])
    columns = ", ".join(qident(column.name) for column in job.columns)
    query = source_sql.strip().rstrip(";")

    if job.upsert and job.primary_keys:
        conflict = ", ".join(qident(name) for name in job.primary_keys)
        updatable = [column.name for column in job.columns if column.name not in set(job.primary_keys)]
        if updatable:
            assignments = ", ".join(
                f"{qident(name)} = EXCLUDED.{qident(name)}" for name in updatable
            )
            return (
                f"INSERT INTO {target} ({columns})\n{query}\n"
                f"ON CONFLICT ({conflict}) DO UPDATE SET {assignments}"
            )

    return f"INSERT INTO {target} ({columns})\n{query}"


def build_seatunnel_ddl_job(job: CompiledJob, indent: str = "  ") -> str:
    """The SeaTunnel job body that creates the target table.

    Carries three things: the DDL commands, the source query that fixes the column
    list, and the ``schema_save_mode`` that tells SeaTunnel what to do about the
    table. A JDBC source returning zero rows carries the exact column list, and
    ``schema_save_mode = RECREATE_SCHEMA`` makes SeaTunnel create the table to
    match. Recreate rather than ignore is deliberate: the specification's
    ``rerun: resume`` means the run may be repeated, and create-if-absent would
    leave a half-created table from a failed attempt in place.

    Returned as a body rather than a whole file because every job goes into one
    submitted config, under SeaTunnel's ``job { <name> { ... } }`` form.
    """
    source_relation = job.sources[0]["relation"]
    shape = ", ".join(f"{qident(column.name)} AS {qident(column.name)}" for column in job.columns)
    shape_query = f"SELECT {shape}\nFROM {source_relation} {flashback_clause()}\nWHERE 1 = 0"

    lines: List[str] = []
    lines.append(
        hocon_block(
            "env",
            [
                ("parallelism", 1),
                ("job.mode", "BATCH"),
                ("job.name", seatunnel_job_name(job, "ddl")),
                ("checkpoint.interval", 0),
            ],
            indent=indent,
        )
    )
    lines.append("")
    lines.append(
        hocon_block(
            "ddl",
            _ddl_block_entries(job),
            indent=indent,
        )
    )
    lines.append("")
    lines.append(
        hocon_block(
            "source",
            [
                (
                    "Jdbc",
                    [
                        ("url", "${SOURCE_JDBC_URL}"),
                        ("driver", "oracle.jdbc.OracleDriver"),
                        ("user", "${SOURCE_DB_USER}"),
                        ("password", "${SOURCE_DB_PASSWORD}"),
                        ("query", shape_query.replace(":run_scn", "${run_scn}")),
                        ("fetch_size", 1),
                        ("result_table_name", seatunnel_result_table(job, "ddl_shape")),
                        (
                            "properties",
                            [
                                ("oracle.jdbc.TIMESTAMPAsDate", "false"),
                                ("oracle.jdbc.defaultNChar", "true"),
                            ],
                        ),
                    ],
                )
            ],
            indent=indent,
        )
    )
    lines.append("")
    lines.append(hocon_block("sink", [("Jdbc", _ddl_sink_entries(job))], indent=indent))
    return "\n".join(lines)


def _ddl_block_entries(job: CompiledJob) -> List[Tuple[str, Any]]:
    """The `ddl` block: the commands and the mode, stated rather than implied."""
    statements = ddl_statements(job)
    entries: List[Tuple[str, Any]] = [
        ("schema_save_mode", "RECREATE_SCHEMA"),
        ("target", job.ddl.get("relation") or pg_ident(job.target["schema"]) + "." + pg_ident(job.target["table"])),
        ("columns", [column.name for column in job.columns]),
        ("command_count", len(statements)),
    ]
    for statement in statements:
        entries.append((f"{statement['name']}.sql", statement["sql"]))
    if job.ddl.get("partition"):
        entries.append(("partition", job.ddl["partition"]))
    if job.ddl.get("tablespace"):
        entries.append(("tablespace", job.ddl["tablespace"]))
    if job.ddl.get("deferrable"):
        entries.append(("deferrable", True))
        entries.append(("initially_deferred", bool(job.ddl.get("initially_deferred"))))
    return entries


def _dml_block_entries(job: CompiledJob, source_sql: str, sink_mode: str) -> List[Tuple[str, Any]]:
    """The `dml` block: the write command and the mode it is performed under."""
    entries: List[Tuple[str, Any]] = [
        ("schema_save_mode", sink_mode),
        ("target", pg_ident(job.target["schema"]) + "." + pg_ident(job.target["table"])),
        ("columns", [column.name for column in job.columns]),
        ("statement", dml_statement(job, source_sql)),
    ]
    if job.upsert:
        entries.append(("write_mode", "upsert"))
        entries.append(("conflict_target", list(job.primary_keys)))
    else:
        entries.append(("write_mode", "append"))
    return entries


def _ddl_sink_entries(job: CompiledJob) -> List[Tuple[str, Any]]:
    entries: List[Tuple[str, Any]] = [
        ("source_table_name", seatunnel_result_table(job, "ddl_shape")),
        ("url", "${TARGET_JDBC_URL}"),
        ("driver", "org.postgresql.Driver"),
        ("user", "${TARGET_DB_USER}"),
        ("password", "${TARGET_DB_PASSWORD}"),
        ("table_schema", job.target["schema"]),
        ("table_name", job.target["table"]),
        ("generate_sink_sql", True),
        # RECREATE_SCHEMA is destructive by design, so it is gated by the
        # governance gate the runner applies before any *_ddl job is submitted.
        ("schema_save_mode", "RECREATE_SCHEMA"),
        ("batch_size", 1),
        ("is_legacy", False),
    ]
    return entries


def build_seatunnel_data_job(
    job: CompiledJob,
    run: Dict[str, Any],
    capture: Dict[str, Any],
    indent: str = "  ",
) -> str:
    """The SeaTunnel job body that loads the target table."""
    query = (job.query or "").rstrip()
    if query.endswith(";"):
        query = query[:-1]

    source_table = seatunnel_result_table(job, "src")
    sink_table = seatunnel_result_table(job, "sink")

    # A set-level operation -- a dedupe, a denormalise, a pivot -- has no
    # per-change-stream form: it needs the whole relation. Running one on a CDC
    # stream would aggregate one changed row at a time and produce nonsense, so
    # such a job is extracted as a snapshot even in a CDC run. The alternative is
    # to fail the job, and failing here would leave the table unmigrated.
    run_is_cdc = str(run.get("acquisition")) == "cdc"
    driving = f"{job.sources[0]['relation']} AS OF SCN ${{run_scn}}"

    # A query that does not read its own driving relation cannot be rewritten onto
    # a change stream, so it is extracted as a snapshot instead of being submitted
    # against a CDC source that would feed it the wrong rows.
    is_cdc = run_is_cdc and not job.snapshot_only and driving in (
        query.replace(SCN_BIND, "${run_scn}").replace(WATERMARK_BIND, "${run_watermark}")
    )

    lines: List[str] = []
    if run_is_cdc and not is_cdc:
        operations = ", ".join(str(op.get("operation")) for op in job.operations) or "a set-level operation"
        lines.append("# SNAPSHOT-ONLY JOB: run.acquisition is cdc but this job cannot run on a change")
        lines.append(f"# stream ({operations}), so it reads the whole relation with the compiled query.")
        lines.append("# Re-extract it if its source changes during the migration.")
    lines.append(
        hocon_block(
            "env",
            [
                ("parallelism", 4),
                ("job.mode", "BATCH"),
                ("job.name", seatunnel_job_name(job, "data")),
                ("checkpoint.interval", 10000),
                (
                    "checkpoint.mode",
                    "EXACTLY_ONCE" if str(run.get("rerun")) == "resume" else "AT_LEAST_ONCE",
                ),
            ],
            indent=indent,
        )
    )
    lines.append("")

    # The compiled query, with binds expressed as SeaTunnel variables.
    compiled = query.replace(SCN_BIND, "${run_scn}").replace(WATERMARK_BIND, "${run_watermark}")

    if is_cdc:
        # A CDC source emits one changed row at a time for a base relation, so the
        # projection has to read that stream instead of the base table. Swapping
        # the driving relation for the CDC result table is the only edit made,
        # and the result is re-parsed by the artifact validation stage.
        driving_reads_stream = driving in compiled
        driving = f"{job.sources[0]['relation']} AS OF SCN ${{run_scn}}"
        driving_reads_stream = driving in compiled
        if not driving_reads_stream:
            lines.append(
                "# WARNING: the compiled query does not read the driving relation verbatim, so the CDC"
            )
            lines.append(
                "# projection was not rewritten onto the change stream. Check this before running it."
            )
        compiled = compiled.replace(driving, source_table)
        if "AS OF SCN" in compiled:
            # The driving relation now reads the change stream, which has no
            # consistent snapshot to pin. Any flashback clause left belongs to a
            # joined lookup dimension, which is read from the source at the pinned
            # SCN -- the right thing for slow-changing reference data, but only if
            # it is stated rather than left to be discovered in a diff.
            lines.append(
                "# NOTE: the driving relation reads the change stream. A remaining AS OF SCN clause"
            )
            lines.append(
                "# belongs to a joined lookup dimension, which is read from the source at the pinned SCN."
            )
        lines.append(
            hocon_block(
                "source",
                [
                    (
                        "CDC",
                        [
                            ("base-url", "${SOURCE_JDBC_URL}"),
                            ("username", "${SOURCE_DB_USER}"),
                            ("password", "${SOURCE_DB_PASSWORD}"),
                            ("server-id", "${CDC_SERVER_ID:-5400}"),
                            ("server-time-zone", "UTC"),
                            ("schema-name", job.sources[0]["schema"]),
                            ("table-name", f"{job.sources[0]['schema']}.\\*"),
                            ("startup.mode", "initial"),
                            ("scan.startup.mode", "initial"),
                            ("scan.snapshot.fetch.size", 512),
                            ("result_table_name", source_table),
                            ("deserializer", "Oracle"),
                        ],
                    )
                ],
                indent=indent,
            )
        )
    else:
        lines.append(
            hocon_block(
                "source",
                [
                    (
                        "Jdbc",
                        [
                            ("url", "${SOURCE_JDBC_URL}"),
                            ("driver", "oracle.jdbc.OracleDriver"),
                            ("user", "${SOURCE_DB_USER}"),
                            ("password", "${SOURCE_DB_PASSWORD}"),
                            ("query", compiled),
                            ("fetch_size", 2048),
                            ("result_table_name", source_table),
                            ("properties", [("oracle.jdbc.TIMESTAMPAsDate", "false")]),
                        ],
                    )
                ],
                indent=indent,
            )
        )
    lines.append("")

    # The write, stated rather than left to the sink's inference. It reads the
    # same query the transform is fed, so on a CDC run it names the change stream
    # rather than the base relation the job no longer reads.
    lines.append(hocon_block("dml", _dml_block_entries(job, compiled, "IGNORE"), indent=indent))
    lines.append("")

    lines.append(
        hocon_block(
            "transform",
            [
                (
                    "SQL",
                    [
                        ("source_table_name", source_table),
                        ("result_table_name", sink_table),
                        ("query", compiled if is_cdc else f"SELECT * FROM {source_table}"),
                        ("output_mode", "APPEND"),
                        ("print_total", False),
                    ],
                )
            ],
            indent=indent,
        )
    )
    lines.append("")
    lines.append(hocon_block("sink", [("Jdbc", _data_sink_entries(job, sink_table))], indent=indent))

    for sentinel in job.quarantine_filters:
        lines.append("")
        lines.append(
            hocon_block(
                "sink",
                [
                    (
                        "Jdbc",
                        [
                            ("source_table_name", sink_table),
                            ("url", "${QUARANTINE_JDBC_URL:-${TARGET_JDBC_URL}}"),
                            ("driver", "org.postgresql.Driver"),
                            ("user", "${TARGET_DB_USER}"),
                            ("password", "${TARGET_DB_PASSWORD}"),
                            ("table_schema", job.target["schema"]),
                            ("table_name", f"{job.target['table']}_quarantine"),
                            ("generate_sink_sql", True),
                            ("schema_save_mode", "CREATE_SCHEMA_WHEN_NOT_EXISTS"),
                            ("filter_sql", f"{qident(sentinel['column'])} IS NOT NULL"),
                            ("batch_size", 1000),
                            ("is_legacy", False),
                        ],
                    )
                ],
                indent=indent,
            )
        )

    return "\n".join(lines)


def build_seatunnel_conf(
    jobs: List[CompiledJob],
    spec: Dict[str, Any],
    spec_path: Path,
) -> str:
    """One SeaTunnel config holding every job, in submission order.

    SeaTunnel's multi-job form is ``job { <jobName> { env {} source {} transform {} sink {} } }``.
    A single submitted file is what makes the artifact reviewable as a whole: one
    diff shows every change to every target relation, and one submission covers the
    whole migration. Per-table files make that impossible to see and easy to
    half-apply.
    """
    run = spec.get("run") or {}
    capture = run.get("capture") or {}

    ready = [job for job in jobs if job.query is not None]
    blocked = [job for job in jobs if job.query is None]

    lines: List[str] = [
        HEADER,
        f"# Generated by {TOOL_NAME} {TOOL_VERSION} from {spec_path}",
        "# This file is generated. Edit the specification and recompile; do not edit this file.",
        HEADER,
        "#",
        f"# jobs          : {len(ready)} ({2 * len(ready)} SeaTunnel jobs: ddl + data for each)",
        f"# target engine : {(spec.get('engines') or {}).get('target', {}).get('engine')}",
        f"# source engine : {(spec.get('engines') or {}).get('source', {}).get('engine')}",
        f"# acquisition   : {run.get('acquisition')}",
        "#",
        "# Every target PostgreSQL table is created by its <target>_ddl job and loaded by its",
        "# <target>_data job. Both are in this file and both are submitted from it.",
        "#",
        "#   <target>_ddl    ddl {}      the CREATE TABLE / PARTITION / TABLESPACE commands,",
        "#                             the target column list, and schema_save_mode",
        "#                   source {}   a zero-row read of the Oracle relation, to fix the shape",
        "#                   sink {}     RECREATE_SCHEMA, which is destructive by design",
        "#",
        "#   <target>_data   source {}     the source query -- the compiled projection",
        "#                   dml {}        the INSERT the sink performs, and its write_mode",
        "#                   transform {}  the target query, which becomes the target row",
        "#                   sink {}       IGNORE, because the ddl job already made the table",
        "#",
        "# A dml statement names a PostgreSQL relation on the left and an Oracle one on the",
        "# right, so it is not runnable on either engine alone: it is the write stated down, so",
        "# it can be read against the sink block and the target query and checked.",
        "#",
        "# Submit one job:",
        "#   seatunnel.sh --config " + SEATUNNEL_CONF_NAME + " --name <jobName>",
        "# or submit the whole file; a *_ddl job must pass before its *_data job.",
        "#",
        "# Required variables:",
    ]
    for variable in SEATUNNEL_VARIABLES:
        lines.append(f"#   {variable}")
    if SCN_BIND.lstrip(":") not in [v.split("=")[0] for v in []]:
        lines.append(f"#   run_scn            resolved from {SCN_BIND_PATH}")
    if run.get("acquisition") == "query-incremental":
        lines.append(f"#   run_watermark      resolved from {WATERMARK_BIND_PATH}")
    if blocked:
        lines.append("#")
        lines.append(
            f"# NOT EMITTED -- {len(blocked)} job(s) are blocked and have no query: "
            + ", ".join(job.job_id for job in blocked)
        )
        lines.append("# A blocked job is never emitted as a partial guess.")
        # Which job failed for which reason. Without this the header names the
        # absence and withholds its cause, and the cause is the only part that
        # tells anyone what to change.
        for job in blocked:
            reasons = [d for d in job.diagnostics.items if d.severity == "BLOCK"]
            for reason in reasons:
                lines.append(f"#   {job.job_id}: {reason.code} -- {reason.message}")
    lines.append(HEADER)
    lines.append("")
    lines.append("job {")

    for job in ready:
        for role, title, body in (
            ("ddl", f"create {job.target['schema']}.{job.target['table']}",
             build_seatunnel_ddl_job(job)),
            ("data", f"load {job.target['schema']}.{job.target['table']}",
             build_seatunnel_data_job(job, run, capture)),
        ):
            name = seatunnel_job_name(job, role)
            lines.append("")
            # extend() with a string would append its characters, so split it.
            lines.extend(indent_sql("\n".join(_banner(job, title)), "  # ").splitlines())
            lines.append(f"  {name} {{")
            lines.append(indent_sql(body, "  "))
            lines.append("  }")

    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def seatunnel_job_name(job: CompiledJob, role: str) -> str:
    """SeaTunnel job name: `<schema>_<table>_<role>`, safe as a HOCON key."""
    return f"{sanitize_alias(job.target['schema'])}_{sanitize_alias(job.target['table'])}_{role}"


def seatunnel_result_table(job: CompiledJob, prefix: str) -> str:
    """SeaTunnel internal result-table name, unique per job within the file."""
    base = normalized_relation_name(job.target["schema"], job.target["table"])
    return f"{prefix}_{base}"


def _data_sink_entries(job: CompiledJob, sink_table: str) -> List[Tuple[str, Any]]:
    entries: List[Tuple[str, Any]] = [
        ("source_table_name", sink_table),
        ("url", "${TARGET_JDBC_URL}"),
        ("driver", "org.postgresql.Driver"),
        ("user", "${TARGET_DB_USER}"),
        ("password", "${TARGET_DB_PASSWORD}"),
        ("table_schema", job.target["schema"]),
        ("table_name", job.target["table"]),
        ("generate_sink_sql", False),
        ("schema_save_mode", "IGNORE"),
        ("batch_size", 1000),
        ("is_legacy", False),
    ]
    if job.primary_keys and job.upsert:
        entries.append(("write_mode", "upsert"))
        entries.append(("support_upsert", True))
        entries.append(("primary_keys", list(job.primary_keys)))
    elif job.upsert:
        entries.append(("support_upsert", True))
    return entries


# -- minimal HOCON reader ----------------------------------------------------
#
# The validation stage re-reads the generated .conf and re-parses the SQL out of
# it. That is only meaningful if the reader is honest, so it is a real parser
# for the subset of HOCON the writer emits: nested blocks, `key = value`,
# `key: value`, arrays, triple-quoted and single-quoted strings, and comments.

def parse_hocon(text: str) -> Dict[str, Any]:
    """Parse the HOCON subset the writer emits. Returns a nested mapping.

    Every block the writer emits has an explicit closing brace, so nesting is
    tracked by braces rather than by indentation. That distinction matters: an
    indentation-based reader silently discards a nested plugin block when its
    closing brace sits at column zero, which is exactly how SeaTunnel files are
    written -- and the result is a file that looks right and configures nothing.
    """
    result: Dict[str, Any] = {}
    stack: List[Dict[str, Any]] = [result]
    lines = text.splitlines()
    index = 0

    def current() -> Dict[str, Any]:
        return stack[-1]

    while index < len(lines):
        raw = lines[index]
        index += 1

        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue

        if stripped.startswith("}"):
            if len(stack) > 1:
                stack.pop()
            continue

        # `key { ... }` or `key = { ... }`
        if stripped.endswith("{"):
            name = stripped[:-1].strip()
            if name.endswith("="):
                name = name[:-1].strip()
            if "=" not in name and ":" in name:
                name = name.split(":", 1)[0].strip()
            block: Dict[str, Any] = {}
            if name:
                current()[name] = block
            stack.append(block)
            continue

        # `key = value` or `key: value`
        separator: Optional[Tuple[str, int]] = None
        for candidate in ("=", ":"):
            if candidate in stripped:
                position = stripped.index(candidate)
                if separator is None or position < separator[1]:
                    separator = (candidate, position)
        if separator is None:
            continue

        key = stripped[: separator[1]].strip()
        value = stripped[separator[1] + 1 :].strip()
        if not key:
            continue

        # A triple-quoted SQL body spans lines, so it has to be collected before
        # the value is interpreted. A bracket count is not enough here: a SQL
        # body rarely contains a bracket, so an unterminated `"""` would look
        # like a complete scalar and the body would be read as further keys.
        if value.startswith('"""') and value.count('"""') < 2:
            collected = [value]
            while index < len(lines) and not _closes_triple_quote("\n".join(collected)):
                collected.append(lines[index])
                index += 1
            value = "\n".join(collected)

        # A value that opens a bracket but never closes it spans lines.
        if _bracket_deficit(value) > 0:
            collected = [value]
            while index < len(lines) and _bracket_deficit("\n".join(collected)) > 0:
                collected.append(lines[index])
                index += 1
            value = "\n".join(collected)

        if not value:
            block = {}
            current()[key] = block
            stack.append(block)
            continue

        current()[key] = _hocon_scalar(value)

    return result


def _closes_triple_quote(text: str) -> bool:
    """True once a triple-quoted HOCON body has seen its closing delimiter."""
    body = text[3:]
    return body.count(TRIPLE_QUOTE) >= 1


def _bracket_deficit(text: str) -> int:
    """How many brackets are still open, ignoring anything inside a string."""
    depth = 0
    in_string = False
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                in_string = False
        elif char in "\"'":
            in_string = True
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        index += 1
    return max(depth, 0)


def _hocon_scalar(value: str) -> Any:
    text = value.strip()

    if text.startswith('"""'):
        body = text[3:]
        if body.endswith('"""'):
            return body[:-3].strip("\n")
        return body.strip("\n")

    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        items: List[Any] = []
        depth = 0
        piece = ""
        in_string = False
        quote = ""
        for char in inner:
            if in_string:
                piece += char
                if char == quote:
                    in_string = False
                continue
            if char in "\"'":
                in_string = True
                quote = char
                piece += char
                continue
            if char == "," and depth == 0:
                if piece.strip():
                    items.append(_hocon_atom(piece.strip()))
                piece = ""
                continue
            if char in "[{":
                depth += 1
            elif char in "]}":
                depth -= 1
            piece += char
        if piece.strip():
            items.append(_hocon_atom(piece.strip()))
        return items

    if text == "[]":
        return []

    return _hocon_atom(text)


def _hocon_atom(value: str) -> Any:
    if not value:
        return ""
    if value in {"true", "false"}:
        return value == "true"
    if value == "null":
        return None
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    try:
        if re.fullmatch(r"-?\d+", value):
            return int(value)
        if re.fullmatch(r"-?\d+\.\d+", value):
            return float(value)
    except ValueError:  # pragma: no cover - defensive
        pass
    return value


# ---------------------------------------------------------------------------
# Oracle -> DuckDB translation
# ---------------------------------------------------------------------------
#
# A validation check runs in DuckDB, so the source side of every comparison has
# to be executable there too. SQLGlot's duckdb generator carries a function name
# it does not recognise through verbatim, which means `REGEXP_SUBSTR` and
# `STANDARD_HASH` parse as DuckDB and then fail at *bind* time, part way through
# a run, against real data. A plan that only parses is not a plan.
#
# Translation is therefore never a bare dialect switch. It is two explicit steps:
#
#   1. rewrite the Oracle-only nodes DuckDB cannot bind, on the AST;
#   2. scan the rewritten tree for any function name DuckDB has never heard of.
#
# Step 2 is what makes step 1 trustworthy. A rewrite table that is merely believed
# complete produces a plan that parses; this produces one that binds.

#: Oracle names DuckDB has no scalar function for. Verified against DuckDB, not
#: assumed. Anything still present after the rewrite is a BLOCK.
DUCKDB_UNBINDABLE_ORACLE_FUNCTIONS = {
    "ADD_MONTHS",
    "CONNECT_BY_ROOT",
    "CURRENT_DATE_BEFORE",
    "DECODE",
    "MODEL",
    "NEXT_DAY",
    "NVL",
    "REGEXP_INSTR",
    "REGEXP_SUBSTR",
    "SELF_TO_CURRENCY_CONV",
    "STANDARD_HASH",
    "SYSDATE",
    "SYSTIMESTAMP",
    "TO_BINARY_DOUBLE",
    "TO_CHAR",
    "TO_CLOB",
    "TO_DATE",
    "TO_DSINTERVAL",
    "TO_MULTI_BYTE",
    "TO_NCHAR",
    "TO_NUMBER",
    "TO_TIMESTAMP",
    "TRANSLATE_USING",
    "XMLQUERY",
    "XMLTABLE",
}

#: STANDARD_HASH algorithms DuckDB can express. Every one of these was executed
#: against DuckDB before being listed; an algorithm that is not here cannot be
#: translated, so a `mask strategy=hash` naming it blocks rather than ships a
#: plan that fails when the mask is first computed.
DUCKDB_HASH_BY_ORACLE_ALGORITHM = {
    "MD5": "MD5",
    "SHA1": "SHA1",
    "SHA-1": "SHA1",
    "SHA256": "SHA256",
    "SHA-256": "SHA256",
}


def _literal_int(node: Any) -> Optional[int]:
    """The integer a SQLGlot literal holds, or None when it is not one."""
    if node is None:
        return None
    value = getattr(node, "this", node)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _literal_text(node: Any) -> Optional[str]:
    """The text a SQLGlot literal holds, or None when it is not one."""
    if node is None:
        return None
    value = getattr(node, "this", node)
    return value if isinstance(value, str) else None


def _strip_flashback(tree: Any) -> int:
    """Remove every ``AS OF SCN`` clause, returning how many were removed.

    The DuckDB source view reads a snapshot the runtime has already materialised,
    so the clause has no meaning there and no way to bind: `:run_scn` is Oracle
    syntax. Removing it is also what makes the SCN *one* decision -- the runtime
    exports the snapshot once, and every check reads that one export.
    """
    removed = 0
    for table in tree.find_all(exp.Table):
        if table.args.get("flashback") is not None:
            table.set("flashback", None)
            removed += 1
    return removed


def _rewind_binds(tree: Any) -> int:
    """Turn a named bind into a positional DuckDB parameter.

    ``:run_watermark`` is a SQLGlot ``Placeholder``. DuckDB has no named binds,
    but it does have positional parameters, so the bind becomes ``?`` and the job
    records that it must be supplied. Rewriting it to a literal instead would
    silently pin a migration to one watermark.
    """
    count = 0
    for node in tree.find_all(exp.Placeholder):
        node.set("this", "?")
        count += 1
    return count


def _duckdb_rewrite(node: Any, diags: Optional[Diagnostics], rule_id: Optional[str], where: str) -> Any:
    """Rewrite one Oracle-only node into its DuckDB equivalent, or report it.

    Only *total* rewrites are listed. A partial one -- a REGEXP_SUBSTR that wants
    the third match, a STANDARD_HASH over an algorithm DuckDB lacks -- is left
    alone and turned into a BLOCK, because the residual scan would catch it as a
    failure in DuckDB anyway and a clear message here is worth more than that.
    """
    if isinstance(node, exp.RegexpSubstr):
        position = _literal_int(node.args.get("position"))
        occurrence = _literal_int(node.args.get("occurrence"))
        subject = node.this
        pattern = node.args["expression"]

        # Oracle scans forward from `position` and returns the `occurrence`-th
        # match. DuckDB has no occurrence argument, but it has the two functions
        # that together express it exactly, with the same NULL-when-absent
        # behaviour: REGEXP_EXTRACT_ALL finds every match, LIST_EXTRACT picks one.
        #
        #   occurrence 0 (or absent)  ->  REGEXP_EXTRACT(s, p)      the whole match
        #   occurrence n >= 1         ->  LIST_EXTRACT(REGEXP_EXTRACT_ALL(s, p), n)
        #
        # A non-1 position is the one case left, because scanning from an offset is
        # not expressible as a plain function call.
        if position not in (None, 1):
            if diags is not None:
                diags.add(
                    "DUCKDB_FUNCTION_UNMAPPED",
                    "BLOCK",
                    f"{where} uses REGEXP_SUBSTR at position {position}. DuckDB's regexp functions "
                    "always scan from the start, and expressing an offset scan would mean rewriting the "
                    "rule rather than translating it",
                    rule_id=rule_id,
                )
            return node

        if occurrence in (None, 0):
            return exp.Anonymous(
                this="REGEXP_EXTRACT", expressions=[subject, pattern]
            )

        if occurrence is not None and occurrence >= 1:
            return exp.Anonymous(
                this="LIST_EXTRACT",
                expressions=[
                    exp.Anonymous(
                        this="REGEXP_EXTRACT_ALL", expressions=[subject, pattern]
                    ),
                    exp.Literal.number(occurrence),
                ],
            )

        if diags is not None:
            diags.add(
                "DUCKDB_FUNCTION_UNMAPPED",
                "BLOCK",
                f"{where} uses REGEXP_SUBSTR with occurrence {occurrence}, which is neither the "
                "whole match nor a positive match number",
                rule_id=rule_id,
            )
        return node

    if isinstance(node, exp.StandardHash):
        algorithm = (_literal_text(node.args.get("expression")) or "").upper()
        target = DUCKDB_HASH_BY_ORACLE_ALGORITHM.get(algorithm)
        if target:
            return exp.Anonymous(this=target, expressions=[node.this])
        if diags is not None:
            diags.add(
                "DUCKDB_HASH_UNMAPPED",
                "BLOCK",
                f"{where} uses STANDARD_HASH with algorithm "
                f"{algorithm or '<none>'}; DuckDB can express {sorted(set(DUCKDB_HASH_BY_ORACLE_ALGORITHM.values()))} "
                "only, so a validation plan over this query could not be executed",
                rule_id=rule_id,
                algorithm=algorithm,
            )
        return node

    if isinstance(node, exp.ToChar):
        # DuckDB has no TO_CHAR, and SQLGlot degrades it to CAST(x AS TEXT) --
        # silently dropping the format mask. Oracle and DuckDB format masks are
        # not the same language either, so a mask cannot be translated safely
        # even by hand. A masked TO_CHAR therefore blocks; an unmasked one is
        # already just a cast, which is reported so the loss is on the record.
        if node.args.get("format") is not None:
            if diags is not None:
                diags.add(
                    "DUCKDB_FORMAT_UNMAPPED",
                    "BLOCK",
                    f"{where} uses TO_CHAR with a format mask. DuckDB has no TO_CHAR and its "
                    "STRFTIME masks are not Oracle's, so generating one would silently drop the "
                    "formatting; translate the rule to STRFTIME explicitly, or drop the mask",
                    rule_id=rule_id,
                )
            return node
        if diags is not None:
            diags.add(
                "DUCKDB_TOCHAR_UNMASKED",
                "EDGE",
                f"{where} uses TO_CHAR with no format mask, which is a plain cast on both engines. "
                "It is generated as one, and the default formatting is whatever the target chooses",
                rule_id=rule_id,
            )
        return node

    return node


def _duckdb_unbindable(tree: Any) -> List[str]:
    """Oracle function names still present in a translated tree.

    Read off the AST rather than the text, so a name inside a string literal -- a
    regexp pattern, a table comment -- cannot raise a false alarm.

    A function SQLGlot has no node for survives as ``exp.Anonymous``, whose
    ``sql_name()`` is the literal string ``ANONYMOUS``. Reading ``name`` instead
    is what makes the scan see those at all: without it every unknown function
    looks anonymous and the scan passes whatever it is handed.
    """
    found: set = set()
    for node in tree.walk():
        if not isinstance(node, exp.Func):
            continue
        if isinstance(node, exp.Anonymous):
            name = str(node.name or "").upper()
        else:
            try:
                name = (node.sql_name() or "").upper()
            except Exception:  # noqa: BLE001 - a node without a name cannot be a name
                continue
        if name in DUCKDB_UNBINDABLE_ORACLE_FUNCTIONS:
            found.add(name)
    return sorted(found)


def render_duckdb(
    raw_sql: str,
    diags: Optional[Diagnostics] = None,
    rule_id: Optional[str] = None,
    where: str = "the compiled query",
) -> Tuple[Optional[str], Dict[str, str]]:
    """Translate compiled cTunnel SQL into DuckDB SQL, or say why it cannot be.

    Returns ``(sql, report)``. ``sql`` is None only when the statement does not
    parse in the source dialect at all; an unbindable Oracle function leaves the
    SQL produced and is recorded in ``report["residual"]``, because the text is
    still useful for a human reading the plan and the diagnostic is what stops it
    shipping.
    """
    report: Dict[str, str] = {
        "dialect": "duckdbstrict",
        "parse": "PASS",
        "stable": "PASS",
        "residual": "",
        "flashback_removed": "0",
        "binds_rewound": "0",
    }

    if not SQLGLOT_AVAILABLE:
        report["parse"] = "SKIPPED_SQLGLOT_NOT_INSTALLED"
        report["stable"] = "SKIPPED_SQLGLOT_NOT_INSTALLED"
        return raw_sql, report

    parsed, parse_error = parse_ctunnel(raw_sql)
    if parsed is None:
        report["parse"] = "FAIL"
        report["error"] = parse_error or "unknown"
        report["stable"] = "FAIL"
        return None, report

    tree = parsed.copy()
    report["flashback_removed"] = str(_strip_flashback(tree))
    report["binds_rewound"] = str(_rewind_binds(tree))
    before = len(diags.blocking) if diags is not None else 0
    tree = tree.transform(lambda node: _duckdb_rewrite(node, diags, rule_id, where))
    # The rewrite can add a blocking finding without failing outright -- a node it
    # deliberately left alone, because generating it would be silently wrong. When
    # that happens nothing is generated: the text would parse, and would compare
    # the wrong thing, which is worse than not producing it.
    if diags is not None and len(diags.blocking) > before:
        report["blocked"] = "1"
        report["stable"] = "FAIL"
        report["error"] = "the statement contains a construct that cannot be translated for DuckDB"
        return None, report

    try:
        rendered = tree.sql(dialect="duckdbstrict", pretty=True)
    except UnsupportedError as exc:
        # The strict dialect raises rather than emitting a valid-but-wrong
        # statement. A construct DuckDB cannot express is a BLOCK, not a guess.
        report["parse"] = "FAIL"
        report["stable"] = "FAIL"
        report["unsupported"] = str(exc)
        if diags is not None:
            diags.add(
                "DUCKDB_CONSTRUCT_UNSUPPORTED",
                "BLOCK",
                f"{where} uses a construct DuckDB cannot express: {exc}. Generating it anyway would "
                "produce a statement that parses and compares the wrong thing, so it is not generated",
                rule_id=rule_id,
            )
        return None, report
    except Exception as exc:  # noqa: BLE001
        report["parse"] = "FAIL"
        report["stable"] = "FAIL"
        report["error"] = str(exc)
        return None, report

    # Round-trip in DuckDB's own dialect: the text that ships must survive a
    # second parse unchanged, exactly as the SeaTunnel artifact is re-read.
    try:
        reparsed = parse_one(rendered, read="duckdb")
        again = reparsed.sql(dialect="duckdb")
    except Exception as exc:  # noqa: BLE001
        report["parse"] = "FAIL"
        report["stable"] = "FAIL"
        report["error"] = str(exc)
        return rendered, report

    report["stable"] = "PASS" if again == reparsed.sql(dialect="duckdb") else "DRIFT"

    residual = _duckdb_unbindable(reparsed)
    if residual:
        report["residual"] = ", ".join(residual)
        if diags is not None:
            diags.add(
                "DUCKDB_ORACLE_FUNCTION_LEFT",
                "BLOCK",
                f"{where} still calls {residual} after translation to DuckDB; DuckDB cannot bind "
                "those, so the validation plan would fail when it ran rather than when it was compiled",
                rule_id=rule_id,
                functions=residual,
            )

    return rendered, report


# ---------------------------------------------------------------------------
# DuckDB validation jobs
# ---------------------------------------------------------------------------
#
# One YAML file holding every validation job, mirroring the SeaTunnel file: one
# artifact per engine, one job per target relation, one index naming the order.
#
# The recurring question each job answers is the same one, and it is not the same
# as the SeaTunnel job's. A projection cannot raise, cannot see a previous row
# image, and cannot undo a lossy step -- so what lands on the target has to be
# checked *against* something. Three relations are bound per job:
#
#   __RAW__<job>      the Oracle base relation at the pinned SCN, exported
#   __SOURCE__<job>    the compiled projection, which is what SeaTunnel read
#   __TARGET__<job>    the target relation as the load actually left it
#
# Comparing __SOURCE__ against __TARGET__ catches the sink, the transport and the
# write mode. Comparing __RAW__ against __SOURCE__ catches the recipes. Comparing
# __RAW__ against __TARGET__ catches what both got wrong.

@dataclass
class DuckDBJob:
    """One DuckDB validation job: one target relation and every check on it."""

    job_id: str
    kind: str
    target: Dict[str, str]
    target_view: str
    raw_relations: List[Dict[str, str]] = field(default_factory=list)
    source_view: str = ""
    quarantine_view: Optional[str] = None
    columns: List[Dict[str, Any]] = field(default_factory=list)
    primary_keys: List[str] = field(default_factory=list)
    projection: Optional[str] = None
    projection_glot: Dict[str, str] = field(default_factory=dict)
    checks: List[Dict[str, Any]] = field(default_factory=list)
    binds: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def check_count(self) -> int:
        return len(self.checks)


def duckdb_view_name(prefix: str, job: CompiledJob) -> str:
    """A run-time bound relation name: ``__SOURCE__shop_client`` and friends."""
    return f"{prefix}{normalized_relation_name(job.target['schema'], job.target['table'])}"


def make_check(
    check_id: str,
    source_spec: str,
    category: str,
    check_type: str,
    sql: Optional[str],
    expected: str,
    bindings: Optional[List[str]] = None,
    notes: Optional[str] = None,
    severity: str = "error",
    runtime_strategy: Optional[str] = None,
) -> Dict[str, Any]:
    """One validation rule, in the shape both files use.

    ``source_spec`` is what makes a rule auditable: it names the exact path in the
    specification the rule came from (``acceptance.checks[row-count-exact]``, a
    rule id, ``fidelityFloor[TZ_OFFSET]``), so a reviewer can go from a check back
    to the line that asked for it without guessing.

    The SQL is parsed in the DuckDB dialect as it is built. A parse failure is
    recorded rather than raised, so one bad statement is reported as one finding
    instead of aborting the whole compilation and hiding everything after it.
    """
    item: Dict[str, Any] = {
        "check_id": check_id,
        "source_spec": source_spec,
        "category": category,
        "type": check_type,
        "severity": severity,
        "expected": expected,
        "execution": "contract-only",
        "query_dialect": "duckdb" if sql else "none",
    }
    if sql:
        if SQLGLOT_AVAILABLE:
            try:
                parsed = parse_one(sql, read="duckdb")
                rendered = parsed.sql(dialect="duckdb", pretty=True)
                glot = {"dialect": "duckdb", "parse": "PASS"}
            except Exception as exc:  # noqa: BLE001
                rendered = sql
                glot = {"dialect": "duckdb", "parse": "FAIL", "error": str(exc)}
        else:
            rendered = sql
            glot = {"dialect": "duckdb", "parse": "SKIPPED_SQLGLOT_NOT_INSTALLED"}
        item["sql"] = rendered
        item["sqlglot"] = glot
    else:
        item["sql"] = None
        item["sqlglot"] = {"dialect": "duckdb", "parse": "NOT_APPLICABLE"}
    if runtime_strategy:
        item["runtime_strategy"] = runtime_strategy
    if bindings:
        item["bindings"] = bindings
    if notes:
        item["notes"] = notes
    return item


def duckdb_check(
    check_id: str,
    source_spec: str,
    category: str,
    check_type: str,
    sql: Optional[str],
    expected: str,
    rule_id: Optional[str] = None,
    notes: Optional[str] = None,
    runtime_strategy: Optional[str] = None,
) -> Dict[str, Any]:
    """A check in the DuckDB plan's own shape.

    Deliberately the same item ``make_check`` produces for the declarative
    contract, so the two files can be diffed against each other, with
    ``execution`` corrected: a check inside this file is meant to be run, and
    saying so is the difference between an instruction and a description.
    """
    item = make_check(
        check_id,
        source_spec,
        category,
        check_type,
        sql,
        expected,
        notes=notes,
        runtime_strategy=runtime_strategy,
    )
    item["execution"] = "duckdb-job"
    if rule_id:
        item["rule_id"] = rule_id
    return item


def _rules_by_id(spec: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Rules and overrides indexed by id, so a target column can find its recipe."""
    combined = list(spec.get("rules") or []) + list(spec.get("overrides") or [])
    return {str(rule.get("id")): rule for rule in combined if rule.get("id")}


def _raw_views(job: CompiledJob) -> List[Dict[str, Any]]:
    """The ``__RAW__`` relations one job compares against.

    A raw view is the Oracle base relation as the runtime exported it at the
    pinned SCN. It is what makes a fidelity claim checkable: comparing the
    projection against the target proves the sink was faithful, and only
    comparing the raw relation against the target can prove the *recipe* did not
    quietly lose something on the way.
    """
    views: List[Dict[str, Any]] = []
    for source in job.sources:
        schema, table = source["schema"], source["table"]
        views.append(
            {
                "view": f"__RAW__{sanitize_alias(schema)}_{sanitize_alias(table)}",
                "relation": source["relation"],
                "schema": schema,
                "table": table,
                "in_catalog": bool(source.get("catalog_present")),
            }
        )
    return views


def _primary_view(job: CompiledJob) -> str:
    """The raw view for the job's own driving relation.

    ``__RAW__`` views are per *relation*, not per job, so two jobs reading the
    same table share one. The driving relation is the one a passthrough column
    comes from, so it is the right one to join against.
    """
    source = job.sources[0]
    return f"__RAW__{sanitize_alias(source['schema'])}_{sanitize_alias(source['table'])}"


def _first_grouped_key(job: CompiledJob) -> Optional[str]:
    """The key a deduplicate keeps one row per, for the duplicate-row check."""
    for column in job.columns:
        if column.is_pk:
            return column.name
    return None


def _duckdb_acceptance_checks(spec: Dict[str, Any], job: CompiledJob, dj: DuckDBJob) -> List[Dict[str, Any]]:
    """Checks derived from ``acceptance``.

    Every check here is one the specification asked for by name. What differs
    from the declarative contract is which relations it compares: the contract
    names the raw source relation, this names ``__SOURCE__``, the projection the
    compiler actually produced. A row-selection rule legitimately reduces the
    row count, so counting the raw table against the target reports a failure
    that is really the specification working.
    """
    acceptance = spec.get("acceptance") or {}
    declared = [str(v) for v in acceptance.get("checks") or []]
    source, target = dj.source_view, dj.target_view
    checks: List[Dict[str, Any]] = []

    if "row-count-exact" in declared:
        checks.append(
            duckdb_check(
                f"V-ROWCOUNT-{job.job_id}",
                "acceptance.checks[row-count-exact]",
                "reconciliation",
                "row-count-exact",
                f"SELECT 1 WHERE (SELECT COUNT(*) FROM {source}) <> (SELECT COUNT(*) FROM {target})",
                "ZERO_ROWS",
                rule_id=job.job_id,
                notes=(
                    f"{source} is the compiled projection read at {SCN_BIND}, so this compares what "
                    "SeaTunnel read against what the sink wrote. Counting the raw table instead would "
                    "report a row-selection rule as a lost row."
                ),
            )
        )

    if "chunk-digest-match" in declared:
        checks.append(
            duckdb_check(
                f"V-DIGEST-{job.job_id}",
                "acceptance.checks[chunk-digest-match]",
                "reconciliation",
                "chunk-digest-match",
                None,
                "SOURCE_DIGEST_EQUALS_TARGET_DIGEST",
                rule_id=job.job_id,
                runtime_strategy="chunked-sha256",
                notes=(
                    f"Digest {source} against {target}. Canonical chunking, column order, NULL encoding "
                    "and the digest algorithm are runtime concerns. Rows a change-aware rule upserts are "
                    "excluded, because their previous image is not reproducible from a snapshot."
                ),
            )
        )

    if "quarantine-empty" in declared:
        max_rows = int(acceptance.get("maxQuarantinedRows", 0))
        if dj.quarantine_view:
            checks.append(
                duckdb_check(
                    f"V-QUARANTINE-{job.job_id}",
                    "acceptance.checks[quarantine-empty]",
                    "quality",
                    "quarantine-empty",
                    f"SELECT 1 FROM {dj.quarantine_view} LIMIT 1",
                    "ZERO_ROWS",
                    rule_id=job.job_id,
                    runtime_strategy=None if max_rows == 0 else f"tolerated-max={max_rows}",
                    notes=(
                        f"{dj.quarantine_view} holds every row a sentinel captured. The specification "
                        f"tolerates at most {max_rows}, so this is a bounded check when that number is "
                        "not zero, and an empty check when it is."
                    ),
                )
            )

    if "no-orphan-keys" in declared:
        checks.append(
            duckdb_check(
                f"V-NO-ORPHAN-{job.job_id}",
                "acceptance.checks[no-orphan-keys]",
                "integrity",
                "no-orphan-keys",
                None,
                "ZERO_ORPHANS",
                rule_id=job.job_id,
                runtime_strategy="catalog-fk-join",
                notes=(
                    "Concrete orphan SQL needs the source PK/FK catalog, which the specification does "
                    "not carry. The relationship is read from the raw views' catalog at run time."
                ),
            )
        )

    if "referential-integrity" in declared:
        checks.append(
            duckdb_check(
                f"V-REFERENTIAL-{job.job_id}",
                "acceptance.checks[referential-integrity]",
                "integrity",
                "referential-integrity",
                None,
                "NO_REFERENTIAL_VIOLATIONS",
                rule_id=job.job_id,
                runtime_strategy="catalog-fk-join",
                notes="Requires the source PK/FK metadata plus the target constraint list.",
            )
        )

    if "monotonic-key-unique" in declared:
        if job.primary_keys:
            keys = ", ".join(qident(name) for name in job.primary_keys)
            checks.append(
                duckdb_check(
                    f"V-KEY-UNIQUE-{job.job_id}",
                    "acceptance.checks[monotonic-key-unique]",
                    "integrity",
                    "monotonic-key-unique",
                    f"SELECT COUNT(*) - COUNT(DISTINCT ({keys})) AS duplicates FROM {target}",
                    "ZERO_DUPLICATE_KEYS",
                    rule_id=job.job_id,
                )
            )

    # -- stricter, per table ------------------------------------------------

    for index, strict in enumerate(acceptance.get("stricter") or []):
        match = strict.get("match") or {}
        name = str(match.get("name") or "*")
        if not fnmatch.fnmatch(job.sources[0]["schema"], str(match.get("schema") or "*")):
            continue
        if not fnmatch.fnmatch(job.sources[0]["table"], name):
            continue

        for check_name in strict.get("checks") or []:
            if check_name == "aggregate-match":
                tolerance = (strict.get("aggregateTolerance") or {}).get("value", 0)
                checks.append(
                    duckdb_check(
                        f"V-STRICT-{index}-AGGREGATE-{job.job_id}",
                        f"acceptance.stricter[{index}]",
                        "reconciliation",
                        "aggregate-match",
                        f"SELECT 1 WHERE ABS("
                        f"(SELECT SUM(CAST({qident('__aggregate_column__')} AS DOUBLE)) FROM {target}) "
                        f"- (SELECT SUM(CAST({qident('__aggregate_column__')} AS DOUBLE)) FROM {source})"
                        f") > {float(tolerance)}",
                        "ZERO_ROWS",
                        rule_id=job.job_id,
                        notes=(
                            f"Table {name}; tolerance={tolerance}. Both sides are aggregated over the "
                            "same named column, bound at run time, because a sum over which column is "
                            "a data-owner decision the specification does not make."
                        ),
                    )
                )
            elif check_name == "behaviour-equivalence":
                checks.append(
                    duckdb_check(
                        f"V-STRICT-{index}-BEHAVIOUR-{job.job_id}",
                        f"acceptance.stricter[{index}]",
                        "behaviour",
                        "behaviour-equivalence",
                        None,
                        "SOURCE_RESULT_EQUALS_TARGET_RESULT",
                        rule_id=job.job_id,
                        runtime_strategy="principal-parity",
                        notes=(
                            f"Table {name}. Row visibility is compared per principal on both sides, "
                            f"for {[p.get('source') for p in acceptance.get('principals') or []]}. A "
                            "projection cannot reproduce Oracle's row-level security, so this is the "
                            "only place that can be shown."
                        ),
                    )
                )

    for principal in acceptance.get("principals") or []:
        checks.append(
            duckdb_check(
                f"V-PRINCIPAL-{principal.get('source')}",
                "acceptance.principals",
                "behaviour",
                "principal-exists",
                None,
                "PRINCIPAL_PRESENT_ON_BOTH_SIDES",
                runtime_strategy="role-inspection",
                notes=f"{principal.get('source')} -> {principal.get('target')}. {principal.get('note', '')}",
            )
        )

    return checks


def _duckdb_delivery_checks(job: CompiledJob, dj: DuckDBJob) -> List[Dict[str, Any]]:
    """Prove the sink delivered the projection that was validated.

    This is the check that has no equivalent in the SeaTunnel file, because it
    can only exist once both sides are readable in the same place. Every compiled
    column is compared across ``__SOURCE__`` and ``__TARGET__`` with
    ``IS DISTINCT FROM``, so a NULL that became '', a trailing space that was
    trimmed in transit, a numeric that lost its scale on write, or an upsert that
    overwrote a newer row all show up as a named column rather than as a row-count
    difference nobody can explain.

    It needs a key to join on. Without one there is no defensible row pairing, so
    the check is emitted with no SQL and the runtime strategy that does apply
    rather than a join that would invent a cartesian product.
    """
    columns = [column.name for column in job.columns]
    if not columns or not dj.source_view or not dj.target_view:
        return []

    if not job.primary_keys:
        return [
            duckdb_check(
                f"V-DELIVERY-{job.job_id}",
                "compiled column list",
                "reconciliation",
                "column-delivery",
                None,
                "EVERY_COMPILED_COLUMN_MATCHES",
                rule_id=job.job_id,
                runtime_strategy="unordered-column-digest",
                notes=(
                    "No primary key was compiled for this relation, so source and target rows cannot "
                    "be paired without inventing a join. The check compares unordered per-column "
                    f"digests over {len(columns)} column(s) instead."
                ),
            )
        ]

    keys = ", ".join(qident(name) for name in job.primary_keys)
    key_names = {name.lower() for name in job.primary_keys}
    # The key columns are excluded: the USING join already guarantees they are
    # equal, so comparing them would add a branch that can never fail. What the
    # join *cannot* see is a row whose key is NULL -- USING drops it -- and that is
    # the row-count check's job, so the two cover each other rather than overlap.
    compared = [column for column in columns if column.lower() not in key_names]
    branches = [
        "SELECT {alias} AS \"column_name\" FROM {src} AS s JOIN {tgt} AS t USING ({keys}) "
        "WHERE s.{col} IS DISTINCT FROM t.{col}".format(
            alias=sql_literal(column),
            src=dj.source_view,
            tgt=dj.target_view,
            keys=keys,
            col=qident(column),
        )
        for column in compared
    ]
    if not branches:
        return []
    sql = "SELECT * FROM (\n  " + "\n  UNION ALL\n  ".join(branches) + "\n) LIMIT 20"

    return [
        duckdb_check(
            f"V-DELIVERY-{job.job_id}",
            "compiled column list",
            "reconciliation",
            "column-delivery",
            sql,
            "ZERO_ROWS",
            rule_id=job.job_id,
            notes=(
                f"{len(compared)} of {len(columns)} compiled column(s) compared across "
                f"{dj.source_view} and {dj.target_view} on key ({keys}), using IS DISTINCT FROM so a "
                "NULL that became an empty string is a difference. Any row returned names the column "
                f"that drifted. The {len(columns) - len(compared)} key column(s) are excluded because "
                "the join already proves them equal; a row whose key is NULL is invisible to a USING "
                "join and is caught by the row-count check instead."
            ),
        )
    ]


def _duckdb_fidelity_checks(
    spec: Dict[str, Any],
    job: CompiledJob,
    dj: DuckDBJob,
    rules_by_id: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Turn ``fidelityFloor`` and ``defaults`` into checks that can fail.

    The floor is a promise that no transport may drop a value state. A promise is
    not evidence, so each declared state is attached to the columns it can
    actually be observed on.

    The general observation is one comparison: for every target column the
    specification chose *not* to transform, the raw value must be identical on
    the target. That single comparison is what makes TRAILING_WHITESPACE,
    UNICODE_NORMALISATION, BINARY, HIGH_PRECISION and SUB_SECOND checkable, and it
    is why the raw views are bound at all. The floor entries below it are the ones
    that need a targeted assertion rather than a general one.
    """
    floor = [str(v) for v in spec.get("fidelityFloor") or []]
    defaults = dict(spec.get("defaults") or {})
    checks: List[Dict[str, Any]] = []

    # -- passthrough fidelity: the general observation ------------------------

    passthrough = [
        column
        for column in job.columns
        if column.source_name and str(column.origin) not in rules_by_id
    ]
    if passthrough and job.primary_keys:
        keys = ", ".join(qident(name) for name in job.primary_keys)
        raw = _primary_view(job)
        branches = [
            "SELECT {alias} AS \"column_name\" FROM {raw} AS r JOIN {tgt} AS t USING ({keys}) "
            "WHERE r.{src} IS DISTINCT FROM t.{tgtcol}".format(
                alias=sql_literal(column.name),
                raw=raw,
                tgt=dj.target_view,
                keys=keys,
                src=qident(str(column.source_name)),
                tgtcol=qident(column.name),
            )
            for column in passthrough
        ]
        sql = "SELECT * FROM (\n  " + "\n  UNION ALL\n  ".join(branches) + "\n) LIMIT 20"
        checks.append(
            duckdb_check(
                f"V-FIDELITY-{job.job_id}",
                "fidelityFloor",
                "fidelity",
                "passthrough-fidelity",
                sql,
                "ZERO_ROWS",
                rule_id=job.job_id,
                notes=(
                    f"{len(passthrough)} column(s) have no recipe, so the specification expects the "
                    f"raw value to arrive intact. {raw} is the Oracle relation at {SCN_BIND}. This is "
                    f"the check that makes {floor} evidence rather than a claim; a transformed column "
                    "is deliberately absent, because its expected value is whatever the recipe says."
                ),
            )
        )
    elif passthrough:
        checks.append(
            duckdb_check(
                f"V-FIDELITY-{job.job_id}",
                "fidelityFloor",
                "fidelity",
                "passthrough-fidelity",
                None,
                "EVERY_UNTRANSFORMED_COLUMN_MATCHES",
                rule_id=job.job_id,
                runtime_strategy="unordered-column-digest",
                notes=(
                    f"{len(passthrough)} untransformed column(s), but no primary key was compiled, so "
                    "the raw and target rows cannot be paired."
                ),
            )
        )

    # -- floor entries that need their own assertion --------------------------

    if "NULL" in floor:
        # A NOT NULL target column is the only place a dropped NULL is visible:
        # everywhere else the value is legitimately absent.
        not_null = [column for column in job.columns if not column.nullable]
        for column in not_null:
            checks.append(
                duckdb_check(
                    f"V-FIDELITY-NULL-{job.job_id}-{column.name}",
                    "fidelityFloor[NULL]",
                    "fidelity",
                    "not-null-preserved",
                    f"SELECT 1 FROM {dj.target_view} WHERE {qident(column.name)} IS NULL LIMIT 1",
                    "ZERO_ROWS",
                    rule_id=str(column.origin or job.job_id),
                    notes=(
                        f"{column.name} is declared NOT NULL, so any NULL here is a value the "
                        "migration lost rather than one the source held."
                    ),
                )
            )

    if "EMPTY_LOB" in floor or str(defaults.get("emptyStringIsNull")) == "null":
        # Oracle cannot hold an empty string, so '' and NULL are the same value on
        # the source. If the target engine can, a loader that writes '' for NULL
        # invents a row state the source never had.
        character = [
            column
            for column in job.columns
            if column.type_sql.lower().startswith(("character", "varchar", "text", "char"))
        ]
        if character:
            predicate = " OR ".join(f"{qident(column.name)} = ''" for column in character)
            checks.append(
                duckdb_check(
                    f"V-FIDELITY-EMPTY-LOB-{job.job_id}",
                    "fidelityFloor[EMPTY_LOB]",
                    "fidelity",
                    "empty-lob-preserved",
                    f"SELECT 1 FROM {dj.target_view} WHERE {predicate} LIMIT 1",
                    "ZERO_ROWS",
                    rule_id=job.job_id,
                    notes=(
                        f"defaults.emptyStringIsNull is {defaults.get('emptyStringIsNull')!r}, so on the "
                        "source an empty string *is* NULL. A non-empty target engine can hold both, so "
                        "an empty string here is a state the source never had: either a NULL was "
                        "invented, or an empty string was lost."
                    ),
                )
            )

    if "TZ_OFFSET" in floor and str(defaults.get("timestampWithTimeZone")) == "split-offset":
        # A split offset is a computed number of minutes, so it has a real range
        # and an out-of-range value is provably an arithmetic error rather than
        # bad data.
        offsets = [column for column in job.columns if column.name.endswith("_offset")]
        for column in offsets:
            checks.append(
                duckdb_check(
                    f"V-FIDELITY-TZOFFSET-{job.job_id}-{column.name}",
                    "fidelityFloor[TZ_OFFSET]",
                    "fidelity",
                    "offset-range",
                    f"SELECT 1 FROM {dj.target_view} WHERE {qident(column.name)} IS NULL "
                    f"OR {qident(column.name)} < -840 OR {qident(column.name)} > 840 LIMIT 1",
                    "ZERO_ROWS",
                    rule_id=str(column.origin or job.job_id),
                    notes=(
                        f"{column.name} is minutes east of UTC. Real offsets fall in -840..840, so "
                        "anything outside that means the offset arithmetic is wrong, not the data."
                    ),
                )
            )

    if "BINARY" in floor:
        binaries = [column for column in job.columns if column.type_sql.lower() in {"bytea", "blob", "varbinary"}]
        for column in binaries:
            checks.append(
                duckdb_check(
                    f"V-FIDELITY-BINARY-{job.job_id}-{column.name}",
                    "fidelityFloor[BINARY]",
                    "fidelity",
                    "binary-preserved",
                    f"SELECT 1 FROM {dj.target_view} WHERE {qident(column.name)} IS NULL LIMIT 1",
                    "ZERO_ROWS",
                    rule_id=str(column.origin or job.job_id),
                    runtime_strategy="byte-length-vs-source",
                    notes=(
                        f"{column.name} carries binary data. Comparing byte length against the raw "
                        "value catches a load that decoded it; a NULL check alone would not."
                    ),
                )
            )

    return checks


def _duckdb_operation_checks(spec: Dict[str, Any], job: CompiledJob, dj: DuckDBJob) -> List[Dict[str, Any]]:
    """Checks derived from what the compiled query actually did.

    Every entry here corresponds to a place where the specification asked for
    something a projection cannot deliver on its own: a miss that must stop the
    run, a grain that must be unique, a set of branches that must be exhaustive,
    an unlisted value that must not vanish. In each case the operation *emitted*
    a sentinel or a shape, and the check is what makes the assertion true.
    """
    checks: List[Dict[str, Any]] = []
    target = dj.target_view

    for operation in job.operations:
        kind = str(operation.get("operation"))
        rule_id = str(operation.get("rule_id") or job.job_id)

        if kind == "lookup":
            checks.append(
                duckdb_check(
                    f"V-LOOKUP-MISS-{rule_id}",
                    rule_id,
                    "integrity",
                    "lookup-miss",
                    None,
                    "ZERO_MISSES",
                    rule_id=rule_id,
                    runtime_strategy="on-miss-sentinel",
                    notes=(
                        "The lookup emitted its taken column as NULL and a miss counter for every row "
                        "the join did not match. A SELECT cannot raise, so an onMiss of fail-run is "
                        "delivered by this check rather than by the query."
                    ),
                )
            )

        elif kind == "set/deduplicate":
            key = _first_grouped_key(job)
            if key:
                checks.append(
                    duckdb_check(
                        f"V-DEDUPE-{rule_id}",
                        rule_id,
                        "reconciliation",
                        "duplicate-row-count",
                        f"SELECT COUNT(*) - COUNT(DISTINCT {qident(key)}) FROM {target}",
                        "ZERO_DUPLICATES",
                        rule_id=rule_id,
                    )
                )
            else:
                checks.append(
                    duckdb_check(
                        f"V-DEDUPE-{rule_id}",
                        rule_id,
                        "reconciliation",
                        "duplicate-row-count",
                        None,
                        "ONE_ROW_PER_DEDUPE_KEY",
                        rule_id=rule_id,
                        runtime_strategy="declared-order-sensitive-key",
                        notes=(
                            "The dedupe kept one row per key, but no key was compiled, so the "
                            "duplicates this was written to remove cannot be counted on the target."
                        ),
                    )
                )

        elif kind == "pivot":
            checks.append(
                duckdb_check(
                    f"V-PIVOT-GRAIN-{rule_id}",
                    rule_id,
                    "integrity",
                    "grain-uniqueness",
                    None,
                    "ONE_ROW_PER_GRAIN",
                    rule_id=rule_id,
                    runtime_strategy="declared-grain",
                    notes="The pivot grain must be unique on the target, one row per product.",
                )
            )

        elif kind == "column-split":
            sentinels = [column.name for column in job.columns if column.name.startswith(QUARANTINE_PREFIX)]
            if sentinels:
                predicate = " OR ".join(f"{qident(name)} IS NOT NULL" for name in sentinels)
                checks.append(
                    duckdb_check(
                        f"V-COLUMNSPLIT-{rule_id}",
                        rule_id,
                        "quality",
                        "split-extra-parts",
                        f"SELECT 1 FROM {target} WHERE {predicate} LIMIT 1",
                        "ZERO_ROWS",
                        rule_id=rule_id,
                        notes=(
                            f"{', '.join(sentinels)} counts the parts that had no target column. "
                            "Without this they would vanish silently."
                        ),
                    )
                )

        elif kind == "cardinality/split":
            checks.append(
                duckdb_check(
                    f"V-SPLIT-COVER-{rule_id}",
                    rule_id,
                    "reconciliation",
                    "split-branch-non-empty",
                    None,
                    "EVERY_BRANCH_HAS_ROWS",
                    rule_id=rule_id,
                    runtime_strategy="branch-counts",
                    notes=(
                        "A split partitions the source, so the branch counts must sum to the source "
                        "count and no branch may be empty. An empty branch means the discriminator "
                        "literal matched nothing, which is a silent loss rather than a zero."
                    ),
                )
            )

        elif kind == "cardinality/merge":
            checks.append(
                duckdb_check(
                    f"V-MERGE-DISCRIMINATOR-{rule_id}",
                    rule_id,
                    "reconciliation",
                    "merge-discriminator-distribution",
                    None,
                    "EVERY_SOURCE_PRESENT_ON_TARGET",
                    rule_id=rule_id,
                    runtime_strategy="group-by-discriminator",
                    notes=(
                        "The merge injects a discriminator per source, so every source must appear "
                        "on the target and no third value may appear."
                    ),
                )
            )

        elif kind == "relational/denormalise":
            checks.append(
                duckdb_check(
                    f"V-DENORMALISE-GRAIN-{rule_id}",
                    rule_id,
                    "integrity",
                    "grain-uniqueness",
                    None,
                    "ONE_ROW_PER_GRAIN",
                    rule_id=rule_id,
                    runtime_strategy="declared-grain",
                    notes=(
                        "grain.uniqueness is asserted, not proven. A fan-out join breaks the assertion "
                        "silently, so this is the check that makes it true."
                    ),
                )
            )

        elif kind == "change-aware/propagate-if-changed":
            checks.append(
                duckdb_check(
                    f"V-UPSERT-KEY-{rule_id}",
                    rule_id,
                    "integrity",
                    "upsert-target-has-unique-key",
                    None,
                    "UPSERT_TARGET_HAS_UNIQUE_KEY",
                    rule_id=rule_id,
                    runtime_strategy="ddl-inspection",
                    notes=(
                        "The job is written as an upsert, which fails at run time if the target key "
                        "is not unique. This reports it before the load rather than during it."
                    ),
                )
            )

    for sentinel in job.quarantine_filters:
        column = sentinel.get("column")
        if not column:
            continue
        checks.append(
            duckdb_check(
                f"V-QUARANTINE-COLUMN-{job.job_id}-{column}",
                str(sentinel.get("origin") or job.job_id),
                "quality",
                "quarantine-sentinel",
                f"SELECT 1 FROM {target} WHERE {qident(column)} IS NOT NULL LIMIT 1",
                "ZERO_ROWS",
                notes=f"{column} is a quarantine sentinel, not business data.",
            )
        )

    return checks


def _duckdb_sensitive_checks(spec: Dict[str, Any], job: CompiledJob, dj: DuckDBJob) -> List[Dict[str, Any]]:
    """Checks derived from ``sensitive``.

    A declared handling is a promise about what is *not* on the target, so the
    check is the complement of the promise rather than a statement about it:

      * ``mask``  -- the column exists and does not equal the raw value
      * ``drop``  -- the column is absent from the target entirely
      * ``keep``  -- the column is present and equal to the raw value
    """
    rules_by_id = _rules_by_id(spec)
    raw = _primary_view(job)
    keys = ", ".join(qident(name) for name in job.primary_keys) if job.primary_keys else ""
    target_names = {column.name.lower(): column for column in job.columns}
    checks: List[Dict[str, Any]] = []

    for index, entry in enumerate(spec.get("sensitive") or []):
        match = entry.get("match") or {}
        schema, table = str(match.get("schema") or ""), str(match.get("name") or "")
        if not fnmatch.fnmatch(job.sources[0]["schema"], schema):
            continue
        if not fnmatch.fnmatch(job.sources[0]["table"], table):
            continue

        handling = str(entry.get("handling"))
        source_column = str(match.get("column") or "")
        where = f"sensitive[{index}]"

        rule = rules_by_id.get(str(entry.get("rule_id") or ""), None)
        if rule is None:
            for candidate_id, candidate in rules_by_id.items():
                rmatch = candidate.get("match") or {}
                if (
                    rmatch.get("objectClass") == match.get("objectClass")
                    and rmatch.get("schema") == match.get("schema")
                    and rmatch.get("name") == match.get("name")
                    and rmatch.get("column") == match.get("column")
                ):
                    rule = candidate
                    break

        target_column = None
        for column in job.columns:
            if str(column.origin) == str((rule or {}).get("id") or "\0"):
                target_column = column
                break
            if column.source_name and column.source_name.upper() == source_column.upper():
                target_column = column
        if target_column is None and len(target_names) == 1:
            target_column = next(iter(target_names.values()))

        if handling == "drop":
            present = target_column.name if target_column else None
            checks.append(
                duckdb_check(
                    f"V-SENSITIVE-{job.job_id}-DROP-{source_column}",
                    where,
                    "sensitive",
                    "sensitive-dropped",
                    None,
                    "COLUMN_ABSENT_FROM_TARGET",
                    rule_id=str((rule or {}).get("id") or source_column),
                    runtime_strategy="target-column-list",
                    notes=(
                        f"{source_column} is declared dropped. The compiled target "
                        + (
                            f"does not carry it (no column named {present!r} was compiled for it)."
                            if present is None
                            else f"carries {present!r}, which contradicts the declaration."
                        )
                    ),
                )
            )
            if target_column is not None:
                checks[-1]["notes"] += (
                    " A column named after it *is* on the target, so this is a governance finding "
                    "rather than a silent pass."
                )
            continue

        if target_column is None or not target_column.name:
            continue

        if not keys:
            checks.append(
                duckdb_check(
                    f"V-SENSITIVE-{job.job_id}-{handling.upper()}-{source_column}",
                    where,
                    "sensitive",
                    f"sensitive-{handling}",
                    None,
                    "SOURCE_AND_TARGET_DISAGREE" if handling in {"mask", "keep"} else "COMPARABLE",
                    rule_id=str((rule or {}).get("id") or source_column),
                    runtime_strategy="unordered-column-digest",
                    notes=(
                        f"{source_column} is declared {handling}, but this relation has no compiled "
                        "key, so raw and target rows cannot be paired."
                    ),
                )
            )
            continue

        raw_name = target_column.source_name or source_column
        comparison = (
            f"SELECT 1 FROM {raw} AS r JOIN {dj.target_view} AS t USING ({keys}) "
            f"WHERE r.{qident(raw_name)} IS NOT DISTINCT FROM t.{qident(target_column.name)} LIMIT 1"
        )

        if handling == "mask":
            notes = (
                f"{source_column} is declared masked and lands as {target_column.name}. A masked "
                "value is irreversible, so it must never equal the raw one; any row where it does is "
                "a masking rule that did not apply to that row."
            )
        elif handling == "keep":
            notes = (
                f"{source_column} is declared kept and must arrive byte-identical, so this confirms "
                "that nothing in the transport treated it as sensitive and redacted it."
            )
        else:
            notes = f"{source_column} is declared {handling} and lands as {target_column.name}."

        checks.append(
            duckdb_check(
                f"V-SENSITIVE-{job.job_id}-{handling.upper()}-{source_column}",
                where,
                "sensitive",
                f"sensitive-{handling}",
                comparison,
                "ZERO_ROWS",
                rule_id=str((rule or {}).get("id") or source_column),
                notes=notes,
            )
        )

    return checks


def _duckdb_governance_checks(spec: Dict[str, Any], job: CompiledJob, dj: DuckDBJob) -> List[Dict[str, Any]]:
    """One check per governance finding, so sign-off is backed by evidence.

    ``governance.requiresApproval`` names categories a human must sign off on. A
    signed-off finding that produces no evidence is a comment; this makes each one
    an assertion the runner can re-evaluate on the loaded data.
    """
    findings = job.diagnostics.of("GOVERNANCE")
    if not findings:
        return []
    checks: List[Dict[str, Any]] = []
    for finding in findings:
        checks.append(
            duckdb_check(
                f"V-GOVERNANCE-{job.job_id}-{finding.code}",
                f"job.{job.job_id}",
                "governance",
                "governance-attested",
                None,
                "ATTESTED_BY_A_NAMED_HUMAN",
                rule_id=finding.rule_id or job.job_id,
                runtime_strategy="signed-off-against-evidence",
                notes=(
                    f"{finding.message} This was a compile-time finding; the check is the run-time "
                    "evidence for the approval that governance.requiresApproval asks for."
                ),
            )
        )
    return checks


def build_duckdb_jobs(
    spec: Dict[str, Any],
    jobs: List[CompiledJob],
    spec_path: Path,
    diags: Diagnostics,
) -> List[DuckDBJob]:
    """Compile one DuckDB validation job per target relation.

    A blocked job is skipped entirely. It has no query, so there is no projection
    to compare and no target to compare it against; emitting a job with empty
    checks would let a blocked migration look like a validated one.
    """
    plans: List[DuckDBJob] = []

    for job in jobs:
        if job.query is None:
            continue

        dj = DuckDBJob(
            job_id=job.job_id,
            kind=job.kind,
            target=dict(job.target),
            target_view=duckdb_view_name(TARGET_VIEW_PREFIX, job),
            source_view=duckdb_view_name(SOURCE_VIEW_PREFIX, job),
            raw_relations=_raw_views(job),
            primary_keys=list(job.primary_keys),
            columns=[column.as_dict() for column in job.columns],
            assumptions=list(job.assumptions),
            # Stripped of the leading colon so it matches the `${run_scn}` form the
            # runner substitutes, rather than the `:run_scn` form the compiler
            # parses with.
            binds=[str(bind["placeholder"]).lstrip(":") for bind in job.binds],
        )
        if job.quarantine_filters:
            dj.quarantine_view = duckdb_view_name(QUARANTINE_VIEW_PREFIX, job)

        projection, glot = render_duckdb(
            job.query, diags, rule_id=None, where=f"the projection compiled for {job.job_id}"
        )
        dj.projection = projection
        dj.projection_glot = glot
        if glot.get("residual"):
            dj.blockers.append(
                f"the projection still calls {glot['residual']}, which DuckDB cannot bind"
            )
        if glot.get("blocked"):
            dj.blockers.append(
                "the projection contains a construct that could not be translated for DuckDB"
            )
        if glot.get("stable") == "DRIFT":
            dj.blockers.append("the DuckDB projection does not survive a second parse unchanged")

        dj.checks.extend(_duckdb_acceptance_checks(spec, job, dj))
        dj.checks.extend(_duckdb_delivery_checks(job, dj))
        dj.checks.extend(_duckdb_fidelity_checks(spec, job, dj, _rules_by_id(spec)))
        dj.checks.extend(_duckdb_operation_checks(spec, job, dj))
        dj.checks.extend(_duckdb_sensitive_checks(spec, job, dj))
        dj.checks.extend(_duckdb_governance_checks(spec, job, dj))

        # Every emitted statement has to bind. `make_check` already proved it
        # parses; a check that parsed and still failed to parse here would mean the
        # text changed between the proof and the artifact, which is the whole thing
        # the artifact re-read exists to prevent.
        for check in dj.checks:
            if check.get("sql") and check.get("sqlglot", {}).get("parse") != "PASS":
                dj.blockers.append(f"check {check['check_id']} does not parse as DuckDB")

        dj.notes.append(
            f"{len(dj.checks)} check(s) over {len(dj.columns)} compiled column(s); "
            f"{len(dj.raw_relations)} raw relation(s) bound."
        )
        plans.append(dj)

    return plans


def render_yaml_document(value: Dict[str, Any], header: Sequence[str] = ()) -> str:
    """Serialise a YAML document, optionally preceded by comment lines.

    The SeaTunnel artifact is written by hand because HOCON is not YAML. This one
    is YAML, so it is emitted by the same serialiser that will be read back, and
    the header is prepended as real comments rather than a data field that a
    consumer would have to know to ignore.
    """
    body = yaml.safe_dump(
        value,
        sort_keys=False,
        allow_unicode=True,
        width=100,
        default_flow_style=False,
    )
    if not header:
        return body
    # A header line may already be written as `# ...`; prefixing it again would
    # produce `# # ...` and, worse, throw away the column alignment the caller
    # wrote the table with.
    lines = [line if line.startswith("#") else f"# {line}" for line in header]
    return "\n".join(line.rstrip() for line in lines) + "\n" + body


def _duckdb_job_body(dj: DuckDBJob, order: int, depends_on: Sequence[str]) -> Dict[str, Any]:
    """The body of one DuckDB job, shaped like a SeaTunnel job body.

    Same section order on purpose: ``env``, then what is read, then what is
    written, then what is checked, then what is emitted. A reader who knows one
    file can read the other without a map.
    """
    body: Dict[str, Any] = {
        "order": order,
        "env": {
            "job_name": dj.job_id,
            "mode": "READ_ONLY",
            "snapshot": "${run_scn}",
            "zero_rows_means_pass": True,
            "nonzero_rows_means_fail": True,
        },
        "source": {
            "projection_view": dj.source_view,
            "projection": dj.projection,
            "sqlglot": dj.projection_glot,
            "materialise": (
                f"CREATE OR REPLACE VIEW {dj.source_view} AS {dj.projection}"
                if dj.projection
                else None
            ),
            "raw_relations": dj.raw_relations,
            "raw_instruction": (
                "Export each listed Oracle relation once, at the pinned SCN, and attach it under its "
                "`view` name. Every check in this job then compares against that one export, so the "
                "source and the target are read at the same snapshot."
            ),
        },
        "target": {
            "relation": dj.target.get("relation"),
            "view": dj.target_view,
            "quarantine_view": dj.quarantine_view,
            "instruction": (
                f"Attach the loaded {dj.target.get('relation')} as {dj.target_view}."
            ),
        },
        "check": dj.checks,
        "sink": {
            "on_violation": "report",
            "evidence": f"{dj.job_id}.evidence.json",
            "records": "one row per violation",
            "pass_condition": "every check returns zero rows",
        },
    }
    if dj.binds:
        body["env"]["binds"] = dj.binds
    if depends_on:
        body["env"]["depends_on"] = list(depends_on)
    if dj.blockers:
        body["blockers"] = dj.blockers
    if dj.assumptions:
        body["assumptions"] = dj.assumptions
    return body


def build_duckdb_jobs_document(
    plans: List[DuckDBJob],
    spec: Dict[str, Any],
    spec_path: Path,
    jobs: List[CompiledJob],
) -> Dict[str, Any]:
    """The single DuckDB file: every validation job, in submission order."""
    run = spec.get("run") or {}
    body: Dict[str, Any] = {
        "duckdb_jobs_version": "1.0",
        "produced_by": f"{TOOL_NAME} {TOOL_VERSION}",
        "source_of_truth": str(spec_path),
        "engine": {"name": "duckdb", "dialect": "duckdb"},
        "convention": {
            "zero_rows_means_pass": True,
            "nonzero_rows_means_fail": True,
            "records": "one row per violation",
            "relations_bound_at_run_time": True,
            "note": (
                "No check names an Oracle relation directly. The runtime exports the source at the "
                "pinned SCN and attaches the loaded target, and every check compares those views, so "
                "a check is comparable across jobs and the snapshot is one decision."
            ),
        },
        "acquisition": str(run.get("acquisition") or ""),
        "bindings": {
            SCN_BIND.lstrip(":"): SCN_BIND_PATH,
            **(
                {WATERMARK_BIND.lstrip(":"): WATERMARK_BIND_PATH}
                if str(run.get("acquisition")) == "query-incremental"
                else {}
            ),
        },
        "job": {
            dj.job_id: _duckdb_job_body(
                dj,
                order=position,
                depends_on=_duckdb_depends_on(plans[: position - 1], dj),
            )
            for position, dj in enumerate(plans, start=1)
        },
        "summary": {
            "jobs": len(plans),
            "checks": sum(dj.check_count for dj in plans),
            "checks_with_sql": sum(1 for dj in plans for c in dj.checks if c.get("sql")),
            "checks_runtime_only": sum(
                1 for dj in plans for c in dj.checks if not c.get("sql")
            ),
            "blocked_jobs": [job.job_id for job in jobs if job.query is None],
            "blockers": {dj.job_id: dj.blockers for dj in plans if dj.blockers},
        },
    }
    return body


def _duckdb_depends_on(earlier: List[DuckDBJob], dj: DuckDBJob) -> List[str]:
    """Jobs that must finish loading before this one can be checked.

    A merge or a split consumes another job's *output*, so the producing job has
    to have loaded before the consuming job can be checked. A lookup or a join
    reads the source, not the target, so it creates no ordering requirement.
    """
    consumed = {
        relation(source["schema"], source["table"]) for source in dj.raw_relations
    }
    return [
        other.job_id
        for other in earlier
        if consumed & {relation(other.target["schema"], other.target["table"])}
    ]


def validate_duckdb_artifacts(
    document_text: str,
    plans: List[DuckDBJob],
) -> Dict[str, Any]:
    """Re-read the emitted DuckDB file and re-prove every statement in it.

    This is the same invariant the SeaTunnel artifact is held to, applied to the
    second file: the artifact that leaves this compiler is the file, not the dict
    it came from. So the text is parsed back with the YAML reader, every job is
    matched to the job it was compiled from, every ``check[].sql`` is extracted
    *from the file* and re-parsed in the DuckDB dialect, and every relation a
    check names is checked against the relations the job actually binds.

    A quoting bug here produces a file that reads as correct and checks nothing,
    which is the failure mode this stage exists to make impossible.
    """
    key = DUCKDB_JOBS_NAME
    if not isinstance(document_text, str) or not document_text.strip():
        return {
            "ok": False,
            "jobs_checked": 0,
            "checks_reparsed": 0,
            "failures": [{"file": key, "problem": "the DuckDB jobs file was not produced"}],
        }

    try:
        document = yaml.safe_load(document_text)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "jobs_checked": 0,
            "checks_reparsed": 0,
            "failures": [{"file": key, "problem": f"does not parse as YAML: {exc}"}],
        }

    if not isinstance(document, dict) or not isinstance(document.get("job"), dict):
        return {
            "ok": False,
            "jobs_checked": 0,
            "checks_reparsed": 0,
            "failures": [{"file": key, "problem": "the file has no `job:` mapping"}],
        }

    failures: List[Dict[str, str]] = []
    by_id = {dj.job_id: dj for dj in plans}
    seen: set = set()
    checks_reparsed = 0
    bound_prefixes = (SOURCE_VIEW_PREFIX, TARGET_VIEW_PREFIX, QUARANTINE_VIEW_PREFIX, "__RAW__")

    for name, body in document["job"].items():
        seen.add(name)
        if not isinstance(body, dict):
            failures.append({"file": key, "problem": f"job `{name}` is not a mapping"})
            continue
        dj = by_id.get(name)
        if dj is None:
            failures.append({"file": key, "problem": f"job `{name}` has no compiled job behind it"})
            continue

        target_view = ((body.get("target") or {}).get("view")) or ""
        source_view = ((body.get("source") or {}).get("projection_view")) or ""
        raw_views = {str(entry.get("view")) for entry in ((body.get("source") or {}).get("raw_relations") or [])}

        if target_view != dj.target_view:
            failures.append(
                {
                    "file": key,
                    "problem": f"job `{name}` binds target view `{target_view}` but compiled `{dj.target_view}`",
                }
            )
        if source_view != dj.source_view:
            failures.append(
                {
                    "file": key,
                    "problem": f"job `{name}` binds source view `{source_view}` but compiled `{dj.source_view}`",
                }
            )

        # The projection is the query DuckDB will actually run, so it is re-parsed
        # from the file exactly as the SeaTunnel query is.
        projection = (body.get("source") or {}).get("projection")
        if dj.projection:
            if not projection:
                failures.append({"file": key, "problem": f"job `{name}` carries no projection"})
            else:
                rendered, glot = render_duckdb(projection)
                if rendered is None or glot.get("parse") != "PASS":
                    failures.append(
                        {
                            "file": key,
                            "problem": f"job `{name}` projection does not re-parse: {glot.get('error')}",
                        }
                    )
                elif _duckdb_unbindable(parse_one(rendered, read="duckdb")):
                    failures.append(
                        {
                            "file": key,
                            "problem": (
                                f"job `{name}` projection calls "
                                f"{_duckdb_unbindable(parse_one(rendered, read='duckdb'))}, "
                                "which DuckDB cannot bind"
                            ),
                        }
                    )
                else:
                    checks_reparsed += 1

        checks = body.get("check") or []
        expected_ids = [check["check_id"] for check in dj.checks]
        found_ids = [str(check.get("check_id")) for check in checks if isinstance(check, dict)]
        if found_ids != expected_ids:
            failures.append(
                {
                    "file": key,
                    "problem": (
                        f"job `{name}` carries checks {found_ids} but compiled {expected_ids}; "
                        "the file and the compiler disagree about what to check"
                    ),
                }
            )

        for check in checks:
            if not isinstance(check, dict):
                continue
            sql = check.get("sql")
            if not sql:
                continue
            # The round-trip proof (invariant 2.2) applied to the text as it sits
            # in the file: parse, generate, re-parse, generate. A statement whose
            # second render differs from its first would change between the
            # version that was validated and the version that runs.
            try:
                first = parse_one(str(sql), read="duckdb").sql(dialect="duckdb")
                again = parse_one(first, read="duckdb").sql(dialect="duckdb")
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    {"file": key, "problem": f"check `{check.get('check_id')}` does not re-parse: {exc}"}
                )
                continue
            if first != again:
                failures.append(
                    {
                        "file": key,
                        "problem": (
                            f"check `{check.get('check_id')}` does not survive a second parse "
                            "unchanged; the validated text and the shipped text would differ"
                        ),
                    }
                )
                continue
            checks_reparsed += 1

            # A check may only name relations its own job binds. Anything else is a
            # relation the runner will not have, and it fails at run time with a
            # name nobody can trace back to this file.
            allowed = {target_view, source_view, *raw_views}
            for named in set(re.findall(r"\b(__[A-Z]+__[A-Za-z0-9_]+)", str(sql))):
                if named in allowed:
                    continue
                if named.startswith(bound_prefixes) and named in {
                    dj.target_view,
                    dj.source_view,
                    dj.quarantine_view or "",
                    *_raw_view_names(dj),
                }:
                    continue
                failures.append(
                    {
                        "file": key,
                        "problem": (
                            f"check `{check.get('check_id')}` reads `{named}`, which job `{name}` "
                            f"does not bind; it binds {sorted(allowed)}"
                        ),
                    }
                )

    missing = sorted(set(by_id) - seen)
    for name in missing:
        failures.append({"file": key, "problem": f"compiled job `{name}` was not emitted"})

    return {
        "ok": not failures,
        "jobs_checked": len(document["job"]),
        "checks_reparsed": checks_reparsed,
        "failures": failures,
        "note": (
            "The single DuckDB file is re-read with the YAML reader, every job is matched back to the "
            "job it was compiled from, every projection and every check's SQL is extracted from the "
            "file and re-parsed in the DuckDB dialect, and every relation a check names is checked "
            "against the relations that job binds."
        ),
    }


def _raw_view_names(dj: DuckDBJob) -> List[str]:
    return [str(entry["view"]) for entry in dj.raw_relations]


def validate_artifacts(
    artifacts: Dict[str, Any],
    jobs: List[CompiledJob],
    diags: Diagnostics,
) -> Dict[str, Any]:
    """Re-read the generated config and validate every query inside it.

    This is the stage that makes the SeaTunnel config, rather than the in-memory
    query, the thing that was validated. It is deliberately run last in the
    compiler and deliberately run over the file that was produced, because the
    artifact that leaves this compiler is the config, not the string in memory.
    """
    conf_key = SEATUNNEL_CONF_NAME
    content = artifacts.get(conf_key)

    if not isinstance(content, str):
        return {
            "ok": False,
            "files_checked": 0,
            "jobs_checked": 0,
            "queries_reparsed": 0,
            "failures": [{"file": conf_key, "problem": "the SeaTunnel config was not produced"}],
            "note": "One config carries every compiled query.",
        }

    failures: List[Dict[str, str]] = []
    jobs_checked = 0
    queries_reparsed = 0

    by_name: Dict[str, CompiledJob] = {}
    for job in jobs:
        # A blocked job has no query, so nothing is emitted for it. Expecting it in
        # the file would report every blocked job as a missing job and make this
        # stage fail on exactly the runs where it is already blocked for a better
        # reason. The blocked jobs are reported from the compile report instead.
        if job.query is None:
            continue
        by_name[seatunnel_job_name(job, "ddl")] = job
        by_name[seatunnel_job_name(job, "data")] = job

    columns_by_job = {job.job_id: [column.name for column in job.columns] for job in jobs}
    declared_binds = {SCN_BIND.lstrip(":"), WATERMARK_BIND.lstrip(":")}

    try:
        parsed = parse_hocon(content)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "files_checked": 1,
            "jobs_checked": 0,
            "queries_reparsed": 0,
            "failures": [{"file": conf_key, "problem": f"does not parse as HOCON: {exc}"}],
            "note": "One config carries every compiled query.",
        }

    job_block = parsed.get("job")
    if not isinstance(job_block, dict):
        return {
            "ok": False,
            "files_checked": 1,
            "jobs_checked": 0,
            "queries_reparsed": 0,
            "failures": [{"file": conf_key, "problem": "the config has no `job { ... }` block"}],
            "note": "One config carries every compiled query.",
        }

    seen_names: set = set()

    for name, settings in job_block.items():
        seen_names.add(name)
        jobs_checked += 1

        if name not in by_name:
            failures.append({"file": conf_key, "problem": f"job `{name}` has no compiled job behind it"})
            continue

        job = by_name[name]
        expected = f"{job.target['schema']}.{job.target['table']}"

        source_block = settings.get("source") or {}
        transform_block = settings.get("transform") or {}
        sink_block = settings.get("sink") or {}

        # A snapshot job carries the compiled query in the source block; a CDC job
        # carries it in the transform, because the source is the change stream.
        query = None
        query_origin = None
        for block, label in ((source_block, "source"), (transform_block, "transform")):
            for plugin_settings in block.values():
                if isinstance(plugin_settings, dict) and plugin_settings.get("query"):
                    query = plugin_settings["query"]
                    query_origin = label

        if query is None:
            failures.append(
                {"file": conf_key, "problem": f"job `{name}` has no query in its source or transform block"}
            )
            continue

        normalized = query.replace("${run_scn}", SCN_BIND).replace("${run_watermark}", WATERMARK_BIND)
        rendered, glot = render_ctunnel(normalized)
        if rendered is None:
            failures.append(
                {
                    "file": conf_key,
                    "problem": f"job `{name}` {query_origin} query does not re-parse: {glot.get('error')}",
                }
            )
            continue
        queries_reparsed += 1

        for sink_settings in sink_block.values():
            if not isinstance(sink_settings, dict):
                continue
            table = str(sink_settings.get("table_name") or "")
            if not table or table.endswith("_quarantine"):
                continue
            declared = f"{sink_settings.get('table_schema')}.{table}"
            if declared != expected:
                failures.append(
                    {
                        "file": conf_key,
                        "problem": f"job `{name}` writes to {declared} but its compiled target is {expected}",
                    }
                )

        # The ddl job reads zero rows to fix the column list, so its projection
        # has to be exactly the compiled target columns. A mismatch here is how a
        # target table gets created with the wrong shape.
        if name.endswith("_ddl"):
            shape_found = set(re.findall(r'AS "([^"]+)"', query))
            shape_expected = set(columns_by_job.get(job.job_id, []))
            if shape_expected and shape_expected != shape_found:
                failures.append(
                    {
                        "file": conf_key,
                        "problem": (
                            f"job `{name}` projects a column list that differs from the compiled "
                            f"target columns: missing {sorted(shape_expected - shape_found)}, "
                            f"unexpected {sorted(shape_found - shape_expected)}"
                        ),
                    }
                )

    missing = sorted(set(by_name) - seen_names)
    for name in missing:
        failures.append({"file": conf_key, "problem": f"compiled job `{name}` was not emitted"})

    for placeholder in sorted(set(re.findall(r"\$\{([^}]+)\}", content))):
        if placeholder in declared_binds:
            continue
        if placeholder.startswith(("SOURCE_", "TARGET_", "QUARANTINE_", "CDC_")):
            continue
        failures.append({"file": conf_key, "problem": f"undeclared placeholder ${{{placeholder}}}"})

    return {
        "ok": not failures,
        "files_checked": 1,
        "jobs_checked": jobs_checked,
        "queries_reparsed": queries_reparsed,
        "failures": failures,
        "note": (
            "The single config is re-read with the HOCON reader, every job inside it is matched "
            "back to the job it came from, every query is extracted from the file and re-parsed in "
            "the source dialect, every sink target is compared with the compiled target, and every "
            "${placeholder} is checked against the declared bindings."
        ),
    }


def _fk_between(entry: Dict[str, Any], job: CompiledJob) -> bool:
    """True when this job's data load must wait for an earlier job to finish.

    A lookup or a denormalise join reads its peer from the *source*, not from the
    target, so there is no ordering requirement between them. What does create one
    is a merge or a split, whose sources another job consumes.
    """
    for source in job.sources:
        if source["relation"] == entry.get("target_relation"):
            return True
    return False


# ---------------------------------------------------------------------------
# Compiler driver
# ---------------------------------------------------------------------------

def output_folder_name(spec_path: Path) -> str:
    """The folder one specification's output goes in, named after its own file.

    Two specifications produce two folders, so neither can overwrite the other.
    Derived from the stem rather than typed, so `foo.yaml` and `foo.yml` cannot
    land in one folder and silently replace each other.

    Only the stem is used, so `hr/hr-spec.yaml` and `shop/hr-spec.yaml` still
    collide. That is deliberate: two specifications of the same name need
    distinguishing names, and inventing a suffix silently would make the output
    path depend on a rule nobody was told.
    """
    return spec_path.stem


def compile_all(
    spec: Dict[str, Any],
    spec_path: Path,
    catalog: Optional[Dict[str, Any]] = None,
    catalog_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run the whole compilation and return every artifact plus the diagnostics."""
    spec_diags = Diagnostics()
    parsed_catalog = parse_catalog(spec, catalog, str(catalog_path) if catalog_path else "")
    parsed_catalog = filter_catalog_to_spec(parsed_catalog, spec, spec_diags)
    # After filtering, so the shapes a rule states are scoped exactly as the catalog
    # was, and before anything reads the catalog, so every reader sees one shape.
    parsed_catalog = merge_inline_source_tables(parsed_catalog, spec)
    naming = Naming(spec, spec_diags)

    validate_spec(spec, parsed_catalog, spec_diags)

    types = TypeResolver(spec, parsed_catalog, spec_diags)
    plans = plan_jobs(spec, parsed_catalog, naming, spec_diags)

    jobs: List[CompiledJob] = []
    for plan in plans:
        jobs.append(compile_job(spec, plan, parsed_catalog, naming, types, spec_diags))

    jobs.sort(key=lambda job: (job.target["schema"], job.target["table"]))

    # Two artifacts, and only two. Each is a single file holding everything for
    # its engine, which is what makes the whole migration one reviewable diff and
    # impossible to half-apply.
    artifacts: Dict[str, Any] = {}

    conf = build_seatunnel_conf(jobs, spec, spec_path)
    artifacts[SEATUNNEL_CONF_NAME] = conf

    # The DuckDB file is rendered to text first and validated as text, because the
    # artifact that leaves this compiler is the file.
    duckdb_plans = build_duckdb_jobs(spec, jobs, spec_path, spec_diags)
    duckdb_document = build_duckdb_jobs_document(duckdb_plans, spec, spec_path, jobs)
    duckdb_text = render_yaml_document(duckdb_document, _duckdb_header(spec, jobs, duckdb_plans))
    artifacts[DUCKDB_JOBS_NAME] = duckdb_text

    validation = validate_artifacts(artifacts, jobs, spec_diags)
    duckdb_validation = validate_duckdb_artifacts(duckdb_text, duckdb_plans)

    return {
        "artifacts": artifacts,
        "spec": spec,
        "jobs": jobs,
        "diagnostics": spec_diags,
        "catalog": parsed_catalog,
        "naming": naming,
        "validation": validation,
        "duckdb_validation": duckdb_validation,
        "duckdb_plans": duckdb_plans,
        "catalog_path": catalog_path,
        "seatunnel_jobs": len([job for job in jobs if job.query is not None]) * 2,
        "duckdb_checks": sum(dj.check_count for dj in duckdb_plans),
    }


def _duckdb_header(
    spec: Dict[str, Any],
    jobs: List[CompiledJob],
    plans: List[DuckDBJob],
) -> List[str]:
    """The comment block at the top of the DuckDB file.

    Written as a real header rather than a data field, because a consumer should
    not have to know to ignore a key in order to read the file. It states the
    things a reader cannot infer: which relations are bound at run time, what a
    pass looks like, and that nothing here was executed.
    """
    run = spec.get("run") or {}
    blocked = [job.job_id for job in jobs if job.query is None]
    checks = sum(dj.check_count for dj in plans)

    lines = [
        "=" * 74,
        f"Generated by {TOOL_NAME} {TOOL_VERSION} from the approved migration specification.",
        "This file is generated. Edit the specification and recompile; do not edit this file.",
        "=" * 74,
        "",
        f"# jobs            : {len(plans)}",
        f"# checks          : {checks}",
        "# engine          : duckdb",
        f"# acquisition     : {run.get('acquisition')}",
        "#",
        "# One job per target relation, in the same order as the SeaTunnel file and under the",
        "# same job id, so a target relation, its load and its validation can be read together.",
        "#",
        "# Nothing was executed to produce this file. Every statement is generated and proven to",
        "# bind in DuckDB; running it is the runner's job.",
        "#",
        "# Each job binds three relations at run time:",
        f"#   {SOURCE_VIEW_PREFIX}<job>       the compiled projection, read at ${{run_scn}}",
        f"#   {TARGET_VIEW_PREFIX}<job>       the target relation as the load left it",
        "#   __RAW__<schema>_<table>  the Oracle source, exported once at the pinned SCN",
        "#",
        "# A check passes when it returns zero rows. Each row returned is one violation and names",
        "# the column that failed, so a failure is a starting point rather than a count.",
        "#",
        "# Required binding:",
        f"#   run_scn            resolved from {SCN_BIND_PATH}",
    ]
    if str(run.get("acquisition")) == "query-incremental":
        lines.append(f"#   run_watermark      resolved from {WATERMARK_BIND_PATH}")
    if blocked:
        lines.extend(
            [
                "#",
                f"# NOT EMITTED -- {len(blocked)} job(s) are blocked and have no query: "
                + ", ".join(blocked),
                "# A blocked job is never emitted as a partial guess.",
            ]
        )
        # Which job failed for which reason -- the cause is what tells a reader
        # what to change, and this file is the only place they will look.
        for job in jobs:
            if job.job_id not in blocked:
                continue
            for reason in job.diagnostics.items:
                if reason.severity == "BLOCK":
                    lines.append(f"#   {job.job_id}: {reason.code} -- {reason.message}")
    lines.append("=" * 74)
    return lines

def write_artifacts(output_dir: Path, artifacts: Dict[str, Any]) -> List[Path]:
    written: List[Path] = []
    for name, content in artifacts.items():
        path = output_dir / name
        if isinstance(content, str):
            write_text(path, content if content.endswith("\n") else content + "\n")
        else:
            dump_yaml(path, content)
        written.append(path)
    return written


def _load_optional_catalog(
    spec: Dict[str, Any],
    explicit: Optional[str],
    spec_path: Optional[Path] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[Path]]:
    """The source catalog, from an explicit path, the specification's folder, or nowhere.

    Searched in that order, so a catalog beside the specification wins over a
    catalog in the project root: compiling `--spec input/other.yaml` should pick
    up its own catalog, not the root one that belongs to a different
    specification.

    A specification can also name its own catalog with a top-level ``catalog:``
    key, and that is checked **before** any file search. Without it a project
    holding several specifications -- `hr-spec.yaml` beside `catalog_hr.yaml`,
    say -- needs every invocation to repeat `--catalog`, and forgetting it produces
    zero columns and therefore a blocked job, with the real cause one command-line
    flag away.

    It is still only an *input*. Without one, a wildcard scope cannot be
    enumerated and the compiler reports that rather than compiling a subset.
    """
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise SpecReadError(
                f"Catalog not found: {path}\n"
                "  --catalog was given explicitly, so this is not searched for elsewhere."
            )
        return load_yaml(path), path

    # A catalog the specification declares for itself. Resolved relative to the
    # specification when it is a bare filename, so `catalog: catalog_hr.yaml`
    # means the file beside the specification.
    declared = spec.get("catalog")
    if isinstance(declared, dict) and declared.get("tables"):
        return declared, None
    if isinstance(declared, str) and declared:
        declared_path = Path(declared).expanduser()
        if not declared_path.is_absolute() and spec_path is not None:
            beside = spec_path.parent / declared_path
            if beside.is_file():
                declared_path = beside
        if declared_path.is_file():
            return load_yaml(declared_path), declared_path
        raise SpecReadError(
            f"Catalog not found: {declared_path}\n"
            f"  spec.catalog names `{declared}`, so it is not searched for elsewhere."
        )

    candidates: List[Path] = []
    if spec_path is not None:
        base = spec_path.parent
        # `catalog.yaml`, then `<spec-stem>_catalog.yaml`, then
        # `catalog_<spec-stem>.yaml`. The last two let a project keep one catalog
        # per specification without either naming it in the spec or repeating
        # --catalog on every command line.
        candidates += [
            base / "catalog.yaml",
            base / "catalog.yml",
            base / f"{spec_path.stem}_catalog.yaml",
            base / f"catalog_{spec_path.stem}.yaml",
        ]
    candidates += [DEFAULT_CATALOG, Path("input/catalog.yaml"), Path("catalog.yaml")]
    for candidate in candidates:
        if candidate.is_file():
            return load_yaml(candidate), candidate
    return None, None


def catalog_schemas(spec: Dict[str, Any]) -> set:
    """Every *concrete* schema the specification mentions, so a foreign catalog is ignored.

    Auto-discovering `input/catalog.yaml` is convenient, but a catalog for SHOP
    applied to an HR specification produces a wall of "table not found" findings
    that look like broken rules and are actually a mismatched input. Filtering by
    schema turns that into one clear diagnostic.

    A wildcard is **not** a schema, so it is not collected. This is the whole
    reason a catalog survives when the specification says `schema: "*"`: a
    wildcard scope means every schema the catalog has, and collecting the literal
    text `"*"` as a wanted name would leave the set as `{"*"}`, match no real
    schema, and throw the entire catalog away — leaving the planner with nothing
    to enumerate and the run silently empty. An empty result here means "no
    schema is named concretely, so the catalog is not filtered", which is what a
    wildcard scope intends.
    """
    schemas: set = set()
    for entry in (spec.get("scope") or {}).get("include") or []:
        if entry.get("schema"):
            schemas.add(str(entry["schema"]).upper())
    for entry in (spec.get("scope") or {}).get("exclude") or []:
        if entry.get("schema"):
            schemas.add(str(entry["schema"]).upper())
    for section in ("rules", "overrides"):
        for rule in spec.get(section) or []:
            schema = (rule.get("match") or {}).get("schema")
            if schema and "*" not in str(schema):
                schemas.add(str(schema).upper())
    return {schema for schema in schemas if "*" not in schema}


def filter_catalog_to_spec(catalog: Catalog, spec: Dict[str, Any], diags: Diagnostics) -> Catalog:
    """Drop catalog tables the specification never mentions."""
    if catalog.empty:
        return catalog
    wanted = catalog_schemas(spec)
    if not wanted:
        return catalog

    kept = [table for table in catalog.tables if table.schema.upper() in wanted]
    dropped = [f"{t.schema}.{t.name}" for t in catalog.tables if t.schema.upper() not in wanted]

    if dropped and not kept:
        diags.add(
            "CATALOG_SCHEMA_NOT_IN_SCOPE",
            "EDGE",
            f"the supplied catalog describes {sorted({t.schema for t in catalog.tables})}, but this "
            f"specification is about {sorted(wanted)}. The catalog is ignored; pass --catalog to "
            "override this",
        )
        return Catalog(tables=[], source=catalog.source)

    if dropped:
        diags.add(
            "CATALOG_TABLES_OUT_OF_SCOPE",
            "INFO",
            f"{len(dropped)} catalog table(s) outside this specification's schemas were ignored",
            tables=sorted(dropped)[:20],
        )

    return Catalog(tables=kept, source=catalog.source)


def summarize(result: Dict[str, Any], output_dir: Path, written: List[Path]) -> None:
    """Print what was produced, and nothing else.

    The console is a receipt, not a report. Whether a rule compiled, what a
    blocking finding said, which assumptions were made and which value states were
    handled are questions the specification's owner answers -- by running the checks
    in `duckdb.yaml`, not by reading compiler output. So this reports the files,
    their sizes and the job counts, and stops.

    The one thing it will not do is stay silent about a *generated file* that failed
    to re-validate. That is a statement about the compiler's own output rather than
    about the specification, and shipping a file it could not read back is exactly
    the failure mode both files exist to prevent.
    """
    jobs: List[CompiledJob] = result["jobs"]
    plans: List[DuckDBJob] = result["duckdb_plans"]
    validation = result["validation"]
    duckdb_validation = result["duckdb_validation"]
    runnable = len([job for job in jobs if job.query is not None])

    print(f"{TOOL_NAME} {TOOL_VERSION}")
    print()
    print("  spec        : " + str(result.get("spec_path")))
    print(f"  catalog     : {result.get('catalog_path') or 'none supplied'}"
          f" ({len(result['catalog'].tables)} table(s))")
    print("  output      : " + str(output_dir))
    print()
    print(f"  {SEATUNNEL_CONF_NAME:<15}{result['seatunnel_jobs']:>3} jobs"
          f"   {sum(len(job.columns) for job in jobs):>3} columns   "
          f"{_size(written, SEATUNNEL_CONF_NAME)}")
    print(f"  {DUCKDB_JOBS_NAME:<15}{len(plans):>3} jobs"
          f"   {result['duckdb_checks']:>3} checks   "
          f"{_size(written, DUCKDB_JOBS_NAME)}")
    print()
    print(f"  {runnable} of {len(jobs)} target relations compiled")

    failed = [
        failure["problem"]
        for report in (validation, duckdb_validation)
        for failure in report.get("failures") or []
    ]
    if failed:
        print()
        print(f"  {len(failed)} statement(s) did not survive re-validation:")
        for problem in failed[:10]:
            print(f"    - {problem}")


def _size(written: List[Path], name: str) -> str:
    """The size of one written file, or a dash when it is not there."""
    for path in written:
        if path.name == name:
            return f"{path.stat().st_size:>7,} bytes"
    return "  absent"


# ---------------------------------------------------------------------------
# Rulebook -- the vocabularies above, as a file a deployment ships
# ---------------------------------------------------------------------------
#
# A vocabulary hardcoded in source is a list nobody can change without a
# rebuild, and -- worse -- one nobody notices has gone stale. AGENT.md's rule
# that a stale hand-written list silently governing a migration is worse than
# a redundant one applies to rulebooks exactly as it applies to a catalog, so
# the sets and maps in "Vocabulary" are the *built-in defaults* and
# `config/rules.yaml` is the rulebook.
#
# Resolution order, once, at startup:
#
#   1. $TRANSPILER_RULES_FILE, when set -- an explicit path. Its not existing
#      is an error rather than a fallback, for the same reason an explicit
#      --catalog is never searched for elsewhere: a typo must be reported, not
#      quietly answered with a different set of rules than the operator asked
#      for.
#   2. PROJECT_ROOT/config/rules.yaml, when that file is present.
#   3. the built-in defaults, so that deleting the config leaves a working
#      tool rather than a crash -- the defaults are complete.
#
# A file that *is* present but incomplete, mistyped or malformed raises
# RuleBookError and never falls back. Compiling against the defaults while a
# rulebook the deployment believes it shipped sits unread would produce
# artifacts governed by vocabulary nobody approved -- the very failure this
# file exists to prevent, arriving through the loader itself.
#
# Read sites consult the loaded rulebook at run time without naming it:
# `load_rulebook` replaces the module globals, and every `in VALUE_OPS`,
# `sorted(KNOWN_CATEGORIES)` and `ORACLE_TO_PG.get(...)` further down is looked
# up when the code runs, not when it is defined.


class RuleBookError(Exception):
    """The rulebook file exists but cannot be used.

    Raised for every shape or type problem in a rulebook, and for an explicit
    path that is not there, so a bad file is named once, by the loader, with
    the file and the key at fault. The alternatives are worse: a KeyError from
    a read site names no file, and a silent fallback names no rulebook at all.
    """


#: The rulebook a deployment ships, and the variable that overrides where it
#: is read from. Like every other default in this module the path is anchored
#: to the project, not the working directory: this is the project's rulebook,
#: and where the command was typed must not change which rules govern it.
DEFAULT_RULEBOOK = PROJECT_ROOT / "config" / "rules.yaml"
RULEBOOK_ENV_VAR = "TRANSPILER_RULES_FILE"

#: The rulebook's schema: one key per vocabulary, and how each must be shaped.
#:
#: `set` keys are lists in the file -- YAML has no set type -- and load as
#: Python sets, which is what the read sites' `in`, `|` and `sorted` expect.
#: `map` keys are string-to-string mappings. `categories` carries its own
#: shape, category -> list of operations, and is checked separately because it
#: has to agree with `known_categories`.
_RULEBOOK_SET_KEYS = (
    "supported_schema_versions",
    "known_categories",
    "value_ops",
    "derived_ops",
    "drop_ops",
    "structural_ops",
    "snapshot_only_categories",
    "elsewhere_handled_categories",
    "pg_reserved_words",
    "duckdb_unbindable_oracle_functions",
)
_RULEBOOK_MAP_KEYS = (
    "predicate_operators",
    "arithmetic_operators",
    "pivot_aggregates",
    "join_types",
    "oracle_to_pg",
    "duckdb_hash_by_oracle_algorithm",
)
_RULEBOOK_KEYS = frozenset(_RULEBOOK_SET_KEYS) | frozenset(_RULEBOOK_MAP_KEYS) | {"categories"}


def _copy_rulebook(book: Dict[str, Any]) -> Dict[str, Any]:
    """Fresh containers for every vocabulary in `book`.

    Fresh because the loaded book is installed by replacing module globals and
    because self_test reloads: a caller that edited a returned set would
    otherwise be editing the defaults every later load falls back to.
    """
    copied: Dict[str, Any] = {key: set(book[key]) for key in _RULEBOOK_SET_KEYS}
    copied.update({key: dict(book[key]) for key in _RULEBOOK_MAP_KEYS})
    copied["categories"] = {name: set(ops) for name, ops in book["categories"].items()}
    return copied


def _validate_rulebook(book: Any, source: str) -> Dict[str, Any]:
    """Check a rulebook's shape and types, and return it normalised.

    Normalising here -- lists to sets, one shape per vocabulary -- is what lets
    the read sites stay as they are: `operation in VALUE_OPS` wants a set,
    `ORACLE_TO_PG.get` wants a mapping. Every failure raises RuleBookError
    naming the file and the key, because a wrong type discovered later would
    surface as a KeyError from a read site that knows nothing about rulebooks,
    and a missing key would surface as a vocabulary silently reverted to the
    built-in default.
    """
    if not isinstance(book, dict):
        kind = type(book).__name__ if book is not None else "an empty document"
        raise RuleBookError(
            f"{source}: expected a mapping of vocabulary names at the document root, "
            f"found {kind}.\n"
            "  A rulebook is a mapping -- one key per vocabulary -- so it must start\n"
            "  with a key such as `known_categories:` at column zero."
        )

    missing = sorted(key for key in _RULEBOOK_KEYS if key not in book)
    if missing:
        raise RuleBookError(
            f"{source}: missing {missing}.\n"
            "  A rulebook is complete. A vocabulary this file does not carry would fall\n"
            "  back to the built-in default, and the deployment would ship a rulebook\n"
            "  that does not say what it means."
        )

    # Checked before the two lists below are sorted: a scalar key of any other
    # type would make `sorted` compare a str with an int, which is the kind of
    # exception this loader exists to prevent.
    odd_keys = [key for key in book if not isinstance(key, str)]
    if odd_keys:
        raise RuleBookError(
            f"{source}: keys must be strings, found "
            f"{[type(key).__name__ for key in odd_keys]}."
        )

    unknown = sorted(key for key in book if key not in _RULEBOOK_KEYS)
    if unknown:
        raise RuleBookError(
            f"{source}: {unknown} {'is' if len(unknown) == 1 else 'are'} not a vocabulary "
            f"this compiler reads.\n"
            f"  Known keys: {sorted(_RULEBOOK_KEYS)}.\n"
            "  An unknown key is a typo, and the vocabulary it was meant to override\n"
            "  would go on governing the migration unnoticed."
        )

    normalised: Dict[str, Any] = {}
    for key in _RULEBOOK_SET_KEYS:
        value = book[key]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            detail = (
                f" with entries {sorted({type(item).__name__ for item in value})}"
                if isinstance(value, list)
                else f": {value!r}"[:120]
            )
            raise RuleBookError(
                f"{source}: `{key}` must be a list of strings, found "
                f"{type(value).__name__}{detail}."
            )
        normalised[key] = set(value)

    for key in _RULEBOOK_MAP_KEYS:
        value = book[key]
        if not isinstance(value, dict) or any(
            not isinstance(map_key, str) or not isinstance(map_value, str)
            for map_key, map_value in (value.items() if isinstance(value, dict) else ())
        ):
            raise RuleBookError(
                f"{source}: `{key}` must be a mapping of strings to strings, found "
                f"{type(value).__name__}."
            )
        normalised[key] = dict(value)

    categories = book["categories"]
    if not isinstance(categories, dict):
        raise RuleBookError(
            f"{source}: `categories` must be a mapping of category -> list of operations, "
            f"found {type(categories).__name__}."
        )
    for name, operations in categories.items():
        if not isinstance(name, str) or name not in normalised["known_categories"]:
            raise RuleBookError(
                f"{source}: `categories` names `{name}`, which is not in "
                f"`known_categories`.\n"
                "  A specification cannot declare that category, so an operations list for\n"
                "  it could never govern a step -- which means it is a typo, not a rule."
            )
        if not isinstance(operations, list) or any(not isinstance(op, str) for op in operations):
            raise RuleBookError(
                f"{source}: `categories.{name}` must be a list of strings, found "
                f"{type(operations).__name__}."
            )
    absent = sorted(category for category in _GATED_CATEGORIES if category not in categories)
    if absent:
        raise RuleBookError(
            f"{source}: `categories` has no entry for {absent}.\n"
            "  Those are the categories `_validate_step` gates by operation. Omitting one\n"
            "  would not relax its rule, it would switch the check off, and an\n"
            "  unimplemented operation would then reach the compiler instead of blocking."
        )

    normalised["categories"] = {name: set(ops) for name, ops in categories.items()}
    return normalised


def _resolve_rulebook_path(explicit: Optional[Path] = None) -> Optional[Path]:
    """Where to read the rulebook from, or None when there is no file to read.

    None means *nothing was asked for*: the built-in defaults then stand. A
    file that was asked for and is not there raises, because an explicit
    rulebook is a deployment decision and a decision that cannot be found is
    an error rather than an invitation to compile against something else.
    """
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise RuleBookError(f"the rulebook {path} is not a file.")
        return path

    configured = os.environ.get(RULEBOOK_ENV_VAR)
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise RuleBookError(
                f"{RULEBOOK_ENV_VAR} points at {path}, which is not a file.\n"
                "  An explicit rulebook is never replaced by another one. Fix the path, or\n"
                "  unset the variable to read config/rules.yaml instead."
            )
        return path

    if DEFAULT_RULEBOOK.is_file():
        return DEFAULT_RULEBOOK
    return None


def load_rulebook(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load a rulebook, install it as this module's vocabularies, return it.

    This is what "loaded dynamically at startup" means: the install below
    replaces every vocabulary global, so from this call on the read sites
    consult this file. With no argument the resolution order above applies;
    `path` is for a caller that knows exactly which rulebook it wants --
    self_test proves an override with one.
    """
    global _RULEBOOK_ERROR

    resolved = _resolve_rulebook_path(path)
    if resolved is None:
        book = _copy_rulebook(_BUILTIN_RULEBOOK)
    else:
        try:
            text = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise RuleBookError(
                f"{resolved} is not valid UTF-8 ({exc}). Re-save it as UTF-8; the "
                "vocabularies and the files this produces are UTF-8."
            ) from None
        except OSError as exc:
            raise RuleBookError(f"{resolved} could not be read: {exc}") from None

        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            # The specification reader's diagnosis, reused on purpose: it names
            # the line, shows the offending text, and recognises the accidents a
            # copy through a reformatting tool leaves behind -- which is what a
            # hand-edited rulebook hits too. A raw scanner message would point at
            # wherever parsing stopped rather than at what is wrong.
            raise RuleBookError(
                "the rulebook could not be parsed, and a rulebook that cannot be read\n"
                "is not replaced by the built-in defaults:\n\n"
                f"{_describe_yaml_failure(resolved, text, exc)}"
            ) from None

        book = _validate_rulebook(raw, str(resolved))

    _install_rulebook(book)
    _RULEBOOK_ERROR = None
    return book


def _install_rulebook(book: Dict[str, Any]) -> None:
    """Point every vocabulary name in this module at one loaded rulebook.

    The names are read when the code runs, not when it is defined, so one
    assignment per vocabulary is the whole of "read sites consult the loaded
    rulebook at runtime": all thirty-odd of them -- membership tests,
    `sorted(...)`, `.get(...)` -- follow the file from the next call on,
    without any of them naming a path.
    """
    global _RULEBOOK, SUPPORTED_SCHEMA_VERSIONS, KNOWN_CATEGORIES
    global SNAPSHOT_ONLY_CATEGORIES, ELSEWHERE_HANDLED_CATEGORIES
    global VALUE_OPS, DERIVED_OPS, DROP_OPS, STRUCTURAL_OPS, CATEGORY_OPERATIONS
    global PREDICATE_OPERATORS, ARITHMETIC_OPERATORS, PIVOT_AGGREGATES, JOIN_TYPES
    global ORACLE_TO_PG, PG_RESERVED_WORDS, DUCKDB_UNBINDABLE_ORACLE_FUNCTIONS
    global DUCKDB_HASH_BY_ORACLE_ALGORITHM

    _RULEBOOK = book
    SUPPORTED_SCHEMA_VERSIONS = book["supported_schema_versions"]
    KNOWN_CATEGORIES = book["known_categories"]
    SNAPSHOT_ONLY_CATEGORIES = book["snapshot_only_categories"]
    ELSEWHERE_HANDLED_CATEGORIES = book["elsewhere_handled_categories"]
    VALUE_OPS = book["value_ops"]
    DERIVED_OPS = book["derived_ops"]
    DROP_OPS = book["drop_ops"]
    STRUCTURAL_OPS = book["structural_ops"]
    CATEGORY_OPERATIONS = book["categories"]
    PREDICATE_OPERATORS = book["predicate_operators"]
    ARITHMETIC_OPERATORS = book["arithmetic_operators"]
    PIVOT_AGGREGATES = book["pivot_aggregates"]
    JOIN_TYPES = book["join_types"]
    ORACLE_TO_PG = book["oracle_to_pg"]
    PG_RESERVED_WORDS = book["pg_reserved_words"]
    DUCKDB_UNBINDABLE_ORACLE_FUNCTIONS = book["duckdb_unbindable_oracle_functions"]
    DUCKDB_HASH_BY_ORACLE_ALGORITHM = book["duckdb_hash_by_oracle_algorithm"]


def active_rules() -> Dict[str, Any]:
    """The loaded rulebook as plain, JSON-safe data.

    The one symbol another module may import: GET /rules serves this. Sets
    become sorted lists so `json.dumps` takes it directly, and the result is a
    copy, so a caller cannot edit the vocabulary this compiler runs with. A
    rulebook that failed to load raises instead of exporting the built-in
    defaults -- an endpoint called /rules must not answer with rules the
    deployment never shipped.
    """
    if _RULEBOOK_ERROR is not None:
        raise _RULEBOOK_ERROR
    if _RULEBOOK is None:  # pragma: no cover - startup records an error instead
        raise RuleBookError("no rulebook has been loaded")
    exported: Dict[str, Any] = {key: sorted(_RULEBOOK[key]) for key in _RULEBOOK_SET_KEYS}
    exported.update({key: dict(_RULEBOOK[key]) for key in _RULEBOOK_MAP_KEYS})
    exported["categories"] = {name: sorted(ops) for name, ops in _RULEBOOK["categories"].items()}
    return exported


# Captured here, where every vocabulary is still the built-in default and
# before the first load can replace it: once `load_rulebook` has run, the names
# above hold a file's values, and a capture taken then would copy the previous
# rulebook over itself -- the built-in default would stop being built in.
_BUILTIN_RULEBOOK = _copy_rulebook(
    {
        "supported_schema_versions": SUPPORTED_SCHEMA_VERSIONS,
        "known_categories": KNOWN_CATEGORIES,
        "value_ops": VALUE_OPS,
        "derived_ops": DERIVED_OPS,
        "drop_ops": DROP_OPS,
        "structural_ops": STRUCTURAL_OPS,
        "snapshot_only_categories": SNAPSHOT_ONLY_CATEGORIES,
        "elsewhere_handled_categories": ELSEWHERE_HANDLED_CATEGORIES,
        "predicate_operators": PREDICATE_OPERATORS,
        "arithmetic_operators": ARITHMETIC_OPERATORS,
        "pivot_aggregates": PIVOT_AGGREGATES,
        "join_types": JOIN_TYPES,
        "oracle_to_pg": ORACLE_TO_PG,
        "pg_reserved_words": PG_RESERVED_WORDS,
        "duckdb_unbindable_oracle_functions": DUCKDB_UNBINDABLE_ORACLE_FUNCTIONS,
        "duckdb_hash_by_oracle_algorithm": DUCKDB_HASH_BY_ORACLE_ALGORITHM,
        "categories": CATEGORY_OPERATIONS,
    }
)

#: The rulebook in force, as sets and mappings. `active_rules()` is its
#: JSON-safe view; None only until the first successful load.
_RULEBOOK: Optional[Dict[str, Any]] = None

#: Recorded when a rulebook that exists could not be used, and cleared by the
#: next successful load. The module still imports with it set, so the failure
#: can be reported *by name* from the entry points -- `main` before anything
#: compiles, `active_rules` before anything is served -- instead of as a
#: traceback in the middle of an import, which would take the whole tool down
#: without saying which file was at fault.
_RULEBOOK_ERROR: Optional[RuleBookError] = None

try:
    load_rulebook()
except RuleBookError as exc:
    _RULEBOOK_ERROR = exc


# ---------------------------------------------------------------------------
# Service configuration
# ---------------------------------------------------------------------------
#
# config/config.yaml is this tool's own service configuration: where a
# compiled bundle is stored, how long it survives, and where the HTTP front
# door listens. Like the rulebook it lives under config/, is read by THIS
# module, and is never read anywhere else -- src/api.py asks this module for
# it instead of opening the file itself, so one loader, one validation and
# one error message cover both halves of the tool. That is what "the config
# file belongs to the transpiler" means in practice: the service has
# preferences, the transpiler has the file.
#
# Resolution order, on every call:
#
#   1. $TRANSPILER_CONFIG_FILE, when set -- an explicit path that is not a
#      file is an error, for the same reason the rulebook's env var and an
#      explicit --catalog are never quietly replaced by something else.
#   2. PROJECT_ROOT/config/config.yaml, when present.
#   3. the built-in defaults below, so deleting the file leaves a working
#      tool rather than a crash.
#
# A file that exists but is malformed, mistyped or carries an unknown key
# raises ConfigError and never falls back -- the rulebook's argument once
# more: a deployment that shipped a config believes it is in force, and a
# TTL or a port silently taken from defaults instead is the stale-file
# failure arriving through the loader.
#
# The asymmetry with the rulebook is deliberate. A rulebook must be
# COMPLETE -- it is a vocabulary, and a missing entry would silently change
# what compiles -- so its loader rejects an incomplete file. A config may be
# partial: each key has exactly one meaning, so a key the file omits is a
# request for the documented default, not a gap. Unknown keys are errors in
# both files, for the same reason: a typo must not be ignored.
#
# Read fresh on every `app_config()` call rather than once at import, also
# deliberately. Config values govern runtime behaviour (TTL, storage path,
# port), and an operator who fixes a file should not have to restart a
# server for it to take effect -- which is also what makes the loader
# testable against a live service. The rulebook cannot work that way: it IS
# the module globals. The config has no globals to swap, and reading one
# small YAML per request is cheaper than the restart it saves.


class ConfigError(Exception):
    """The config file exists but cannot be used.

    Raised for every shape or type problem, for an unknown key, and for an
    explicit path that is not there -- always naming the file and the key at
    fault, because the alternatives name neither: a KeyError from a read site
    knows nothing about config files, and a silent fallback means the
    operator's settings never took effect without anything saying so.
    """


#: Where the config is read from, and the variable that overrides it.
#: Anchored to the project like every other default here: this is the
#: project's config, so where a command was typed must not change which
#: settings govern it.
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "config.yaml"
CONFIG_ENV_VAR = "TRANSPILER_CONFIG_FILE"

#: The complete configuration, and the shape every file is checked against.
#: These values are what a deleted config falls back to, so they must be
#: the ones a bare install is meant to run with -- `config/config.yaml`
#: ships as exactly this, and a partial file merges over the same table.
_DEFAULT_CONFIG: Dict[str, Any] = {
    "storage": {
        # Where persisted bundles live, relative to the project root when not
        # absolute -- the same anchoring every default in this module has, so
        # a server started from another directory still stores and sweeps the
        # one folder its callers are told about.
        "dir": "storage",
        # Seconds a compiled bundle survives. Folder age is judged against
        # this at read time, so changing it in the file applies to folders
        # already on disk -- unlike the rulebook vocabularies, config has no
        # state to re-time.
        "ttl_seconds": 900,
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8000,
    },
}


def _copy_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Fresh nested containers, so a caller cannot edit the defaults.

    The same reason `_copy_rulebook` exists: the default table is module
    state, and self_test reloads -- a caller that sets `config["server"]
    ["port"]` on one result must not be editing what every later load falls
    back to.
    """
    return {section: dict(values) for section, values in config.items()}


def _validate_config(value: Any, source: str) -> Dict[str, Any]:
    """Check a config's shape and types, merge it over the defaults, return it.

    Every failure raises ConfigError naming the file and the key, because a
    wrong type found later would surface as a KeyError from a read site that
    knows nothing about config files, and an unknown key would be ignored --
    which is exactly the outcome an operator who mistyped a setting must
    never get: their intended value quietly not in force.
    """
    if not isinstance(value, dict):
        kind = type(value).__name__ if value is not None else "an empty document"
        raise ConfigError(
            f"{source}: expected a mapping of settings at the document root, "
            f"found {kind}.\n"
            "  A config is a mapping -- one key per section -- so it must start\n"
            "  with a key such as `storage:` at column zero."
        )

    odd_keys = [key for key in value if not isinstance(key, str)]
    if odd_keys:
        raise ConfigError(
            f"{source}: keys must be strings, found "
            f"{[type(key).__name__ for key in odd_keys]}."
        )

    unknown = sorted(set(value) - set(_DEFAULT_CONFIG))
    if unknown:
        raise ConfigError(
            f"{source}: {unknown} {'is' if len(unknown) == 1 else 'are'} not a section "
            f"this tool reads.\n"
            f"  Known sections: {sorted(_DEFAULT_CONFIG)}.\n"
            "  An unknown section is a typo, and the setting it was meant to change\n"
            "  would go on being the default, unnoticed."
        )

    normalised = _copy_config(_DEFAULT_CONFIG)

    for section, defaults in _DEFAULT_CONFIG.items():
        supplied = value.get(section, {})
        if not isinstance(supplied, dict):
            raise ConfigError(
                f"{source}: `{section}` must be a mapping of settings, found "
                f"{type(supplied).__name__}."
            )
        odd = [key for key in supplied if not isinstance(key, str)]
        if odd:
            raise ConfigError(
                f"{source}: `{section}` keys must be strings, found "
                f"{[type(key).__name__ for key in odd]}."
            )
        section_unknown = sorted(set(supplied) - set(defaults))
        if section_unknown:
            raise ConfigError(
                f"{source}: {section_unknown} "
                f"{'is' if len(section_unknown) == 1 else 'are'} not a setting under "
                f"`{section}`.\n"
                f"  Known keys: {sorted(defaults)}.\n"
                "  An unknown key is a typo, and the setting it was meant to change\n"
                "  would go on being the default, unnoticed."
            )
        normalised[section].update(supplied)

    # Type checks last, against the merged values, so a key omitted from the
    # file is validated too -- the defaults must be held to the same
    # contract a written-down value is, or a bad default would only ever
    # fail in the field.
    ttl = normalised["storage"]["ttl_seconds"]
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
        raise ConfigError(
            f"{source}: `storage.ttl_seconds` must be a positive whole number of "
            f"seconds, found {ttl!r}."
        )
    directory = normalised["storage"]["dir"]
    if not isinstance(directory, str) or not directory.strip():
        raise ConfigError(
            f"{source}: `storage.dir` must be a non-empty path, found {directory!r}."
        )
    host = normalised["server"]["host"]
    if not isinstance(host, str) or not host.strip():
        raise ConfigError(
            f"{source}: `server.host` must be a non-empty hostname, found {host!r}."
        )
    port = normalised["server"]["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError(
            f"{source}: `server.port` must be a whole number between 1 and 65535, "
            f"found {port!r}."
        )
    return normalised


def _resolve_config_path(explicit: Optional[Path] = None) -> Optional[Path]:
    """Where to read the config from, or None when there is no file to read.

    None means *nothing was asked for*: the defaults then stand. A file that
    was asked for and is not there raises -- an explicit config is a
    deployment decision, and a decision that cannot be found is an error
    rather than an invitation to run on defaults.
    """
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"the config {path} is not a file.")
        return path

    configured = os.environ.get(CONFIG_ENV_VAR)
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise ConfigError(
                f"{CONFIG_ENV_VAR} points at {path}, which is not a file.\n"
                "  An explicit config is never replaced by another one. Fix the path, or\n"
                "  unset the variable to read config/config.yaml instead."
            )
        return path

    if DEFAULT_CONFIG.is_file():
        return DEFAULT_CONFIG
    return None


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Read the config, validate it, return it merged over the defaults.

    With no argument the resolution order above applies; `path` is for a
    caller that knows exactly which config it wants -- self_test proves the
    merge and the rejections with temporary files.
    """
    resolved = _resolve_config_path(path)
    if resolved is None:
        return _copy_config(_DEFAULT_CONFIG)

    try:
        text = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"{resolved} is not valid UTF-8 ({exc}). Re-save it as UTF-8; the config "
            "and the files this produces are UTF-8."
        ) from None
    except OSError as exc:
        raise ConfigError(f"{resolved} could not be read: {exc}") from None

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # The specification reader's diagnosis again, on purpose: it names the
        # line, shows the offending text, and recognises the accidents a copy
        # through a reformatting tool leaves behind -- which is what a
        # hand-edited config hits too.
        raise ConfigError(
            "the config could not be parsed, and a config that cannot be read\n"
            "is not replaced by the defaults:\n\n"
            f"{_describe_yaml_failure(resolved, text, exc)}"
        ) from None

    return _validate_config(raw, str(resolved))


def app_config() -> Dict[str, Any]:
    """The configuration in force, read fresh from disk on every call.

    The one symbol another module may import: src/api.py's storage, TTL and
    server settings all come from here, so the config file belongs to the
    transpiler and the service simply asks. A config that failed to load
    raises ConfigError instead of returning the defaults -- a caller must
    never believe it is running with a file the deployment shipped when it
    is not.
    """
    return load_config()


def main(argv: Optional[Sequence[str]] = None) -> int:
    if _RULEBOOK_ERROR is not None:
        # A rulebook that exists but cannot be used stops the run before
        # anything compiles. Falling back to the built-in defaults here would
        # emit artifacts governed by a vocabulary the deployment believes it
        # replaced -- exactly the stale list AGENT.md calls worse than a
        # redundant one. Exit 2 is the code this module already uses for an
        # input file that could not be read.
        print(f"\n{_RULEBOOK_ERROR}\n", file=sys.stderr)
        print("  No output was written.", file=sys.stderr)
        return 2

    try:
        # Every entry point reads this tool's own input files before acting.
        # The CLI consumes none of these settings today, but a broken config
        # must be named by the entry point an operator actually ran -- with
        # the file and the key at fault -- rather than first surfacing as a
        # 500 from a later HTTP request against the same file.
        app_config()
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        print("  No output was written.", file=sys.stderr)
        return 2

    try:
        return _main(argv)
    except SpecReadError as exc:
        # A specification that cannot be read is the reader's problem to fix, not a
        # crash. One clear paragraph naming the cause beats a scanner traceback.
        print(f"\n{exc}\n", file=sys.stderr)
        print("  No output was written.", file=sys.stderr)
        return 2
    except RuleBookError as exc:
        # Same treatment for a rulebook: it is an input like the specification,
        # so an unreadable one is a message and an exit code, never a traceback.
        print(f"\n{exc}\n", file=sys.stderr)
        print("  No output was written.", file=sys.stderr)
        return 2
    except ConfigError as exc:
        # Belt and braces: the pre-flight check above is where a broken config
        # is expected to surface, but one raised mid-run gets the same
        # treatment -- a message and exit 2, never a traceback.
        print(f"\n{exc}\n", file=sys.stderr)
        print("  No output was written.", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"\n  File not found: {exc.filename or exc}\n", file=sys.stderr)
        return 2


def _main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="transpiler",
        description=(
            "Compile an approved migration specification into two files: output/seatunnel.conf, one "
            "Apache SeaTunnel HOCON file carrying every source query, target query, DDL and DML "
            "command and schema_save_mode; and output/duckdb.yaml, one DuckDB file carrying the "
            "validation rules the specification implies, the compiled query for each of them, and "
            "the source queries they compare against."
        ),
    )
    parser.add_argument(
        "--spec",
        default=str(DEFAULT_SPEC),
        help=(
            "the approved migration specification. Defaults to the project's own "
            "input/hr-spec.yaml, so the command behaves the same wherever it "
            "is run from"
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="directory for the two generated files. Defaults to the project's own output/.",
    )
    parser.add_argument(
        "--catalog",
        default=None,
        help=(
            "source catalog describing the Oracle tables. Required to expand wildcard scope such as "
            "SHOP.* and wildcard column rules. Defaults to the project's own input/catalog.yaml "
            "when that file exists."
        ),
    )
    parser.add_argument("--self-test", action="store_true", help="run the built-in smoke test and exit")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    spec_path = Path(args.spec).expanduser()

    if not spec_path.is_file():
        print(f"\n  Specification not found: {spec_path}", file=sys.stderr)
        print(f"  The default is {DEFAULT_SPEC}", file=sys.stderr)
        print("  Pass --spec to compile a different one.\n", file=sys.stderr)
        return 2

    # One specification, one folder, named after the file. Compiling a second
    # specification therefore cannot overwrite the first one's output, which is
    # what makes "give me a new spec" safe to repeat.
    output_dir = Path(args.output_dir).expanduser() / output_folder_name(spec_path)

    spec = load_yaml(spec_path)
    catalog, catalog_path = _load_optional_catalog(spec, args.catalog, spec_path)

    result = compile_all(spec, spec_path, catalog, catalog_path)
    result["spec_path"] = str(spec_path)

    written = write_artifacts(output_dir, result["artifacts"])
    summarize(result, output_dir, written)

    # The only thing that changes the exit code is whether a *generated file*
    # survived being read back and re-parsed. Whether the specification compiled
    # cleanly is DuckDB's question, not this program's: a rule the compiler could
    # not express is already carried into duckdb.yaml as an assumption or a
    # governance check, where the owner will meet it.
    if not result["validation"]["ok"] or not result["duckdb_validation"]["ok"]:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Built-in smoke test
# ---------------------------------------------------------------------------

def self_test() -> int:
    """Prove the dialects round-trip, without needing an input specification."""
    if not SQLGLOT_AVAILABLE:
        print("self-test needs sqlglot installed")
        return 2

    cases = [
        (
            "flashback with a table alias",
            f'SELECT "A" FROM "S"."T" {flashback_clause()} AS t',
            SCN_BIND,
        ),
        (
            "flashback across a join",
            f'SELECT t."A" FROM "S"."T" {flashback_clause()} AS t '
            f'LEFT JOIN "S"."B" {flashback_clause()} AS b ON t."K" = b."K"',
            SCN_BIND,
        ),
        (
            "spec types survive",
            'SELECT CAST("A" AS varchar(160)) AS a, CAST("B" AS numeric(14,4)) AS b FROM "S"."T"',
            "NUMERIC(14, 4)",
        ),
        (
            "timestamptz does not double-suffix",
            'SELECT CAST("C" AS timestamptz) AS c FROM "S"."T"',
            "TIMESTAMP WITH TIME ZONE",
        ),
        (
            "regexp-extract is Oracle-shaped",
            "SELECT REGEXP_SUBSTR(\"X\", 'ORD-[0-9]{6}', 1, 0) AS r FROM \"S\".\"T\"",
            "REGEXP_SUBSTR",
        ),
    ]

    failures = 0
    for label, sql, expected in cases:
        rendered, glot = render_ctunnel(sql)
        ok = rendered is not None and expected in rendered and glot.get("stable") == "PASS"
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            failures += 1
            print(f"        {glot}")
            print(f"        {rendered}")

    ddl, ddl_glot = render_pg(
        'CREATE TABLE IF NOT EXISTS "shop"."orders" ("id" numeric(12,0) NOT NULL, '
        '"t" timestamptz, CONSTRAINT "pk" PRIMARY KEY ("id")) PARTITION BY RANGE ("t")'
    )
    ok = ddl is not None and ddl_glot.get("stable") == "PASS"
    print(f"  {'PASS' if ok else 'FAIL'}  target DDL round-trips in pgcontract")
    if not ok:
        failures += 1
        print(f"        {ddl_glot}")

    conf = hocon_block("env", [("parallelism", 4), ("job.mode", "BATCH")])
    reparsed = parse_hocon(conf)
    ok = reparsed.get("env", {}).get("parallelism") == 4 and reparsed["env"]["job.mode"] == "BATCH"
    print(f"  {'PASS' if ok else 'FAIL'}  HOCON writer and reader agree")
    if not ok:
        failures += 1
        print(f"        {reparsed}")

    # DuckDB translation. These exist because a dialect switch is not enough:
    # SQLGlot carries REGEXP_SUBSTR and STANDARD_HASH through to DuckDB verbatim,
    # so without the rewrite the plan parses and then fails to bind at run time.
    duckdb_diags = Diagnostics()
    translated, duck_glot = render_duckdb(
        'SELECT REGEXP_SUBSTR("N", \'ORD-[0-9]{6}\', 1, 0) AS "r", '
        'STANDARD_HASH("E", \'SHA256\') AS "h" '
        f'FROM "S"."T" {flashback_clause()} AS t',
        duckdb_diags,
    )
    ok = (
        translated is not None
        and "REGEXP_EXTRACT" in translated
        and "SHA256" in translated
        and "REGEXP_SUBSTR" not in translated
        and "STANDARD_HASH" not in translated
        and not duck_glot.get("residual")
    )
    print(f"  {'PASS' if ok else 'FAIL'}  Oracle functions are rewritten for DuckDB")
    if not ok:
        failures += 1
        print(f"        {translated}")
        print(f"        {duck_glot}")

    ok = (
        translated is not None
        and "AS OF SCN" not in translated
        and duck_glot.get("flashback_removed") == "1"
    )
    print(f"  {'PASS' if ok else 'FAIL'}  the flashback clause is dropped for DuckDB")
    if not ok:
        failures += 1
        print(f"        {translated}")

    # The residual scan is the only thing that makes the rewrite table
    # trustworthy, so it is proved on a function SQLGlot carries through verbatim
    # and DuckDB has never heard of.
    residual_diags = Diagnostics()
    residual_sql, residual_glot = render_duckdb(
        'SELECT XMLQUERY("X") AS d FROM "S"."T"', residual_diags
    )
    ok = (
        residual_sql is not None
        and "XMLQUERY" in residual_glot.get("residual", "")
        and any(d.code == "DUCKDB_ORACLE_FUNCTION_LEFT" for d in residual_diags.blocking)
    )
    print(f"  {'PASS' if ok else 'FAIL'}  an unbindable Oracle function is reported, not shipped")
    if not ok:
        failures += 1
        print(f"        {residual_glot}")

    # SQLGlot would otherwise degrade TO_CHAR(fmt) to CAST(x AS TEXT): valid,
    # and silently wrong. It has to block instead.
    masked_diags = Diagnostics()
    render_duckdb(
        "SELECT TO_CHAR(\"D\", 'YYYY') AS d FROM \"S\".\"T\"", masked_diags
    )
    ok = (
        any(d.code == "DUCKDB_FORMAT_UNMAPPED" for d in masked_diags.blocking)
        and not any(d.severity == "EDGE" for d in masked_diags.blocking)
    )
    print(f"  {'PASS' if ok else 'FAIL'}  a masked TO_CHAR blocks rather than degrading to a cast")
    if not ok:
        failures += 1

    # A bind with no DuckDB equivalent has to block rather than be pinned.
    unmapped_diags = Diagnostics()
    render_duckdb(
        "SELECT REGEXP_SUBSTR(\"N\", 'p', 2, 1) AS r FROM \"S\".\"T\"", unmapped_diags
    )
    ok = any(d.code == "DUCKDB_FUNCTION_UNMAPPED" for d in unmapped_diags.blocking)
    print(f"  {'PASS' if ok else 'FAIL'}  a partial REGEXP_SUBSTR rewrite blocks")
    if not ok:
        failures += 1

    document = render_yaml_document({"job": {"a": {"check": [{"sql": "SELECT 1"}]}}}, ["header"])
    reparsed = yaml.safe_load(document)
    ok = reparsed["job"]["a"]["check"][0]["sql"] == "SELECT 1" and document.startswith("# header")
    print(f"  {'PASS' if ok else 'FAIL'}  the DuckDB YAML writer and reader agree")
    if not ok:
        failures += 1
        print(f"        {reparsed}")

    # Reading a specification must fail with an explanation, never a traceback.
    # These three are the accidents that actually happen: a tool escaping the
    # comment marker, a reformat that left non-breaking spaces, and a genuine
    # syntax error with nothing to blame.
    import tempfile

    good = 'schemaVersion: "5.1.0"\nrules:\n  - id: a\n    match: { objectClass: table, schema: S, name: T }\n'
    cases = [
        (
            "an escaped comment marker is named, not left to the parser",
            good.replace("rules:", "\\# rules:", 1),
            "escaped comment marker",
        ),
        (
            "non-breaking spaces are named",
            good.replace("rules:", "\u00a0rules:", 1),
            "non-breaking space",
        ),
        (
            "a tab in indentation is named",
            good.replace("  - id: a", "\t- id: a", 1),
            "tab",
        ),
        (
            "a genuine syntax error still reports the line",
            good.replace("rules:", "rules", 1),
            "not valid YAML",
        ),
        (
            "a non-mapping root is rejected with advice",
            "- a\n- b\n",
            "mapping at the document root",
        ),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for label, text, expected in cases:
            path = Path(tmp) / "spec.yaml"
            path.write_text(text, encoding="utf-8")
            try:
                load_yaml(path)
                message = ""
            except SpecReadError as exc:
                message = str(exc)
            except Exception as exc:  # noqa: BLE001
                message = f"WRONG EXCEPTION {type(exc).__name__}: {exc}"
            ok = expected in message
            print(f"  {'PASS' if ok else 'FAIL'}  {label}")
            if not ok:
                failures += 1
                print(f"        expected {expected!r} in: {message[:300] or '<no error raised>'}")

    # The whole planning path, from a one-table specification to two files. This
    # is the only in-module check that exercises `plan_jobs` end to end, which is
    # exactly where a scope that resolves to nothing turns into `jobs: 0`.
    with tempfile.TemporaryDirectory() as tmp:
        catalog_path = Path(tmp) / "catalog.yaml"
        catalog_path.write_text(
            'tables:\n'
            '  - schema: HR\n'
            '    name: ORDERS\n'
            '    primaryKey: [ORDER_ID]\n'
            '    columns:\n'
            '      - { name: ORDER_ID,    type: "NUMBER(10)", nullable: false }\n'
            '      - { name: CUSTOMER_ID, type: "NUMBER(10)", nullable: false }\n'
            '      - { name: ORDER_DATE,  type: "DATE",       nullable: false }\n'
            '      - { name: AMOUNT,      type: "NUMBER(12,2)" }\n'
            '      - { name: STATUS,      type: "VARCHAR2(20)" }\n',
            encoding="utf-8",
        )
        spec_path = Path(tmp) / "hr-spec.yaml"
        spec_path.write_text(
            'schemaVersion: "5.1.0"\n'
            f"catalog: {catalog_path.name}\n"
            "engines:\n"
            "  source: { engine: oracle }\n"
            "  target: { engine: postgresql }\n"
            "scope:\n"
            "  include:\n"
            "    - { objectClass: table, schema: HR, name: ORDERS }\n"
            "rules:\n"
            "  - id: orders-table\n"
            "    match: { objectClass: table, schema: HR, name: ORDERS }\n"
            "    target: { schema: public, table: orders }\n"
            "acceptance:\n"
            "  checks: [row-count-exact]\n"
            "run:\n"
            "  acquisition: snapshot\n"
            "  capture: { startPosition: explicit-scn, scn: 123456789 }\n",
            encoding="utf-8",
        )

        spec = load_yaml(spec_path)
        found, found_path = _load_optional_catalog(spec, None, spec_path)
        ok = found_path is not None
        print(f"  {'PASS' if ok else 'FAIL'}  a catalog named by the specification is found")
        if not ok:
            failures += 1

        catalog = parse_catalog(spec, found, str(found_path or ""))
        catalog = filter_catalog_to_spec(catalog, spec, Diagnostics())
        ok = [t.name for t in catalog.tables] == ["ORDERS"]
        print(f"  {'PASS' if ok else 'FAIL'}  the catalog survives filtering for a concrete schema")
        if not ok:
            failures += 1

        diags = Diagnostics()
        naming = Naming(spec, diags)
        TypeResolver(spec, catalog, diags)
        plans = plan_jobs(spec, catalog, naming, diags)
        ok = len(plans) == 1 and plans[0].target_schema == "public" and plans[0].target_table == "orders"
        print(f"  {'PASS' if ok else 'FAIL'}  one plan for HR.ORDERS -> public.orders")
        if not ok:
            failures += 1
            print(f"        {len(plans)} plan(s): {[(p.job_id, p.target_schema, p.target_table) for p in plans]}")

        built = compile_all(spec, spec_path, found, found_path)
        jobs = built["jobs"]
        ok = len(jobs) == 1 and jobs[0].query is not None
        print(f"  {'PASS' if ok else 'FAIL'}  the job compiles to a query")
        if not ok:
            failures += 1
            for item in built["diagnostics"].blocking:
                print(f"        {item.code}: {item.message[:140]}")

        if jobs and jobs[0].query is not None:
            query = jobs[0].query
            ok = f"AS OF SCN {SCN_BIND}" in query
            print(f"  {'PASS' if ok else 'FAIL'}  the source query is pinned to the SCN bind")
            if not ok:
                failures += 1
                print(f"        {query[:160]}")

            ok = "123456789" not in query
            print(f"  {'PASS' if ok else 'FAIL'}  the configured SCN is not hardcoded into the SQL")
            if not ok:
                failures += 1

            ok = jobs[0].target["schema"] == "public" and jobs[0].target["table"] == "orders"
            print(f"  {'PASS' if ok else 'FAIL'}  the target is public.orders")
            if not ok:
                failures += 1

        conf = built["artifacts"].get(SEATUNNEL_CONF_NAME, "")
        ok = conf.count("_ddl {") == 1 and conf.count("_data {") == 1
        print(f"  {'PASS' if ok else 'FAIL'}  one DDL job and one data job")
        if not ok:
            failures += 1

        ok = "CREATE TABLE IF NOT EXISTS" in conf
        print(f"  {'PASS' if ok else 'FAIL'}  the DDL job carries a CREATE TABLE")
        if not ok:
            failures += 1

        ok = "INSERT INTO" in conf
        print(f"  {'PASS' if ok else 'FAIL'}  the data job carries an INSERT")
        if not ok:
            failures += 1

        ok = built["duckdb_plans"] and built["duckdb_plans"][0].check_count > 0
        print(f"  {'PASS' if ok else 'FAIL'}  one DuckDB job with validation rules")
        if not ok:
            failures += 1

        ok = built["validation"]["ok"] and built["duckdb_validation"]["ok"]
        print(f"  {'PASS' if ok else 'FAIL'}  both artifacts survive re-validation")
        if not ok:
            failures += 1
            print(f"        {built['validation'].get('failures')}")
            print(f"        {built['duckdb_validation'].get('failures')}")

        ok = built["duckdb_plans"][0].projection is not None
        print(f"  {'PASS' if ok else 'FAIL'}  the DuckDB projection translates")
        if not ok:
            failures += 1

        # A wildcard schema must enumerate the catalog, not discard it.
        wildcard = dict(spec)
        wildcard["scope"] = {"include": [{"objectClass": "table", "schema": "*", "name": "ORDERS"}]}
        wildcard["rules"] = [
            {"id": "orders-table",
             "match": {"objectClass": "table", "schema": "*", "name": "ORDERS"},
             "target": {"schema": "public", "table": "orders"}}
        ]
        kept = filter_catalog_to_spec(
            parse_catalog(wildcard, found, ""), wildcard, Diagnostics()
        )
        ok = not kept.empty
        print(f"  {'PASS' if ok else 'FAIL'}  a wildcard schema keeps the catalog")
        if not ok:
            failures += 1

        # A relation the scope resolves and the rules name, but that is then excluded,
        # produces no job. `jobs: 0` is truthful, yet two empty files do not say
        # whether anything matched -- so the net statement is required.
        excluded = dict(spec)
        excluded["scope"] = {
            "include": [{"objectClass": "table", "schema": "HR", "name": "ORDERS"}],
            "exclude": [{"objectClass": "table", "schema": "*", "name": "*"}],
        }
        excluded_diags = Diagnostics()
        excluded_naming = Naming(excluded, excluded_diags)
        excluded_plans = plan_jobs(
            excluded, parse_catalog(excluded, found, ""), excluded_naming, excluded_diags
        )
        codes = {d.code for d in excluded_diags.items}
        ok = not excluded_plans and "PLANNER_EMPTY_RESULT" in codes
        print(f"  {'PASS' if ok else 'FAIL'}  relations dropped by scope.exclude are stated, not silent")
        if not ok:
            failures += 1
            print(f"        {len(excluded_plans)} plan(s), codes {sorted(codes)}")

        # A scope matching genuinely nothing is not a defect, and must stay quiet:
        # a specification that migrates no table is a legitimate, empty migration.
        # A rule that names exactly what it migrates is authoritative, so a table the
        # catalog has no metadata for is still migrated -- with its columns
        # becoming assumptions recorded in the file, rather than the table
        # vanishing. The specification is the source of truth.
        uncatalogued = dict(spec)
        uncatalogued["scope"] = {"include": [{"objectClass": "table", "schema": "HR", "name": "GHOST"}]}
        uncatalogued["rules"] = [
            {"id": "ghost",
             "match": {"objectClass": "table", "schema": "HR", "name": "GHOST"},
             "target": {"schema": "public", "table": "ghost"}}
        ]
        ghost_diags = Diagnostics()
        ghost_naming = Naming(uncatalogued, ghost_diags)
        ghost_plans = plan_jobs(
            uncatalogued, parse_catalog(uncatalogued, found, ""), ghost_naming, ghost_diags
        )
        ok = len(ghost_plans) == 1
        print(f"  {'PASS' if ok else 'FAIL'}  a rule naming a table the catalog lacks still migrates it")
        if not ok:
            failures += 1
            print(f"        {len(ghost_plans)} plan(s), codes {sorted({d.code for d in ghost_diags.items})}")

    # Does the compiler need a catalog file? It needs the source's *shape* -- which
    # columns the relation has, and their types. A specification may state that
    # itself, under a table rule's own `columns:`, which is what makes it possible
    # to compile with nothing but the one file. These checks pin both halves of that:
    # the specification alone is enough, and it is not enough when it says nothing.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        inline_spec = tmp_path / "inline-spec.yaml"
        inline_spec.write_text(
            'schemaVersion: "5.1.0"\n'
            "engines:\n"
            "  source: { engine: oracle }\n"
            "  target: { engine: postgresql }\n"
            "scope:\n"
            "  include:\n"
            "    - { objectClass: table, schema: HR, name: ORDERS }\n"
            "rules:\n"
            "  - id: orders-table\n"
            "    match: { objectClass: table, schema: HR, name: ORDERS }\n"
            "    target: { schema: public, table: orders }\n"
            "    columns:\n"
            '      - { name: ORDER_ID,    type: "NUMBER(10)",      nullable: false }\n'
            '      - { name: CUSTOMER_ID, type: "NUMBER(10)" }\n'
            '      - { name: ORDER_DATE,  type: "DATE" }\n'
            '      - { name: AMOUNT,      type: "NUMBER(12,2)" }\n'
            '      - { name: STATUS,      type: "VARCHAR2(20)" }\n'
            "    primaryKey: [ORDER_ID]\n"
            "acceptance:\n"
            "  checks: [row-count-exact]\n"
            "run:\n"
            "  acquisition: snapshot\n"
            "  capture: { startPosition: explicit-scn, scn: 123456789 }\n",
            encoding="utf-8",
        )

        solo = compile_all(load_yaml(inline_spec), inline_spec, None, None)
        solo_jobs = solo["jobs"]
        ok = len(solo_jobs) == 1 and solo_jobs[0].query is not None
        print(f"  {'PASS' if ok else 'FAIL'}  a specification alone, with its own columns, compiles")
        if not ok:
            failures += 1
            for item in solo["diagnostics"].blocking[:3]:
                print(f"        {item.code}: {item.message[:140]}")

        # The target names follow the specification's own naming rules, so what
        # matters here is that all five declared columns and their Oracle types are
        # the ones the DDL was built from.
        inline_conf = solo["artifacts"].get(SEATUNNEL_CONF_NAME, "")
        ok = all(
            f'"{name}"' in inline_conf
            for name in ("ORDER_ID", "CUSTOMER_ID", "ORDER_DATE", "AMOUNT", "STATUS")
        ) and "NUMERIC(12, 2)" in inline_conf and "VARCHAR(20)" in inline_conf
        print(f"  {'PASS' if ok else 'FAIL'}  the declared columns and types reach the DDL")
        if not ok:
            failures += 1
            print(f"        missing from {len(inline_conf)} bytes of config")

        # A key stated inline has to reach the DDL too: a change-aware write and the
        # key check in duckdb.yaml both need one, and neither can invent it.
        ok = 'PRIMARY KEY ("ORDER_ID")' in inline_conf
        print(f"  {'PASS' if ok else 'FAIL'}  a key declared inline reaches the CREATE TABLE")
        if not ok:
            failures += 1

        ok = not any(d.code == "NO_PRIMARY_KEY" for d in solo["diagnostics"].items)
        print(f"  {'PASS' if ok else 'FAIL'}  an inline key is not reported as missing")
        if not ok:
            failures += 1

        ok = any(
            d.code == "SOURCE_SHAPE_FROM_SPEC"
            for j in solo["jobs"]
            for d in j.diagnostics.items
        )
        print(f"  {'PASS' if ok else 'FAIL'}  a shape taken from the spec is recorded, not silent")
        if not ok:
            failures += 1

        # And into the files. The person who needs to know a projection came from a
        # hand-written list only has the artifact; a diagnostic that never reaches
        # it is a diagnostic nobody reads.
        solo_duckdb = solo["artifacts"].get(DUCKDB_JOBS_NAME, "")
        solo_conf = solo["artifacts"].get(SEATUNNEL_CONF_NAME, "")
        ok = (
            any("own `columns:`" in a for a in solo_jobs[0].assumptions)
            and "own `columns:`" in solo_duckdb
            and "own `columns:`" in solo_conf
        ) if solo_jobs else False
        print(f"  {'PASS' if ok else 'FAIL'}  a spec-supplied shape reaches both artifacts")
        if not ok:
            failures += 1
            print(f"        assumptions {solo_jobs[0].assumptions if solo_jobs else []}")

        # An Oracle-extracted catalog outranks a hand-written list. A stale
        # `columns:` must not be able to silently govern a migration.
        (tmp_path / "catalog.yaml").write_text(
            'tables:\n'
            '  - schema: HR\n'
            '    name: ORDERS\n'
            '    primaryKey: [ORDER_ID]\n'
            '    columns:\n'
            '      - { name: ORDER_ID, type: "NUMBER(10)" }\n'
            '      - { name: SECRET,   type: "VARCHAR2(64)" }\n',
            encoding="utf-8",
        )
        rival_path = tmp_path / "rival-spec.yaml"
        rival_path.write_text(
            'schemaVersion: "5.1.0"\n'
            "catalog: catalog.yaml\n"
            "engines:\n"
            "  source: { engine: oracle }\n"
            "  target: { engine: postgresql }\n"
            "scope:\n"
            "  include:\n"
            "    - { objectClass: table, schema: HR, name: ORDERS }\n"
            "rules:\n"
            "  - id: orders-table\n"
            "    match: { objectClass: table, schema: HR, name: ORDERS }\n"
            "    target: { schema: public, table: orders }\n"
            "    columns:\n"
            '      - { name: ORDER_ID, type: "NUMBER(10)" }\n',
            encoding="utf-8",
        )
        rival_spec = load_yaml(rival_path)
        found, found_path = _load_optional_catalog(rival_spec, None, rival_path)
        rival_conf = compile_all(rival_spec, rival_path, found, found_path)["artifacts"].get(
            SEATUNNEL_CONF_NAME, ""
        )
        ok = "SECRET" in rival_conf
        print(f"  {'PASS' if ok else 'FAIL'}  a catalog outranks an inline column list")
        if not ok:
            failures += 1
            print("        the inline list governed the migration instead of the catalog")

        # And when neither states the shape, there is no honest job to emit. The
        # table does not quietly become an empty projection.
        bare_path = tmp_path / "bare-spec.yaml"
        bare_path.write_text(
            'schemaVersion: "5.1.0"\n'
            "engines:\n"
            "  source: { engine: oracle }\n"
            "  target: { engine: postgresql }\n"
            "scope:\n"
            "  include:\n"
            "    - { objectClass: table, schema: HR, name: ORDERS }\n"
            "rules:\n"
            "  - id: orders-table\n"
            "    match: { objectClass: table, schema: HR, name: ORDERS }\n"
            "    target: { schema: public, table: orders }\n"
            "acceptance:\n"
            "  checks: [row-count-exact]\n"
            "run:\n"
            "  acquisition: snapshot\n",
            encoding="utf-8",
        )
        bare = compile_all(load_yaml(bare_path), bare_path, None, None)
        # Per-job findings live on the job, not on the spec-wide sink: one job's
        # missing shape is not a statement about the whole specification.
        codes = {d.code for j in bare["jobs"] for d in j.diagnostics.items}
        ok = (
            "SOURCE_SHAPE_UNKNOWN" in codes
            and all(j.query is None for j in bare["jobs"])
            and all(j.status == "BLOCKED" for j in bare["jobs"])
        )
        print(f"  {'PASS' if ok else 'FAIL'}  a spec stating no columns is blocked, not narrowed")
        if not ok:
            failures += 1
            print(f"        codes {sorted(codes)}, statuses {[j.status for j in bare['jobs']]}")

        # A blocking finding has to reach the files, or nobody reading the output
        # ever hears it.
        bare_duckdb = bare["artifacts"].get(DUCKDB_JOBS_NAME, "")
        bare_conf = bare["artifacts"].get(SEATUNNEL_CONF_NAME, "")
        ok = "SOURCE_SHAPE_UNKNOWN" in bare_duckdb or "SOURCE_SHAPE_UNKNOWN" in bare_conf
        print(f"  {'PASS' if ok else 'FAIL'}  the block is stated in the artifact, not only in memory")
        if not ok:
            failures += 1

        # A filter on a computed value (ADR-0098) must compile WITHOUT a
        # catalog. The floor used to read the predicate's `line_total` as a
        # source column and register it twice -- once as a base column, once
        # as the derived recipe's own output -- so the job blocked as
        # DUPLICATE_TARGET_COLUMN and the artifacts silently lacked the job.
        # The assertions are on the job's own state and on the code list:
        # "it compiled" would pass for a job that lost its filter.
        adr_path = tmp_path / "adr-0098-spec.yaml"
        adr_path.write_text(
            'schemaVersion: "5.1.0"\n'
            "engines:\n"
            "  source: { engine: oracle }\n"
            "  target: { engine: postgresql }\n"
            "scope:\n"
            "  include:\n"
            "    - { objectClass: table, schema: SHOP, name: ORDER_LINE }\n"
            "rules:\n"
            "  - id: line-total\n"
            "    match: { objectClass: table, schema: SHOP, name: ORDER_LINE }\n"
            '    target: { column: line_total, type: "numeric(14,2)" }\n'
            "    steps:\n"
            "      - { category: derived, operation: arithmetic, inputs: [QTY, UNIT_PRICE], "
            "parameters: { operator: multiply } }\n"
            "  - id: lines-with-value\n"
            "    match: { objectClass: table, schema: SHOP, name: ORDER_LINE }\n"
            "    steps:\n"
            "      - { category: row-selection, predicate: { column: line_total, "
            "comparison: gt, literal: 0 } }\n"
            "acceptance:\n"
            "  checks: [row-count-exact]\n"
            "run:\n"
            "  acquisition: snapshot\n",
            encoding="utf-8",
        )
        adr = compile_all(load_yaml(adr_path), adr_path, None, None)
        adr_codes = {d.code for j in adr["jobs"] for d in j.diagnostics.items}
        adr_jobs = adr["jobs"]
        adr_query = adr_jobs[0].query or "" if adr_jobs else ""
        ok = (
            len(adr_jobs) == 1
            and adr_jobs[0].status != "BLOCKED"
            and adr_jobs[0].query is not None
            and "DUPLICATE_TARGET_COLUMN" not in adr_codes
            and "line_total" in adr_query
        )
        print(f"  {'PASS' if ok else 'FAIL'}  a filter on a derived value compiles without a catalog")
        if not ok:
            failures += 1
            print(f"        codes {sorted(adr_codes)}, statuses {[j.status for j in adr_jobs]}")

    # The rulebook. The vocabularies live in config/rules.yaml, and these
    # checks prove the *file* governs rather than the constants in this module:
    # the default drives a real compile, an override loaded from a temporary
    # file changes a validation outcome, and a file that cannot be used raises
    # by name instead of falling back to the defaults or crashing the loader.
    # Every assertion is on SQL text or a diagnostic code -- "it compiled"
    # would pass for a rulebook that had lost an operation and taken the
    # transformation with it.
    import json

    # Kept in step with the checks printed in this block; the summary line
    # below counts them by name so a new check cannot be forgotten there.
    RULEBOOK_CASES = 6

    with tempfile.TemporaryDirectory() as tmp:
        rulebook_spec = Path(tmp) / "rulebook-spec.yaml"
        rulebook_spec.write_text(
            'schemaVersion: "5.1.0"\n'
            "engines:\n"
            "  source: { engine: oracle }\n"
            "  target: { engine: postgresql }\n"
            "scope:\n"
            "  include:\n"
            "    - { objectClass: table, schema: HR, name: ORDERS }\n"
            "rules:\n"
            "  - id: orders-table\n"
            "    match: { objectClass: table, schema: HR, name: ORDERS }\n"
            "    target: { schema: public, table: orders }\n"
            "    columns:\n"
            '      - { name: ORDER_ID, type: "NUMBER(10)", nullable: false }\n'
            '      - { name: NAME, type: "VARCHAR2(40)" }\n'
            "    primaryKey: [ORDER_ID]\n"
            "  - id: name-is-trimmed\n"
            "    match: { objectClass: column, schema: HR, name: ORDERS, column: NAME }\n"
            "    steps:\n"
            "      - { category: value, operation: trim }\n"
            "acceptance:\n"
            "  checks: [row-count-exact]\n"
            "run:\n"
            "  acquisition: snapshot\n",
            encoding="utf-8",
        )
        active = active_rules()

        # (a) The default rulebook drives a real path. `trim` is a value
        # operation in the loaded rulebook and the job's projection has to say
        # TRIM -- the SQL is the evidence, and the absence of
        # STEP_OPERATION_UNKNOWN is the validation half of the same claim.
        default_run = compile_all(load_yaml(rulebook_spec), rulebook_spec, None, None)
        default_query = default_run["jobs"][0].query if default_run["jobs"] else None
        default_codes = {d.code for d in default_run["diagnostics"].items}
        ok = (
            default_query is not None
            and "TRIM(" in default_query
            and "trim" in active["categories"]["value"]
            and "STEP_OPERATION_UNKNOWN" not in default_codes
        )
        print(f"  {'PASS' if ok else 'FAIL'}  the default rulebook drives a real compile")
        if not ok:
            failures += 1
            print(f"        codes {sorted(default_codes)}")
            print(f"        {default_query}")

        # (b) An override changes validation. Taking `trim` out of the `value`
        # gate in a temporary rulebook has to block the very same step, and
        # loading it through the environment variable proves that variable
        # wins over the project's own config rather than being merged with it.
        override = json.loads(json.dumps(active))
        override["categories"]["value"] = [
            op for op in override["categories"]["value"] if op != "trim"
        ]
        override_path = Path(tmp) / "override-rules.yaml"
        override_path.write_text(yaml.safe_dump(override, sort_keys=False), encoding="utf-8")

        saved_rulebook_env = os.environ.get(RULEBOOK_ENV_VAR)
        narrow_codes: set = set()
        try:
            os.environ[RULEBOOK_ENV_VAR] = str(override_path)
            load_rulebook()
            narrow_run = compile_all(load_yaml(rulebook_spec), rulebook_spec, None, None)
            # `.blocking`, not every diagnostic: the claim is that the step
            # *blocks*, and a rulebook that only warned would ship it.
            narrow_codes = {d.code for d in narrow_run["diagnostics"].blocking}
            ok = (
                "STEP_OPERATION_UNKNOWN" in narrow_codes
                and "trim" not in active_rules()["categories"]["value"]
            )
        except Exception as exc:  # noqa: BLE001 - a wrong exception fails the check, not the suite
            ok = False
            narrow_codes = {f"WRONG EXCEPTION {type(exc).__name__}: {exc}"}
        finally:
            # Back to the project's own rulebook: whatever the override said,
            # everything after this point compiles against the shipped rules.
            if saved_rulebook_env is None:
                os.environ.pop(RULEBOOK_ENV_VAR, None)
            else:
                os.environ[RULEBOOK_ENV_VAR] = saved_rulebook_env
            load_rulebook()
        print(
            f"  {'PASS' if ok else 'FAIL'}  an override rulebook blocks the step its default allowed"
        )
        if not ok:
            failures += 1
            print(f"        codes {sorted(narrow_codes)}")

        # (c) A rulebook that exists but cannot be used raises the named
        # error. Two ways to be unusable: not parseable at all, and parseable
        # with the wrong type for a key. Neither may be swallowed into a
        # silent fallback to the defaults, and neither may reach the caller as
        # a raw PyYAML or KeyError traceback.
        malformed_path = Path(tmp) / "malformed-rules.yaml"
        malformed_path.write_text("value_ops: [cast, trim\n", encoding="utf-8")
        wrong_type = json.loads(json.dumps(active))
        wrong_type["value_ops"] = "cast"  # a scalar where a list belongs
        wrong_type_path = Path(tmp) / "wrong-type-rules.yaml"
        wrong_type_path.write_text(yaml.safe_dump(wrong_type, sort_keys=False), encoding="utf-8")

        malformed_cases = [
            (
                "a malformed rulebook raises RuleBookError, not a YAML traceback",
                malformed_path,
                "is not valid YAML",
            ),
            (
                "a rulebook with the wrong type for a key raises RuleBookError",
                wrong_type_path,
                "`value_ops` must be a list of strings",
            ),
        ]
        for label, path, expected in malformed_cases:
            try:
                load_rulebook(path)
                message = ""
            except RuleBookError as exc:
                message = str(exc)
            except Exception as exc:  # noqa: BLE001
                message = f"WRONG EXCEPTION {type(exc).__name__}: {exc}"
            ok = expected in message
            print(f"  {'PASS' if ok else 'FAIL'}  {label}")
            if not ok:
                failures += 1
                print(f"        expected {expected!r} in: {message[:300] or '<no error raised>'}")

        # A rejected rulebook must leave the one in force untouched: the
        # failure is reported, not resolved by quietly swapping vocabularies.
        load_rulebook()
        ok = active_rules() == active
        print(f"  {'PASS' if ok else 'FAIL'}  a rejected rulebook leaves the loaded one in force")
        if not ok:
            failures += 1

        # GET /rules serves this, and every vocabulary is a set -- which JSON
        # cannot encode. Sorted lists make it servable and make the response
        # stable between calls.
        try:
            payload = json.dumps(active_rules())
            exported = json.loads(payload)
            ok = (
                isinstance(exported["value_ops"], list)
                and exported["value_ops"] == sorted(exported["value_ops"])
                and isinstance(exported["categories"]["value"], list)
                and "trim" in exported["categories"]["value"]
            )
        except Exception as exc:  # noqa: BLE001
            ok = False
            payload = f"WRONG EXCEPTION {type(exc).__name__}: {exc}"
        print(f"  {'PASS' if ok else 'FAIL'}  active_rules() is JSON-safe for GET /rules")
        if not ok:
            failures += 1
            print(f"        {payload[:300]}")

    # The config loader gets the same treatment the rulebook block above got:
    # prove it loads what ships, prove a partial file merges over the
    # defaults, and prove the two ways to be unusable -- an unknown key and a
    # wrong type -- raise ConfigError by name rather than being ignored (an
    # unknown key) or crashing a read site later (a wrong type). A malformed
    # file must reach the caller as the named error, not a PyYAML traceback.
    # Kept in step with the checks printed in this block; the summary line
    # below counts them by name so a new check cannot be forgotten there.
    CONFIG_CASES = 5

    with tempfile.TemporaryDirectory() as tmp:
        # (a) What ships loads. The assertion is on shape, not on values: a
        # deployment may have edited config/config.yaml, and a self-test that
        # pinned the shipped numbers would fail for the operator who used the
        # file exactly as intended.
        try:
            loaded = app_config()
            ok = (
                set(loaded) == set(_DEFAULT_CONFIG)
                and set(loaded["storage"]) == set(_DEFAULT_CONFIG["storage"])
                and set(loaded["server"]) == set(_DEFAULT_CONFIG["server"])
            )
            detail = str(loaded)[:300]
        except Exception as exc:  # noqa: BLE001
            ok = False
            detail = f"WRONG EXCEPTION {type(exc).__name__}: {exc}"
        print(f"  {'PASS' if ok else 'FAIL'}  the project's config loads with a complete shape")
        if not ok:
            failures += 1
            print(f"        {detail}")

        # (b) A partial config is legal and merges over the defaults: this is
        # the deliberate asymmetry with the rulebook, and the case that would
        # silently run the wrong TTL if the merge were missing.
        partial_path = Path(tmp) / "partial-config.yaml"
        partial_path.write_text("storage:\n  ttl_seconds: 60\n", encoding="utf-8")
        try:
            partial = load_config(partial_path)
            ok = (
                partial["storage"]["ttl_seconds"] == 60
                and partial["storage"]["dir"] == _DEFAULT_CONFIG["storage"]["dir"]
                and partial["server"] == _DEFAULT_CONFIG["server"]
            )
            detail = str(partial)[:300]
        except Exception as exc:  # noqa: BLE001
            ok = False
            detail = f"WRONG EXCEPTION {type(exc).__name__}: {exc}"
        print(f"  {'PASS' if ok else 'FAIL'}  a partial config fills the rest from the defaults")
        if not ok:
            failures += 1
            print(f"        {detail}")

        # (c) Two ways to be unusable, each raising by name with the key in
        # the message: an unknown key would be IGNORED if accepted (the typo
        # keeps its default, unnoticed), and a wrong type would survive the
        # merge only to explode at a read site that names no config file.
        unknown_path = Path(tmp) / "unknown-key-config.yaml"
        unknown_path.write_text("storage:\n  ttl: 60\n", encoding="utf-8")
        wrong_type_path = Path(tmp) / "wrong-type-config.yaml"
        wrong_type_path.write_text("storage:\n  ttl_seconds: sixty\n", encoding="utf-8")
        malformed_path = Path(tmp) / "malformed-config.yaml"
        malformed_path.write_text("storage: [ttl_seconds: 60\n", encoding="utf-8")

        config_cases = [
            (
                "an unknown config key raises ConfigError, not a silent default",
                unknown_path,
                "not a setting under `storage`",
            ),
            (
                "a wrong config type raises ConfigError, not a later KeyError",
                wrong_type_path,
                "`storage.ttl_seconds` must be a positive whole number",
            ),
            (
                "a malformed config raises ConfigError, not a YAML traceback",
                malformed_path,
                "is not valid YAML",
            ),
        ]
        for label, path, expected in config_cases:
            try:
                load_config(path)
                message = ""
            except ConfigError as exc:
                message = str(exc)
            except Exception as exc:  # noqa: BLE001
                message = f"WRONG EXCEPTION {type(exc).__name__}: {exc}"
            ok = expected in message
            print(f"  {'PASS' if ok else 'FAIL'}  {label}")
            if not ok:
                failures += 1
                print(f"        expected {expected!r} in: {message[:300] or '<no error raised>'}")

    # `cases` is the specification-reading list above; the base count covers
    # the checks outside it (it grew from 30 to 31 when the ADR-0098
    # filter-on-a-derived-value check was added), and RULEBOOK_CASES and
    # CONFIG_CASES are the two blocks that follow.
    total = len(cases) + 31 + RULEBOOK_CASES + CONFIG_CASES
    print(f"\nself-test: {total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
