# AGENT.md — migration transpiler: the flow

This is the operating document for anyone or anything working on this project.
It states what the compiler guarantees, the order the pipeline must run in, and
the edge cases that are easy to get wrong. Read it before changing
`src/transpiler.py`.

---

## 1. What this is

One approved migration specification in; **two files** out, one per runner, in a
folder named after the specification.

```text
input/<any>.yaml                    the approved specification -- source of truth
input/catalog.yaml                  optional: Oracle source metadata, when the
                                    specification does not state its own columns
        |
        v
src/transpiler.py                   one module, every rule
        |
        +--> output/<spec>/seatunnel.conf   ONE HOCON file
        |        for every target relation a <schema>_<table>_ddl job and a
        |        <schema>_<table>_data job, each carrying the source query, the
        |        target query, the DDL or DML commands, and schema_save_mode
        |
        +--> output/<spec>/duckdb.yaml      ONE YAML file
                 the validation rules the specification implies, the compiled
                 query for each of them in DuckDB, and the source queries they
                 compare against
```

A catalog file is **optional**. A table rule that states its own `columns:` is
self-sufficient, and that is the recommended shape for a small hand-reviewed
migration. `need_catalog.md` explains what the compiler needs the shape for, the
three places it will look for it, and when a catalog extracted from Oracle is
still the right answer.

There are no other outputs. No orchestrator, no scheduler, no index, no report,
and **no findings on the console** — see §3a for where a finding goes instead.

Every specification gets its own folder, so compiling a new one cannot overwrite
another's, and recompiling one leaves the rest untouched.

The specification is never edited. If it says something this compiler cannot
express, the job is omitted and the omission named in the file header — never
approximated.

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
 1. parse_catalog            Oracle metadata, if any                 --
 2. filter_catalog_to_spec   drop a catalog about other schemas      --
 3. merge_inline_source_tables  fold in each rule's own `columns:`   --
 4. validate_spec            the specification is sound             omit what fails
 5. plan_jobs                scope x rules -> one plan per target   --
 6. compile_job              one plan -> query, columns, DDL       blocked jobs skip
 7. validate_query           every query round-trips byte-stably    omit what drifts
 8. validate_source_query    in the SOURCE engine's own dialect     omit what fails
 9. validate_target_query    in the TARGET engine's own dialect     omit what fails
10. validate_seatunnel_query the .conf itself, re-read              exit non-zero
11. validate_duckdb_query    the .yaml itself, re-read              exit non-zero
12. emit                     output/<spec>/{seatunnel.conf, duckdb.yaml}
```

Stage 3 sits where it does because everything after it reads the catalog and
should not care where a shape came from. See §7 "Where the source shape comes
from".

Stages 4 and 7–9 decide **what goes in the files**. A rule that fails them is
omitted, and the omission is named in the file header — that is the record, not a
console line. Stages 10–11 validate the compiler's **own output**, so they are the
only stages that change the exit code: if a generated file cannot be read back and
re-parsed, that is a defect in this compiler, not a finding about the
specification. There is no stage 12 onward; the two runners take it from there.

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

## 3a. Where a finding goes

The console does not print findings, and that is a decision rather than an
omission. A `BLOCK`, a `GOVERNANCE` finding, an `ASSUMPTION` and an `EDGE` are all
statements about whether a *migration* is correct — and this program does not run
migrations. It emits two files. The runner does.

So each finding is carried into the artifact it concerns, where the person
responsible for the run will meet it:

| Was | Goes into | As |
| --- | --- | --- |
| `BLOCK` on a job | both file headers | the relation is listed as producing no job, **with the code and message that stopped it** |
| `GOVERNANCE` | `duckdb.yaml`, in the job body | a `V-GOVERNANCE-*` check whose `notes` quote the finding |
| `ASSUMPTION` | `duckdb.yaml`, in the job body | the `assumptions` list |
| `EDGE` | `duckdb.yaml`, in the job body | the `notes` on the affected check |
| type unknown without a catalog | `seatunnel.conf` | the type in the `ddl` block, which is written out |
| shape came from the spec, not Oracle | both files | the `assumptions` list, so a reader knows the DDL is not evidence about the database |

The general rule: **if a finding would change what a reader of the two files
should believe, it goes in the file.** A finding nobody can see in the artifact
they are about to run is not recorded, and a console list nobody reads is not
better.

What remains on the console is the receipt — which specification, which folder,
which files, how big, how many jobs — plus one exception: a *generated file* that
failed stage 5 or 6. That is a statement about this compiler, so it is printed
and exits non-zero.

This is also why `--strict` is gone. There is no severity left for it to gate on.

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
| A specification with `\#`, U+00A0 or tabs | named before parsing, exit 2 | `load_yaml` |
| A catalog beside a `--spec` elsewhere | that one wins over the project root | `_load_optional_catalog` |
| `scope.include` with `schema: "*"` | enumerates the catalog; without one, `SCOPE_WILDCARD_NEEDS_CATALOG` | `plan_jobs` |
| `scope` names relations, all then excluded | `PLANNER_EMPTY_RESULT` names them | `plan_jobs` |
| `scope` and rules match nothing at all | *not* an error: an empty migration is legitimate | `plan_jobs` |
| Two relations resolving to one `target.table` | `TARGET_COLLISION`, rather than overwriting on load | `plan_jobs` |
| A rule naming a table the catalog lacks | still migrated; its columns become assumptions | `plan_jobs` |
| A table rule with its own `columns:` and no catalog | compiled; the shape is `SOURCE_SHAPE_FROM_SPEC` | `merge_inline_source_tables` |
| A relation whose columns nothing describes | `SOURCE_SHAPE_UNKNOWN`, the job is blocked | `compile_job` |

### Where the source shape comes from

A migration compiles to a projection, and a projection is a list of columns. The
specification says what to *do* to columns; it does not list the ones no rule
touches, and those still have to migrate. So the compiler needs the source's
**shape** — column names, Oracle types, character semantics, key — from one of
three places, in order of trust. `need_catalog.md` is the full argument; this is
the mechanism.

| Source | What it is | How it is supplied |
| --- | --- | --- |
| a catalog | extracted from Oracle, authoritative | `--catalog`, or `catalog:` in the spec, or auto-searched |
| the rule's own `columns:` | the spec stating the shape outright | `rules[].columns` / `rules[].primaryKey` |
| the columns the rules name | a floor, not a plan | `SOURCE_SHAPE_FROM_RULES` |

`merge_inline_source_tables` folds case 2 into the catalog before anything reads
it, so the key check, the watermark and type inference all see one catalog
regardless of where the shape came from. Synthesised tables carry
`from_spec=True`, which is how a job knows to say so.

**The catalog wins.** A catalog is read out of the database; an inline list is
written by a person. When both exist for one relation the catalog is used and the
inline list ignored, because a stale hand-written list silently governing a
migration is worse than a redundant one.

**A wildcard-schema rule's `columns:` is ignored.** It describes every table it
matched, so its columns cannot be attributed to one relation without a catalog to
say what the relations are. Wildcards genuinely require a catalog —
`SCOPE_WILDCARD_NEEDS_CATALOG` says so rather than enumerating nothing.

**A relation nothing describes is blocked.** No catalog, no `columns:`, no rule
naming its columns means there is no projection to compile, so the job is
`BLOCKED` with `SOURCE_SHAPE_UNKNOWN` and is not emitted. A job with no query must
never become a job with an empty projection, which looks like a migration and
loads nothing.

### Why `jobs: 0` must never be silent

The worst thing this compiler can emit is two empty files. It looks like a
migration with nothing to do rather than one that could not be resolved, and a
reader has no way to tell which.

Three separate guards exist because "empty" has three different causes:

| Cause | Truth | Reported by |
| --- | --- | --- |
| the catalog was filtered away by a wildcard-schema bug | a defect | the filter keeps wildcards — see below |
| relations resolved, then all excluded or consumed | truthful, but invisible | `PLANNER_EMPTY_RESULT`, naming each |
| the scope genuinely matched nothing | truthful and expected | nothing; an empty migration is legitimate |

The first is the one that bit. `catalog_schemas` collected the *text* `"*"` as a
wanted schema name. The filter then compared each table's real schema against
`{"*"}`, matched none, and discarded the entire catalog — after which
`catalog.expand()` had nothing to enumerate and `plan_jobs()` correctly produced
nothing. Two functions in a chain, one of them doing exactly what it was written
to do.

A wildcard is not a schema. `catalog_schemas` now drops it, an empty result means
"do not filter the catalog", and a `schema: "*"` scope enumerates it.

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

That is the whole run. The defaults are anchored to the **project**, not the
working directory:

| Default | Value |
| --- | --- |
| `--spec` | `PROJECT_ROOT/input/migration-spec.yaml` |
| `--catalog` | `PROJECT_ROOT/input/catalog.yaml`, if it exists |
| `--output-dir` | `PROJECT_ROOT/output` |

`PROJECT_ROOT` is derived from `Path(__file__).parent.parent`, not from `cwd`. An
editor that launches the script with the workspace root as its cwd is the common
case, and that is not the directory the script lives in — resolving the defaults
against `cwd` would make the same command behave differently depending on where
it was typed. A relative path passed explicitly is still relative to `cwd`.

`--catalog` is searched for **beside the specification first**, then the project
root, then the cwd. Compiling `--spec input/other.yaml` must pick up
`input/catalog.yaml` rather than the root catalog that belongs to a different
specification. An explicit `--catalog` is never searched for elsewhere, so a typo
is reported instead of silently falling back.

**No catalog is required.** `--catalog` is optional, and when it is absent the
compiler reads each table rule's own `columns:` / `primaryKey:`, or falls back to
the columns the rules themselves name. The receipt states what happened:

```text
catalog     : none supplied (0 table(s))
```

`input/hr-spec.yaml` is the worked example — no `catalog:` line, no catalog file
beside it, and it compiles to both artifacts. `need_catalog.md` covers the rest:
what the shape is needed for, why a rule cannot supply it on every table, and when
an Oracle-extracted catalog is still the right answer.

## Reading a specification

`load_yaml` raises `SpecReadError`, never a YAML traceback, and `main` prints it
with exit code 2. Nothing is written on a read failure.

The diagnosis runs **before** parsing. That order is the whole point: PyYAML
reports where it stopped, which for an escaped comment marker is the first such
line, not the cause. A reader that only paraphrases the parser would send someone
to fix line 3 of a 1000-line file.

Recognised without help from the parser:

| Accident | How it is detected |
| --- | --- |
| `\#` at the start of a line | the line, stripped, starts with `\#` |
| U+00A0 anywhere | counted; reported with a one-line command to fix it |
| tab indentation | reported against the line it is on |
| root is a list / a scalar / empty | reported with what a specification needs |
| not UTF-8 | reported by `read_text`, before any parsing |

Anything else falls through to `yaml.safe_load`, and its error is then dressed
with the file, the line and column, the offending line, a caret, and — when the
parser named a comment or blank line — the nearest preceding line that looks like
the key it was expecting. That last part matters: a missing `:` on `rules` makes
PyYAML blame the comment two lines below it.

All three accidents come from copying a file through a tool that escapes Markdown
punctuation or reformats indentation. They are the cases worth naming, because
the parser's own message for each is actively misleading.

```powershell
python src\transpiler.py                                              # the default spec
python src\transpiler.py --spec input\my-new-migration.yaml           # any spec
python src\transpiler.py --spec input\hr.yaml --catalog input\hr-catalog.yaml
python src\transpiler.py --output-dir D:\build                        # a different root
python src\transpiler.py --self-test                                  # no inputs needed
```

`--catalog` is required whenever the specification names objects by wildcard
(`SHOP.*`, `ORDERS_20*`, `column: AMOUNT`). Without it those rules cannot be
enumerated, so the relations they name are omitted rather than compiled as a
partial guess.

Exit codes: `0` compiled, `1` a generated file did not survive stage 5 or 6,
`2` the specification could not be read. There is no severity gate — see §3a.

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
* **Guess.** No catalog means no wildcard expansion, and the relations it would
  have supplied produce no job. No type means `text` plus a note in the file. No
  partition bounds means a `DEFAULT` partition.
* **Partially compile.** A relation the compiler could not resolve emits no job,
  no `.conf` entry and no validation rules. It is named in the file header, so
  its absence is visible rather than inferred from a row count.
* **Write a third file, or print findings.** Both were removed. If a finding
  matters enough to record, the rule that carries it — a check, an `assumptions`
  entry, a type in a `ddl` block — goes in one of the two files. A finding printed
  to a console and then scrolled away is not recorded anywhere.
* **Judge the migration.** `onMiss: fail-run`, `grain.uniqueness: asserted`,
  `mask strategy=hash` and `change-aware` are all expressed as validation rules,
  because a projection cannot raise, cannot see a previous row image, and cannot
  make a hash reversible — and answering them is DuckDB's job, not this one's.

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