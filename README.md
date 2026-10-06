# Migration spec → Apache SeaTunnel / DuckDB

Compile one approved migration specification (schema 5.1.0) into **two files**,
one per runner.

| File | What it is |
| --- | --- |
| `<spec>/seatunnel.conf` | **one** SeaTunnel HOCON file. For every target relation, a `<schema>_<table>_ddl` job and a `<schema>_<table>_data` job. Each carries the **source query**, the **target query**, the **DDL** or **DML commands**, and **`schema_save_mode`**. |
| `<spec>/duckdb.yaml` | **one** DuckDB YAML file. The **validation rules** the specification implies, the **compiled query** for each of them, and the **source queries** they compare against. |

`<spec>` is the specification file's own name, under `output/`:

```text
output/migration-spec/seatunnel.conf
output/migration-spec/duckdb.yaml
output/migration-spec-scenario-2-explicit-scn/seatunnel.conf
output/migration-spec-scenario-2-explicit-scn/duckdb.yaml
```

**Give it any specification and it gets its own folder.** Two specifications can
never overwrite each other's output, and recompiling one leaves the others alone.

The console is a receipt — which files, how big, how many jobs. It does not report
findings. Whether a rule compiled correctly, what was assumed and what a human must
sign off on are answered by running the checks in `duckdb.yaml`, which is where
they live.

All of it is produced by **one module**, `src/transpiler.py`, which holds every
rule. `AGENT.md` is the operating document: the flow, the invariants, and the
edge-case table.

## Run

```powershell
pip install -r requirements.txt
python src\transpiler.py
```

That is the whole run. Works from any directory: the default `--spec`,
`--catalog` and `--output-dir` resolve against the **project**, not the current
working directory, so an editor that launches the script with the workspace root
as its cwd behaves the same as one that launches it from `src/`. An explicit
relative path you pass is still relative to your cwd, as usual.

Note the package name: `PyYAML`, not `yaml`. There is no PyPI distribution called
`yaml`, so `pip install yaml` fails.

Any specification works — pass it and it compiles into its own folder:

```powershell
python src\transpiler.py                                             # the default spec
python src\transpiler.py --spec input\my-new-migration.yaml          # a new one
python src\transpiler.py --spec input\hr.yaml --catalog input\hr-catalog.yaml
python src\transpiler.py --output-dir D:\build                       # a different root
python src\transpiler.py --self-test                                 # no inputs needed
```

A specification can name its own catalog, so no flag is needed per spec:

```yaml
catalog: catalog_hr.yaml     # resolved beside the specification
```

Also searched automatically, beside the spec: `catalog.yaml`, `catalog.yml`,
`<spec-stem>_catalog.yaml`, `catalog_<spec-stem>.yaml`. `--catalog` still wins,
and an explicit one that does not exist is an error rather than a silent fallback.

### A catalog file is optional

`--catalog` is not required. A table rule can state what its own relation contains,
and then the specification compiles on its own — which is exactly what
`input/hr-spec.yaml` does:

```yaml
rules:
  - id: orders-table
    match:   { objectClass: table, schema: HR, name: ORDERS }
    target:  { schema: public, table: orders }
    columns:
      - { name: ORDER_ID,    type: "NUMBER(10)",      nullable: false }
      - { name: CUSTOMER_ID, type: "NUMBER(10)",      nullable: false }
      - { name: ORDER_DATE,  type: "DATE",            nullable: false }
      - { name: AMOUNT,      type: "NUMBER(12,2)" }
      - { name: STATUS,      type: "VARCHAR2(20)" }
    primaryKey: [ORDER_ID]
```

```powershell
python src\transpiler.py --spec input\hr-spec.yaml
```

```
  catalog     : none supplied (1 table(s))
```

The compiler needs the source's **shape** — its columns, Oracle types and key —
because a projection is a list of columns and a rule only names the ones it
transforms. It looks in three places, and stops at the first that answers:

| Source | What it is |
| --- | --- |
| a catalog | extracted from Oracle. Authoritative. |
| the rule's own `columns:` | the specification stating the shape outright. |
| the columns the rules name | a floor: only what the rules mention migrates. |

Two things follow from that ordering. A **catalog wins** over an inline list, so a
stale hand-written list cannot silently govern a migration. And if nothing
describes a relation at all, the job is **blocked** (`SOURCE_SHAPE_UNKNOWN`) rather
than emitted with an empty projection — a job that loads nothing must not look like
a migration.

**Wildcards still require a catalog.** `schema: "*"` names every schema and only
the database knows which exist, so a wildcard scope cannot be resolved from a
specification. That is the one case with no inline alternative.

[`need_catalog.md`](need_catalog.md) has the full argument, including when an
Oracle-extracted catalog is still the better answer despite all this — and the
short answer is: for anything meant to actually run.

### Scope and wildcards

`scope.include` with a **wildcard schema** (`schema: "*"`) cannot be resolved from
the specification alone — `*` names every schema, and only the catalog says which
exist. It requires a catalog; without one the run says so rather than compiling a
subset. This is the whole reason `input/catalog_orders_wildcard.yaml` exists.

`scope.exclude` is applied after resolution, and each excluded relation is named.

Two relations that resolve to the same `target.table` are a **collision**, not a
merge, and are reported as one — the alternative is two jobs overwriting one
relation. Combining several sources into one target is a `cardinality/merge` step
naming its sources.

A rule that names a table the catalog does not list is still migrated, with its
columns recorded as assumptions in the file. The specification is the source of
truth; a missing catalog entry is not a reason to drop what was asked for.

```
migration-transpiler 3.0.0

  spec        : input\migration-spec.yaml
  output      : ...\output\migration-spec

  seatunnel.conf  24 jobs    51 columns    80,238 bytes
  duckdb.yaml     12 jobs   115 checks   111,287 bytes

  12 of 12 target relations compiled
```

Exit codes: `0` compiled, `1` a generated file did not survive being read back and
re-parsed, `2` the specification could not be read.

`--catalog` is looked for beside the specification first (`input/catalog.yaml`
next to whatever `--spec` you passed), then in the project root. If you pass
`--catalog` explicitly it is not searched for anywhere else, so a typo is
reported rather than silently ignored.

### When a specification will not read

The compiler reports *why* a file is unreadable rather than showing a YAML
traceback, and it names the accidents that actually happen:

```text
input/spec.yaml is not valid YAML: 24 line(s) begin with an escaped comment
marker '\#', at line 1, 3, 5, 7 (and 20 more).

  A backslash before '#' makes it an ordinary character, so the parser reads
  those lines as data rather than comments, and then fails on the first of
  them with an unrelated-looking message.

  Fix: delete the backslashes, so the lines start with '#' again.
```

It detects escaped comment markers (`\#`), non-breaking spaces (U+00A0) in
indentation, tabs, a non-mapping document root, non-UTF-8 bytes, and a genuine
syntax error — for that last one it shows the offending line, a caret, and the
line above when that is where the key went missing. Nothing is written on a read
failure, and the exit code is 2.

All three accidents come from a file being copied through a tool that escapes
Markdown punctuation or reformats indentation. They are the reason the check runs
*before* parsing: PyYAML reports where it stopped, which for an escaped comment
is the first such line rather than the cause.

`--catalog` is required whenever the specification names objects by wildcard
(`SHOP.*`, `ORDERS_20*`, `column: AMOUNT`). Without it those rules cannot be
enumerated, so the relations they name are simply omitted rather than compiled as
a partial guess. It is looked for beside the specification first, then in the
project root.

---

## `output/<spec>/seatunnel.conf`

```hocon
job {
  shop_client_ddl {
    env    { parallelism, job.mode, job.name, checkpoint.interval }
    ddl    { schema_save_mode = "RECREATE_SCHEMA"
             target, columns
             create-table.sql = """CREATE TABLE IF NOT EXISTS ..."""
             partition-default.sql = ..., tablespace.sql = ... }
    source { Jdbc { query = """SELECT ... WHERE 1 = 0""" } }   # source query
    sink   { Jdbc { schema_save_mode = "RECREATE_SCHEMA" } }
  }

  shop_client_data {
    env       { parallelism, job.mode, job.name, checkpoint.mode }
    source    { Jdbc { query = <the compiled projection> } }    # source query
    dml       { schema_save_mode = "IGNORE", write_mode
                statement = """INSERT INTO "shop"."client" (...) SELECT ..."""
                             ON CONFLICT (...) DO UPDATE SET ...""" }
    transform { SQL { query = <the target query> } }
    sink      { Jdbc { schema_save_mode = "IGNORE" } }
  }
}
```

**Why the DDL and DML are written out rather than inferred.** SeaTunnel can create
a table from a zero-row read, but it does so silently: a target created with the
wrong shape produces no error at creation, only a load that quietly writes the
wrong columns. Written out, the `CREATE TABLE` is reviewable on its own and its
column list can be compared against the projection's.

A `dml` statement names a PostgreSQL relation on the left and an Oracle one on the
right, so it is **not runnable on either engine alone**. It is the write stated
down, so it can be read against the sink block and the target query and checked.
It reads the same query the transform is fed, so on a CDC run it names the change
stream rather than the base relation the job no longer reads.

Running it:

```powershell
$env:SOURCE_JDBC_URL = 'jdbc:oracle:thin:@//host:1521/SID'
$env:SOURCE_DB_USER = '...'; $env:SOURCE_DB_PASSWORD = '...'
$env:TARGET_JDBC_URL = 'jdbc:postgresql://host:5432/app'
$env:TARGET_DB_USER = '...'; $env:TARGET_DB_PASSWORD = '...'
$env:run_scn = '94210'        # resolved from RUN_RECORD.oracle_scn, never hardcoded

bin\seatunnel.bat --config .\output\seatunnel.conf --name shop_client_ddl
bin\seatunnel.bat --config .\output\seatunnel.conf --name shop_client_data
```

A `*_ddl` job must pass before its `*_data` job.

---

## `output/<spec>/duckdb.yaml`

```yaml
job:
  JOB-shop_customer:
    order: 1
    env:     { job_name, mode: READ_ONLY, snapshot: ${run_scn}, binds, depends_on }
    source:  { projection_view, projection, sqlglot, materialise, raw_relations }
    target:  { relation, view, quarantine_view }
    check:   [ { check_id, source_spec, type, sql, expected, runtime_strategy, notes } ]
    sink:    { on_violation: report, evidence, pass_condition }
```

Each job binds three relations at run time:

| Relation | What it is |
| --- | --- |
| `__RAW__<schema>_<table>` | the Oracle relation, exported once at the pinned SCN |
| `__SOURCE__<job>` | the compiled projection, read at the pinned SCN |
| `__TARGET__<job>` | the target relation as the load left it |

A check **passes when it returns zero rows**. Every row returned is one violation
and names the column that failed.

The three comparisons answer three different questions:

| Compare | Catches |
| --- | --- |
| `__SOURCE__` vs `__TARGET__` | the sink, the transport and the write mode: a truncation on write, a `NULL` that became `''`, an upsert that overwrote a newer row |
| `__RAW__` vs `__SOURCE__` | the recipes: a value state the specification said must survive and did not |
| `__RAW__` vs `__TARGET__` | what both got wrong |

Checks are derived from the specification section by section, not from a fixed
list: every `acceptance.checks` entry, every `acceptance.stricter` entry, every
operation the compiled query actually performed, every `fidelityFloor` state the
target can evidence, every `sensitive[].handling`, and every governance finding.

Three conventions worth knowing:

- **Row counts compare the projection, not the raw table.** Counting raw against
  target reports every `row-selection` rule as a lost row.
- **`sensitive` is checked as the complement of the promise.** `mask` asserts
  target ≠ raw; `drop` asserts the column is absent; `keep` asserts equal.
- **The `fidelityFloor` derivation** turns the promise into evidence with one
  comparison: every target column the specification chose *not* to transform must
  arrive byte-identical. That is what makes `TRAILING_WHITESPACE`,
  `UNICODE_NORMALISATION`, `BINARY`, `HIGH_PRECISION` and `SUB_SECOND` checkable.
  The states that need a targeted assertion rather than a general one
  (`TZ_OFFSET` range, `EMPTY_LOB`, `NULL` on a `NOT NULL` column) get their own.

---

## What is implemented

Every step category the specification defines:

```text
value          cast trim substring mask default-when-null date-add regexp-extract
               uppercase lowercase replace round truncate date-trunc null-to-empty
               empty-to-null normalize-unicode pad left right split-part coalesce-blank drop
derived        concat coalesce case-when arithmetic cast-as md5
structural     add-column drop-column rename-column set-default
row-selection  allOf / anyOf, nesting, column-to-column comparison
column-split   delimiter, any length, onExtraParts
lookup         fan-out-free pre-aggregated join, cardinality guard, four onMiss behaviours
set            deduplicate (authored SQL or generated), snapshot-only
cardinality    merge (UNION ALL), split (one job per branch)
relational     denormalise with grain, joins, droppedFromSource, fan-out limits
pivot          grouped conditional aggregation, unlisted-value sentinel
change-aware   sink upsert on the target key
key-declaration target primary key, provenance reported
escape         sql-scalar and sql-set, both pinned to the run SCN
```

Plus the sections around them: `naming` (schema map/mirror/flatten, case, quoting,
hash-shortening), `defaults` and `fidelityFloor`, `sensitive`, `targetPhysical`
(partitioning, tablespace, deferrable constraints), `objectPolicies`, `run`
(cdc / snapshot / query-incremental, watermark, late arrival), `governance`.

## How each operation is answered

The recurring principle: a projection cannot raise, cannot see a previous row
image, and cannot undo a lossy step. Where the specification asks for something
only a check or a sink can deliver, that is what is emitted, and the gap is
written down rather than papered over.

| Specification says | Emitted | Why |
| --- | --- | --- |
| `onMiss: fail-run` | `NULL` + a **blocking** miss check | a `SELECT` cannot raise a run-time error |
| `onMiss: quarantine` | a `__q_` sentinel column + a quarantine sink | the sink has to be able to see which rows missed |
| `grain.uniqueness: asserted` | one row per grain + a uniqueness check | the assertion is what the check makes true |
| `change-aware` | `write_mode=upsert` on the target key | a projection has no previous image to compare |
| `pivot onUnlistedValue` | a sentinel counting unlisted values | otherwise they vanish silently |
| `column-split onExtraParts` | a sentinel counting excess parts | extra parts have no target column |
| `mask strategy=hash` | `STANDARD_HASH(…, 'SHA256')` | irreversible, which is the point, and is stated |
| range partitioning from an INTERVAL source | `PARTITION BY RANGE` + `DEFAULT` | real bounds need the source layout; inventing them misfiles rows |

## Dialects

Three dialects. Two are project-local, registered under `ctunnel` and `pgcontract`;
the third is a project-local `duckdbstrict`.

Parsing inherits a real engine; generation is overridden so the emitted text stays
faithful to the approved specification instead of drifting into Oracle's own type
names. Every generated query and DDL statement is proven byte-identical across
`parse → generate → parse → generate`.

`duckdbstrict` exists because a dialect switch is not enough. SQLGlot's default
behaviour when the target cannot express a construct is to emit a *valid but
wrong* statement: `TO_CHAR(d,'YYYY')` becomes `CAST(d AS TEXT)`, which loses the
formatting and makes a date compare equal to a string. `duckdbstrict` raises
instead, so it becomes a `BLOCK` here where the message can name it.

Oracle functions are rewritten explicitly on the AST — `REGEXP_SUBSTR` becomes
`REGEXP_EXTRACT`, or `LIST_EXTRACT(REGEXP_EXTRACT_ALL(…), n)` for the Nth match;
`STANDARD_HASH` becomes `SHA256`/`SHA1`/`MD5` — and then a **residual scan** reads
the rewritten tree for any name DuckDB has never heard of. The scan is what makes
the rewrite table trustworthy: a table that is merely *believed* complete produces
a plan that parses, and this produces one that binds.

Do not use the optional `sqlglot[c]` compiled build while developing runtime
subclassed dialects; SQLGlot documents limitations with runtime subclassing there.

## Reading the output

The console is a receipt, not a report:

```text
spec        which specification was compiled
output      the folder it went to
seatunnel.conf / duckdb.yaml   jobs, columns or checks, and size
N of M target relations compiled
```

That is all it prints on a normal run. **It does not report findings** — no
blocking list, no governance sign-offs, no assumptions, no edge cases — because
none of those are the compiler's to answer. They are answered by running the
checks in `duckdb.yaml`, where each one states what must be true and how to find
out:

- a rule the compiler could not express is not silently dropped; it becomes a
  `V-GOVERNANCE-*` check or an `assumptions` entry **inside the job body**
- a value state the specification said must survive is a `V-FIDELITY-*` check
- a relation the compiler could not resolve produces no job, and is named in the
  file header **with the code and message that stopped it** — naming the absence
  without its cause tells the reader nothing about what to change

The one thing the console will not stay quiet about is a *generated file* that did
not survive being read back and re-parsed. That is a statement about the compiler's
own output, not about the specification, and it exits non-zero.

## Layout

```text
AGENT.md                     the flow, the invariants, the edge-case table
need_catalog.md              why a catalog file is (and is not) needed
src/transpiler.py            every rule; one module
requirements.txt             PyYAML and sqlglot
input/hr-spec.yaml           admin.ORDERS -> public.orders. Self-sufficient: its
                             rule declares columns, so it needs no catalog.
input/hr-spec-wildcard.yaml  schema: "*", so it does need one
input/catalog_orders_wildcard.yaml   HR/SALES/ARCHIVE, each holding ORDERS
input/migration-spec.yaml    the reference specification
output/<spec-name>/          generated: seatunnel.conf, duckdb.yaml
```

`--self-test` is the only regression suite and lives inside `src/transpiler.py`,
so it needs no test files and no input. It covers the dialects, the Oracle→DuckDB
rewrites, specification-reading failures, the three places a source shape can come
from, and the full `HR.ORDERS → public.orders` path from `plan_jobs` to both
re-validated files.

## Known defects

Two pre-existing ones, both in `seatunnel.conf`, both of which would fail in
Oracle as well. Parsing cannot see either, because both are *legal* SQL that is
not *answerable* — they need the generated statements executed against real data
to surface:

- `shop_order_summary` — an `escape/sql-set` body is aggregated by the recipe
  planner, which then projects `"s0"."AMOUNT"` against a subquery that only emits
  `TOTAL`.
- `shop_stock_by_product` — a `pivot` selects a non-aggregated column while
  grouping by the grain only, so the statement is not answerable.

Details in `AGENT.md` §13.