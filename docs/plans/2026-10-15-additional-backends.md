# Additional Backend Implementation Plan

**Goal:** Attempt CockroachDB, YugabyteDB, Firebird, Db2 and TiDB using real
database execution before advertising support. Snowflake was explicitly
removed from the requested matrix.

**Architecture:** Keep the approved backport approach: scoped query/compiler
adapters, backend-owned compilation and explicit capability overrides. Add
only backend distinctions required by observed behaviour; no core hooks or
global monkey patches. TiDB supports ordinary derived joins only.

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
order, nesting and native locking where supported. TiDB must reject LATERAL.
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
