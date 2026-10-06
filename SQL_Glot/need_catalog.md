# Do I need a catalog file?

**No, not for a specification that says what its tables contain.** A spec file
with a `columns:` list on each table rule compiles on its own:

```
python src/transpiler.py --spec input/hr-spec.yaml
```

`input/hr-spec.yaml` is exactly that case — no `catalog:` line, no catalog file
beside it, and it produces both artifacts. This file explains what the compiler
actually needs, the three ways it can get it, and which situations still want a
catalog file rather than a hand-written list.

---

## 1. The one thing the compiler cannot invent

A migration compiles to a projection, and a projection is a list of columns:

```sql
SELECT "ORDER_ID", "CUSTOMER_ID", "ORDER_DATE", "AMOUNT", "STATUS"
FROM "HR"."ORDERS" AS OF SCN ${run_scn}
```

Three things have to be known to write that, and **none of them are properties of
the transformation**:

| Needed | Why |
| --- | --- |
| Every column of the source relation | A rule says what to *do* to columns. It says nothing about the columns no rule touches — and those still have to migrate. |
| Each column's Oracle type | `NUMBER(12,2)` becomes `NUMERIC(12, 2)`, `VARCHAR2(20)` becomes `VARCHAR(20) COLLATE "C"`, and `DATE` stays `DATE`. Without the type the DDL has no type to write. |
| Each column's character semantics | `VARCHAR2` counts bytes or characters depending on the column. That decides whether a `substr` needs a different argument on the target, and whether a length can be trusted. |
| The source primary key | It becomes the target's primary key. A change-aware (upsert) write cannot run without one, and the compiler will not invent one. |

A specification is a document about *change*. `AMOUNT` becomes money; `PHONE_OLD`
is dropped; `EMAIL` is masked. Read any migration spec and you will find it naming
the columns it touches. You will not find it listing the forty columns it leaves
alone — because listing those is not a decision, it is metadata, and metadata is
what a catalog is for.

So the question is never "is a catalog required?" It is: **where does this
particular specification get its column metadata from?**

---

## 2. The three sources, in order of trust

The compiler looks in three places and stops at the first that answers.

### a. `--catalog file.yaml`, or `catalog: file.yaml` in the spec

An Oracle-extracted catalog. Authoritative, because it was read out of the
database rather than written by a person:

```yaml
catalog: catalog_hr.yaml

tables:
  - schema: HR
    name: ORDERS
    primaryKey: [ORDER_ID]
    columns:
      - { name: ORDER_ID, type: "NUMBER(10)", nullable: false }
      - { name: STATUS,    type: "VARCHAR2(20)" }
```

Also searched automatically when the spec names none: `catalog.yaml`,
`catalog.yml`, `<stem>_catalog.yaml`, `catalog_<stem>.yaml`, beside the spec.

### b. The table rule's own `columns:` — no catalog file at all

A table rule may state what its own relation contains. The shape is identical to a
catalog entry, so a rule reads the same either way:

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

Two things worth being precise about:

- **`columns:` describes the source, not the target.** It is the rule's `match`,
  not its `target`. A rule matching `HR.ORDERS` says what `HR.ORDERS` contains,
  whatever the table it lands in is called.
- **The catalog still wins.** If both exist for the same relation, the catalog is
  used and the inline list is ignored, with the reason recorded in the artifacts
  (`SOURCE_SHAPE_FROM_SPEC`). A stale hand-written list silently governing a
  migration is a worse failure than a redundant one — the catalog is what Oracle
  actually has.

This is the recommended shape for a **small, stable, hand-authored** migration:
one file to review, one file to change, and no way for the two halves to disagree.

### c. Whatever the rules happen to name — a floor, not a plan

If neither of the above applies, the compiler projects only the columns the
spec's own steps mention. That is enough for a rule-driven table whose rules touch
everything, and it is recorded as an assumption:

```
SOURCE_SHAPE_FROM_RULES
  no catalog describes SHOP.ORDERS, so the projection covers only the 7
  column(s) this specification's own rules name ([AMOUNT, CURRENCY, ...]).
  Any column the rules never mention is not migrated, and no type is known for
  these, so the target column type comes from the rules that touch it
```

This is how `input/migration-spec.yaml` compiles: it is a reference document
covering all thirteen step categories against a `SHOP` schema that exists in no
estate, so it names columns constantly and needs no catalog.

It is a floor. Do not rely on it for a real migration — a column no rule mentions
is silently not migrated.

### When nothing describes the shape

If a relation's columns are named nowhere — no catalog, no `columns:`, no rule
touching them — the job is **blocked**, not narrowed:

```
SOURCE_SHAPE_UNKNOWN   BLOCK
  nothing describes the columns of HR.ORDERS, so there is no projection to
  compile and this job would load nothing. Either give the table rule a
  `columns:` list, or supply a catalog for the source schema
```

Both artifacts carry that line in their header, naming the job and the reason. A
job with no query is never emitted as a partial guess.

---

## 3. When you should still write a catalog file

The inline list is a declaration. A catalog is an observation. Reach for the
catalog when any of these is true.

**Wildcards in scope.** `schema: "*"` cannot be resolved from a specification:
`*` names every schema, and only the database knows which schemas exist. A
wildcard *table* name (`ORDERS_*`) has the same problem one level down. Without a
catalog there is nothing to enumerate, and the compiler says so rather than
compiling an empty migration. See `input/hr-spec-wildcard.yaml`.

**Any table you do not want to type out.** A 40-table migration is 400 lines of
YAML that nobody will review carefully, and one typo in a column name is a bug
that surfaces as a runtime failure. Extract it:

```sql
SELECT table_schema, table_name, column_name, data_type,
       data_length, data_precision, data_scale, nullable
  FROM all_tab_columns
 WHERE owner = 'HR'
 ORDER BY table_name, column_id;
```

**Many relations sharing one schema.** Writing `columns:` per rule repeats the
same schema prefix forty times and gives forty places to disagree. One catalog
entry per table is the same information in fewer places.

**Anything whose drift matters.** A hand-written list is correct until someone
adds a column in Oracle. Then the spec silently under-migrates — and a generated
artifact is the last place you would look. A catalog regenerated from the database
turns that from a silent omission into a diff you can review. This is the argument
for the catalog, and it is the strongest one.

**Anything meant to be run.** For illustrative or reference specs (the SHOP
schema), the inline list is fine. For a migration that will actually execute, take
the metadata from Oracle.

---

## 4. Deciding

| Situation | Use |
| --- | --- |
| Illustrative spec, no real source | `columns:` inline, or rely on the rules naming them |
| Small migration, one or two tables, hand-reviewed | **`columns:` inline** |
| Any wildcard in `scope.include` | catalog — required |
| More than a handful of tables | catalog |
| A migration that will really run | catalog |
| Tables whose shape changes often | catalog, regenerated per run |

A specification can mix: catalog for the schema with fifty tables, `columns:`
inline for the one that is new and not in the catalog yet. The catalog wins for
the tables it has; the inline list covers the rest.

---

## 5. Finding out which one a spec is using

The receipt printed on every run states it outright:

```
catalog     : none supplied (1 table(s))
```

- `none supplied (0 table(s))` — no catalog. The spec is carrying its own shape,
  or the rules name enough columns. Check for `SOURCE_SHAPE_FROM_SPEC` or
  `SOURCE_SHAPE_FROM_RULES` in the artifacts to see which.
- `none supplied (N table(s))` where N > 0 — the spec's own `columns:` lists were
  read. `N` is how many relations they described.
- a filename — that catalog was read.

The generated artifacts also carry the per-job reasoning in their headers, and
every assumption in an `assumptions:` list in `duckdb.yaml`. If a column did not
make it, the reason is in one of those two places.