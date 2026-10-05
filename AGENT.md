# AGENT.md — migration transpiler: the flow

This is the operating document for anyone or anything working on this project.
It states what the compiler guarantees, the order the pipeline must run in, and
the edge cases that are easy to get wrong. Read it before changing
`src/transpiler.py`.

---

## 1. What this is

One approved migration specification in; **two files** out, one per runner.

```text
input/migration-spec.yaml          the approved specification -- source of truth
input/catalog.yaml                 the Oracle source metadata it refers to
        |
        v
src/transpiler.py                  one module, every rule
        |
        +--> output/seatunnel.conf   ONE HOCON file
        |        for every target relation a <schema>_<table>_ddl job and a
        |        <schema>_<table>_data job, each carrying the source query, the
        |        target query, the DDL or DML commands, and schema_save_mode
        |
        +--> output/duckdb.yaml      ONE YAML file
                 the validation rules the specification implies, the compiled
                 query for each of them in DuckDB, and the source queries they
                 compare against
```

There are no other outputs. No orchestrator, no scheduler, no index, no report.
The two files are handed to two runners; nothing else decides when anything runs,
and nothing else records what the compiler found — **the findings go to stdout**.

The specification is never edited. If it says something this compiler cannot
express, the job is **blocked and reported**, never approximated.

---

## 2. The two invariants

Everything else follows from these.

### 2.1 Nothing ships that has not been re-read

The artifacts that leave this compiler are the two files, not the strings held in
memory. So the last thing the compiler does is re-read both.

**SeaTunnel** (`validate_artifacts`)

1. read the single `.conf` with the HOCON reader,
2. match every job inside it back to the job it came from,
3. extract every query **from the file** and re-parse it in the source dialect,
4. compare every sink target with the compiled target relation,
5. compare every `*_ddl` column list with the compiled column list,
6. check every `${placeholder}` against the declared bindings.

**DuckDB** (`validate_duckdb_artifacts`)

1. read the single `.yaml` with the YAML reader,
2. match every job back to the job it came from,
3. extract every projection **from the file** and re-parse it in DuckDB,
4. extract every `check[].sql` **from the file**, then prove it survives
   `parse -> generate -> parse -> generate` unchanged,
5. confirm the check list in the file is the check list the compiler produced,
6. confirm every relation a check names is one its own job actually binds.

A failure in either fails the compile and exits non-zero. These stages exist
because a quoting or escaping bug in the writer produces a file that reads
correctly and configures nothing.

A **blocked** job emits nothing, so it is expected in neither file. Expecting it
would report every blocked job as a missing job and fail the stage on exactly the
runs that are already blocked for a better reason.

### 2.2 A query that reflows is a bug, not a warning

Every generated query and every generated DDL statement is proven by
`parse -> generate -> parse -> generate` byte-identity:

* source and CDC-projection SQL in the `ctunnel` dialect (inherits Oracle),
* target DDL in the `pgcontract` dialect (inherits PostgreSQL),
* DuckDB projections and checks in `duckdbstrict`.

If the second render differs from the first, the text would change between the
validated version and the shipped version. That is a `BLOCK`.

---

## 3. The flow

The order is not a preference. Each stage exists because the next one trusts it.

```text
 1. validate_spec            the specification is sound            abort
 2. validate_query           every query round-trips byte-stably   abort
 3. validate_source_query    in the SOURCE engine's own dialect    abort
 4. validate_target_query    in the TARGET engine's own dialect    abort
 5. validate_seatunnel_query the .conf itself, re-read             abort
 6. validate_duckdb_query    the .yaml itself, re-read             abort
 7. emit                     seatunnel.conf + duckdb.yaml, then print
```

Stages 1–4 are compile-time and are reported by `summarize()`. Stages 5–6 are
artifact validation and are what `main()` exits non-zero on. There is no stage 7
onward: the two runners take it from there.

### Why stage 5 exists

Stages 2–4 validate the query the compiler built. Stage 5 validates the query
**as it appears in the file that will be submitted**. Those are different strings
once quoting, escaping, `"""` blocks and variable substitution are involved, and
only the second one is what SeaTunnel will run.

### Why stage 6 is not the same shape as stage 5

The DuckDB file has a second dimension: every check names relations. A statement
that parses and reads a relation the job never binds is valid SQL that fails at
run time, so stage 6 checks the relations as well as the text.

---

## 4. The two files

### 4.1 `seatunnel.conf`

Every target PostgreSQL table gets two jobs inside **one** file:

```hocon
job {
  shop_orders_ddl  { env{} ddl{} source{} sink{} }
  shop_orders_data { env{} source{} dml{} transform{} sink{} }
}
```

Each block carries one of the three things that are easy to leave implicit:

| Block | Carries | Why it is written out |
| --- | --- | --- |
| `ddl {}` | the `CREATE TABLE` / `PARTITION` / `TABLESPACE` commands, the target column list, `schema_save_mode` | the inferred form is **silent** — a table created with the wrong shape produces no error at creation, only a load that writes the wrong columns |
| `dml {}` | the `INSERT` the sink performs, `write_mode`, `schema_save_mode` | the sink block says *how* the rows move; this says *what the row is* |
| `source {}` | the source query | — |
| `transform {}` | the target query | — |

**A `dml` statement names a PostgreSQL relation on the left and an Oracle one on
the right, so it is not runnable on either engine alone.** It is the write stated
down, so that it can be read against the sink block and the target query and
checked. Do not "fix" this by emitting two separate runnable statements: that
would lose the correspondence that makes it reviewable.

`dml.statement` reads the **same query the transform is fed**, passed in rather
than taken from `job.query`. On a CDC run the two differ — the transform has been
rewritten onto the change stream — so reading `job.query` here would describe a
read the job does not perform.

`RECREATE_SCHEMA` on the `*_ddl` job is destructive by design. It is gated behind
the governance sign-off the console prints, not by a stage in this file.

Splitting the config per table would make the migration unreviewable as one diff
and easy to half-apply, which is why there is one file.

### 4.2 `duckdb.yaml`

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

`env` / `source` / `target` / `check` / `sink`, the same section order as the SeaTunnel
body and the same job ids — so a reader who knows one file can read the other
without a map. There is no separate index: `order` and `depends_on` live in `env`.

Three relations are bound at run time:

| Relation | What it is | What comparing it catches |
| --- | --- | --- |
| `__RAW__<schema>_<table>` | the Oracle relation exported once at the pinned SCN | what the recipes lost |
| `__SOURCE__<job>` | the compiled projection, read at the pinned SCN | what the sink, transport and write mode lost |
| `__TARGET__<job>` | the target relation as the load left it | — |

A check **passes when it returns zero rows**. Every row returned is one violation
and names the column that failed, so a failure is a starting point rather than a
count.

Checks are derived per specification section, never from a fixed list:
`_duckdb_acceptance_checks`, `_duckdb_delivery_checks`,
`_duckdb_fidelity_checks`, `_duckdb_operation_checks`,
`_duckdb_sensitive_checks`, `_duckdb_governance_checks`.

Three conventions in those derivations are load-bearing:

* **row counts compare the projection, not the raw table.** Counting the raw
  relation against the target reports every `row-selection` rule as a lost row.
* **`sensitive` handling is checked as the complement of the promise.** `mask`
  asserts the target value does *not* equal the raw one; `drop` asserts the column
  is *absent*; `keep` asserts it *equals* the raw one.
* **the key is excluded from the delivery comparison.** `USING` already proves it
  equal, and a branch that cannot fail is not a check. A row whose key is NULL is
  invisible to `USING` and is caught by the row-count check instead.

Where a claim cannot be expressed as SQL it is emitted with no `sql` and a
`runtime_strategy` naming what the runner does instead. A check with no SQL is a
statement about work, not a failure.

---

## 5. Where the compiled SQL comes from

```text
base relation (AS OF SCN :run_scn)
   └─ set/deduplicate | escape/sql-set | cardinality/merge   → replaces the source
   └─ relational/denormalise joins                          → FROM
   └─ lookup joins (pre-aggregated)                         → FROM
   └─ projection: base columns, value recipes, derived,
      structural, column-split, lookup takes, pivot
   └─ row-selection                                         → WHERE, wrapped if needed
```

`Chain` builds this from the inside out. Wrapping exists because a filter, a
dedupe or a pivot has to sit **above** a projection when it reads a column that
projection computed — impossible in one flat `SELECT`.

A filter is folded into the projection only when every column it reads is a plain
pass-through. It wraps when the column was **produced** (derived, structural,
lookup, pivot, split) or **rewritten** (a value recipe), because in the second
case the filter must compare the transformed value, not the raw one.

### Where the query lives, per acquisition mode

| `run.acquisition` | query is in | why |
| --- | --- | --- |
| `snapshot` | `source { Jdbc { query } }` | the source block *is* the read |
| `query-incremental` | `source { Jdbc { query } }` | same, plus `> ${run_watermark}` |
| `cdc` | `transform { SQL { query } }` | the source is the change stream; the projection reads `src_<table>` |

---

## 6. Rule precedence

`ordered_rules` sorts by `(specificity, is_override, declaration order)`, lowest
first, so the highest-precedence rule is applied **last** and wins.

```text
spec(objectClass:column, exact schema, exact table, exact column)   18
spec(objectClass:column, exact schema, "*"     table, exact column)  14
spec(objectClass:table,  exact schema, exact table)                  11
```

Two rules on one column:

* **transforms compete** — the highest-precedence one wins and the loser is
  reported as `COLUMN_RULE_SUPERSEDED`. This is what makes an *override replace*
  the rule it overrides instead of stacking on top of it.
* **a rename does not compete** — a rule with no steps is a statement about the
  target name and type. It is folded into whatever transform owns the column.
  `status-is-renamed` renames STATUS to `status_code` while
  `normalize-customer-status` rewrites the value: one column, both effects.

---

## 7. The edge cases, and where they are handled

Each of these is a real Oracle→PostgreSQL difference.

| Case | Handling | Code |
| --- | --- | --- |
| Wildcard scope `SHOP.*`, `TMP_*` exclusion | expanded only with a catalog; exclusions reported | `plan_jobs` |
| Wildcard column rule `{name:"*", column:AMOUNT}` | applied only where the catalog shows the column | `column_rules_for` |
| A rule reading a column the catalog lacks | `BLOCK` | `_validate_catalog_consistency` |
| A rule *producing* a column the source lacks | allowed (`priority-from-amount`) | `_rule_reads_column` |
| Two rules claiming one target name | `BLOCK` (`DUPLICATE_TARGET_COLUMN`) | `compile_job` |
| Two transforms on one column | highest precedence wins, loser reported | `compile_job` |
| Rename plus transform on one column | folded into one column | `compile_job` |
| Several `row-selection` rules | intersected, not first-wins | `combine_predicates` |
| Filter on a computed column | wrapper stage, projected names used | `_requalify_predicate` |
| Filter on a renamed column | source→target map applied | `_requalify_predicate` |
| Oracle empty string ≠ NULL | `NULLIF(x,'')` per `defaults` | `coalesce_nullif_empty` |
| Oracle `CHAR` blank padding | reported (`CHAR_BLANK_PADDING_STRIPPED`) | `register` |
| Oracle `DATE` always has a time | widened to `timestamp(6)`, reported | `TypeResolver` |
| TZ offset loss | `<col>` + `<col>_offset` (minutes), range-checked | `extract_offset_minutes` |
| Binary collation | `COLLATE "C"` on character columns | `TypeResolver` |
| CLOB → `jsonb` per `defaults.json` | applied, validity is a check | `TypeResolver` |
| BLOB → `bytea` | mapped | `ORACLE_TO_PG` |
| `fidelityFloor` | becomes checks, not comments | `_duckdb_fidelity_checks` |
| Identifier > 63 bytes | `shorten-with-hash`, collision-proof | `Naming` |
| Reserved word | quoted, never renamed | `Naming` |
| Lookup fan-out | pre-aggregated by key + match count | `compile_lookup` |
| `onMiss: fail-run` | NULL + a **blocking** miss check | `compile_lookup` |
| `onMiss: quarantine` | `__q_` sentinel column + quarantine sink | `compile_lookup` |
| Pivot unlisted value | counted into a `__q_` sentinel | `compile_pivot` |
| Column-split extra parts | counted into a `__q_` sentinel | `compile_column_split` |
| Merge sources | `UNION ALL`, identical column lists, checked | `compile_merge` |
| Merge source missing a column | `BLOCK` | `_merge_column_order` |
| Split discriminator value | derived, **assumption** + non-empty-branch check | `split_targets` |
| Split branch empty | validation check, not a silent zero rows | `_duckdb_operation_checks` |
| A table consumed by a merge | not also loaded standalone | `plan_jobs` |
| A merge with no driving table | planned explicitly | `plan_jobs` |
| Author-supplied SQL (`escape`, dedupe `sql`) | parsed, and pinned to the run SCN | `stamp_flashback` |
| `escape/sql-scalar` holding a statement | `BLOCK` | `escape_body_to_expression` |
| `escape/sql-set` (an aggregate) | snapshot-only, never a change stream | `compile_job` |
| `change-aware` | sink upsert on the key, not a projection | `compile_job` |
| Missing primary key on an upsert | reported; key dropped by a recipe is `BLOCK` | `_primary_keys` |
| Pivot redefines the key as its grain | key comes from the grain | `_primary_keys` |
| `targetPhysical.partition` | `PARTITION BY RANGE` + `DEFAULT`, bounds admitted | `build_target_ddl` |
| `targetPhysical.tablespace` | separate `ALTER ... SET TABLESPACE` | `build_target_ddl` |
| `run.watermark` on a table without it | applied only where the column exists | `_watermark_predicate` |
| `capture` **and** `watermark` both set | `BLOCK` | `_validate_run` |
| `objectPolicies` `blocked-no-path` | blocks the run | `_validate_object_policies` |
| `sensitive.handling` with no backing rule | `BLOCK` | `_validate_sensitive` |
| A declared mask not visible in the expression | `BLOCK` | `_check_sensitive_delivery` |
| `carry: relocate-to-application` with no signed loss | `BLOCK` | `_validate_object_policies` |
| Unknown step category or operation | `BLOCK`, never dropped | `_validate_step` |
| `SCOPE`/`ACCEPTANCE` field missing | `BLOCK` | `validate_spec` |
| Catalog for a different schema | ignored with one clear finding | `filter_catalog_to_spec` |
| A partitioned target needs its `DEFAULT` partition | emitted as a second `CREATE TABLE` | `build_target_ddl` |

### The DuckDB translation edge cases

| Case | Handling | Code |
| --- | --- | --- |
| `REGEXP_SUBSTR(s,p,1,0)` | `REGEXP_EXTRACT(s,p)` | `_duckdb_rewrite` |
| `REGEXP_SUBSTR(s,p,1,n)` | `LIST_EXTRACT(REGEXP_EXTRACT_ALL(s,p),n)` | `_duckdb_rewrite` |
| `REGEXP_SUBSTR` from an offset | `BLOCK` — an offset scan is not a function call | `_duckdb_rewrite` |
| `STANDARD_HASH(…, 'SHA256')` | `SHA256(…)`; `SHA1`/`MD5` likewise | `_duckdb_rewrite` |
| `STANDARD_HASH(…, 'SHA512')` | `BLOCK` — no DuckDB equivalent | `_duckdb_rewrite` |
| `TO_CHAR(x, mask)` | `BLOCK` — SQLGlot degrades it to `CAST(x AS TEXT)` | `_duckdb_rewrite` |
| `TO_CHAR(x)` | `CAST(x AS TEXT)`, reported as `EDGE` | `_duckdb_rewrite` |
| Any other Oracle-only function | caught by the residual scan, `BLOCK` | `_duckdb_unbindable` |
| A construct DuckDB cannot express at all | `BLOCK`, via `duckdbstrict` | `render_duckdb` |
| `AS OF SCN :run_scn` in a projection | removed; the SCN is the runner's one decision | `_strip_flashback` |
| `:run_watermark` in a projection | becomes a positional `?`, recorded as a bind | `_rewind_binds` |
| A check naming a relation its job does not bind | `BLOCK` at artifact validation | `validate_duckdb_artifacts` |
| A check with no runnable SQL | emitted with a `runtime_strategy` instead | `duckdb_check` |

---

## 8. Dialects

### `ctunnel` = Oracle

Reads the Oracle source. Adds the flashback clause, parsed **between a relation
and its alias**, which is where Oracle places it.

```text
"S"."T" AS OF SCN :run_scn t
```

> Hooking `_parse_table_parts` does **not** work: SQLGlot parses the alias inside
> `_parse_table`, after that hook has already run, so a relation with an alias
> never matches. The clause is intercepted at `_parse_table_alias` instead and
> parked on the parser until `_parse_table` can attach it.

Generation keeps the specification's type names: `numeric(12,2)` stays `NUMERIC`,
`varchar(160)` stays `VARCHAR`. `timestamptz` is deliberately **not** mapped —
forcing it to the literal `"TIMESTAMP WITH TIME ZONE"` makes Oracle's generator
append the zone a second time on the next render.

### `pgcontract` = Postgres

Writes target DDL. Inherits PostgreSQL parsing so every statement is proven
legal, and overrides generation so `numeric(12,2)` stays `NUMERIC` rather than
becoming `DECIMAL`.

`TABLESPACE` is emitted as a separate `ALTER TABLE ... SET TABLESPACE`, because
PostgreSQL does not accept it inside `CREATE TABLE` and sqlglot falls back to
parsing it as an opaque `Command`.

### `duckdbstrict` = DuckDB

Runs the validation rules. Inherits DuckDB, and sets
`Generator.unsupported_level = RAISE`.

This is the whole point of it. SQLGlot's default behaviour when the target
dialect cannot express a construct is to emit a **valid but wrong** statement —
`TO_CHAR(d,'YYYY')` becomes `CAST(d AS TEXT)`, silently losing the formatting and
making a date compare equal to a string. For a validation plan that is the worst
available failure: the check passes for the wrong reason. Raising turns every such
construct into a `BLOCK` here, where the message can name it.

Note that `ToChar` and `TO_NUMBER` do *not* go through `unsupported()`, so
`RAISE` does not catch them. They are handled explicitly in `_duckdb_rewrite`.

---

## 9. Bindings

The transpiler never hardcodes a value.

| Bind | Comes from | Used by |
| --- | --- | --- |
| `:run_scn` → `${run_scn}` | `RUN_RECORD.oracle_scn` | every source read |
| `:run_watermark` → `${run_watermark}` | `RUN_RECORD.watermark` | `query-incremental` runs |

Inside `seatunnel.conf` the form is always `${run_scn}` — the file's substitution
syntax, never `:run_scn`. Inside `duckdb.yaml` the clause is removed entirely and
the runtime supplies the snapshot once.

The SCN is resolved **before** any source read and the same value is used for the
load and for validation. A migration that reads at one snapshot and validates at
another will pass row counts and fail everything else.

---

## 10. Running it

```powershell
pip install -r requirements.txt
python src\transpiler.py
```

That is the whole run. Defaults are `--spec input/migration-spec.yaml` and
`--output-dir output`, and `--catalog` is picked up from `input/catalog.yaml`
when it exists.

```powershell
python src\transpiler.py --spec input\other.yaml --catalog input\other-catalog.yaml
python src\transpiler.py --strict      # any blocking finding exits non-zero
python src\transpiler.py --self-test   # dialects and HOCON/YAML, no inputs
```

`--catalog` is required whenever the specification names objects by wildcard
(`SHOP.*`, `ORDERS_20*`, `column: AMOUNT`). Without it those rules cannot be
enumerated and the compiler says so rather than compiling a subset.

The compiler exits non-zero if either artifact fails re-validation, whether or not
`--strict` is passed. Without `--strict` it always writes both files and reports
what blocks, so the console output can be read.

---

## 11. Adding a rule

1. Add the operation to the right vocabulary set in section 3 of the module.
2. Validate it in `_validate_step`, so an unimplemented shape blocks rather than
   falling through.
3. Compile it to **SQL text** with quoted identifiers — never string-interpolated
   identifiers — and let `render_ctunnel` prove it round-trips.
4. If it changes the row set rather than a column, it shapes the source relation,
   and `Chain.set_source` is the hook. If it adds a column, it goes in the
   projection and passes `produced=True` to `register`.
5. Register the outcome in `job.operations` so the validation rules can cover it.
6. If it uses an Oracle-only function, add a **total** rewrite to
   `_duckdb_rewrite`. A partial one is a `BLOCK`.
7. Add a case to `self_test()` at the bottom of the module that asserts the
   **SQL**, not just that the job compiled. `--self-test` is the only regression
   suite; it needs no input files and is what CI should run.
8. Add a row to the table in section 7.

Do not special-case a rule id. The compiler reads the specification; it does not
know any table by name.

---

## 12. What this compiler deliberately does not do

* **Execute anything.** Both artifacts are generated and every statement in them
  is proven to parse; execution is the two runners' job.
* **Guess.** No catalog means no wildcard expansion. No type means `text` plus an
  assumption. No partition bounds means a `DEFAULT` partition plus an admission.
* **Partially execute.** A blocked job emits no query, no `.conf` entry and no
  validation rules. It fails the run by being absent, which is visible.
* **Write a third file.** Diagnostics go to stdout. If a finding matters enough
  to need persisting, the rule that matters — a check, a sentinel column — goes in
  one of the two files, not in a new document.
* **Claim a behaviour it cannot prove.** `onMiss: fail-run`, `grain.uniqueness:
  asserted`, `mask strategy=hash` and `change-aware` are all enforced by a
  validation rule, because a projection cannot raise, cannot see a previous row
  image, and cannot make a hash reversible.

---

## 13. Known defects

Both are pre-existing — present in `seatunnel.conf` before the DuckDB work — and
both would fail in Oracle as well as DuckDB. Parsing cannot see either, because
both are *legal* SQL that is not *answerable*. They need the generated statements
to be executed against real data to surface.

| Job | Defect | Where |
| --- | --- | --- |
| `shop_order_summary` | an `escape/sql-set` body is aggregated by the recipe planner, which then projects a source column the body never emitted (`"s0"."AMOUNT"` against a subquery that produces `TOTAL`) | `compile_job`, escape path |
| `shop_stock_by_product` | a `pivot` selects a non-aggregated column and groups by the grain only, so the statement is not answerable | `compile_pivot` |

Fixing either means teaching the column planner what the escape body actually
emits, or restricting which recipes may apply on top of a set-level escape.