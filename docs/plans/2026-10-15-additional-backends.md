# Additional Backend Implementation Plan

**Goal:** Attempt CockroachDB, YugabyteDB, Firebird, Db2 and TiDB using real
database execution before advertising support.

**Architecture:** Keep the approved backport approach: scoped query/compiler
adapters, backend-owned compilation and explicit capability overrides. Add
only backend distinctions required by observed behaviour; no core hooks or
global monkey patches. Verify TiDB correlation by server version and join
type; the earlier ordinary-only description is not a scope restriction.

**Tech Stack:** Django, the published third-party backend packages, official
database containers, tox and GitHub Actions.

---

### 1. Establish compatibility

Read published package metadata and backend implementations. Attempt actual
installation, dependency checks and Django initialization for each selected
combination. Use django-cockroachdb matching Django 4.2/6.1; the published
YugabyteDB backend targets Django 4.2; Firebird releases target Django 5.2/6.1;
Db2 publishes adapters for Django 4.2/5.2; TiDB targets Django 4.2/6.0.
These are initial candidates, not support claims.

### 2. Execute before enabling support

Files: `tests/runtests.py`, `tests/derived_relations/tests.py`, optional dedicated
third-party regression tests and CI helpers.

Create isolated, disposable databases with official images. Confirm native ORM
setup/CRUD before derived joins, then observe the adapter's unsupported-backend
failure. Run ordinary and correlated left/inner joins, projection, grouping,
ordering/slicing, conditions after slicing, constants/empty sources, parameter
order, nesting and native locking where supported. Check TiDB's ordinary,
inner lateral and left lateral joins independently.
Do not substitute PostgreSQL/MySQL execution or mocked SQL for the real engine.
An incompatible backend remains unsupported with an evidence-backed reason.

### 3. Implement minimal compatibility changes

File: `django_derived_relation.py`.

Enable only verified backend identities. Detect TiDB through the actual
backend wrapper, since it uses vendor `mysql`; keep it out of MySQL's LATERAL
and optimizer-hint paths. Firebird correlation remains disabled after real
Firebird 5 execution returned incorrect NULL results for a correlated LEFT
join; ordinary joins pass on Django 5.2 and 6.1.
Preserve native backend compiler cleanup and conversions. Reproduce failures
with regression tests before fixing them; do not rewrite third-party drivers.

### 4. Maintain the verified matrix

Files: `tox.ini`, `.github/workflows/tests.yml`, `README.rst`, `CHANGES.rst`.

Add installed-wheel execution jobs only for combinations that passed real
tests. Keep failures of experimental combinations visible while investigating;
do not turn them into green skips or advertise unverified support. Record
backend package and server versions, exact tested Django ranges and material
native restrictions. Keep PR descriptions to a supported-platform table.

### 5. Verify and publish the feature branch

Run the existing PostgreSQL/SQLite regression matrix, format/lint/RST checks,
release-tool tests, packaging validation and workflow validation. Review changed
functions' annotations and complexity; report absent automated analyzers.
Commit with normal hooks, open a separate PR against main, and inspect real CI
results. Do not merge, tag or publish the pending release in this task.

### Execution findings

* PostGIS 3.5.2 on PostgreSQL 17.5 passes all 121 tests with Django 4.2.30
  and 6.1.2 (10/9 expected skips). Five spatial execution tests cover ordinary
  spatial ON conditions, correlated filtering and nearest-neighbour slicing,
  geometry and transformed geometry projection, and distance/area conversion
  and lookups. The native PostGIS backend inherits the PostgreSQL join paths;
  no production changes are needed. Dedicated CI jobs use the native backend
  and system GEOS/GDAL libraries.
* CockroachDB 26.3.1 passes ordinary and lateral joins with published adapters
  4.2 and 6.1 on Django 4.2.30 and 6.1.2.
* YugabyteDB 2026.1.2.0 passes both join types with django-yugabytedb 4.2.0.1
  on Django 4.2.30. Its native deferred-FK check fails after TestCase savepoint
  rollback, reproduced with plain Django QuerySet and no derived joins. Tests
  use TransactionTestCase with committed fixtures and ordinary Django flushes.
* Firebird 5.0.3 passes ordinary joins with django-firebird 5.0.4/5.1.1 on
  Django 5.2.18/6.1.2. Its lateral LEFT join lifts projected outer references
  above NULL extension, including with an OFFSET 0 wrapper. Lateral joins stay
  disabled. Native SQL also rejects FOR UPDATE inside derived tables.
* TiDB 8.5.6 rejects LATERAL syntax. TiDB 8.5.7 and 8.5.8 execute INNER JOIN
  LATERAL, reject LEFT JOIN LATERAL, and reject implicit correlation and APPLY.
  Nine inner-join execution regressions cover multiplicity, ordering/slicing,
  post-slice ON conditions, empty sources, grouping, aggregation, parameter
  order, outer-reference projections, dependent sources and nested subqueries.
  All 116 tests pass on 8.5.7/8.5.8 with django-tidb 4.2.6/6.0.1 and Django
  4.2.30/6.0.9 (67/68 expected skips). Native server execution contradicts the
  stable documentation's parsing-only description. Tests use the real TiDB SQL
  server with the unistore local store. The native tidb_version descriptor
  identifies the backend; its own version controls inner lateral support,
  independently of MySQL's compatibility version and optimizer-hint paths.
* Db2 12.1.0.0 accepts native queries using ibm-db-django 1.5.3.0/1.6.0.0.
  The Django 4.2 adapter omits run_syncdb during test creation, and remote
  teardown fails with SQL1032N; that combination stays unverified.
  The Django 5.2 adapter needs its undeclared Jinja2 import installed. Its
  compiler moves all locking clauses to the outermost SELECT, so source locks
  must be rejected rather than silently broadening their scope. All 107 tests
  pass (18 skips) on the actual Db2 server after these adjustments, including
  ordinary and lateral joins, with the installed wheel on Django 5.2.18.

No type checker or cognitive-complexity analyzer is configured. Changed
functions were reviewed manually for annotations and control flow.
