# SQL Server Implementation Plan

**Goal:** Support ordinary and APPLY-backed correlated derived relations on SQL Server.

**Architecture:** Preserve the existing QuerySet API and backend compiler. Add
SQL Server rendering in DerivedJoin, placing conditions around the compiled
source so they run after its slice. Add installed-wheel CI on real SQL Server.

**Tech Stack:** Django 4.2–6.1, mssql-django, pyodbc, SQL Server 2022, tox, GitHub Actions.

---

### Task 1: APPLY rendering and semantics

**Files:** `tests/derived_relations/tests.py`, `django_derived_relation.py`.

1. Add typed compilation regressions for APPLY operator selection and condition
   placement, and execution regressions for a sliced source whose first row
   fails the condition. Check true/false predicates, alias scope and parameters.
2. Run tests before implementation; expect unsupported-backend failure.
3. Enable vendor `microsoft` for ordinary/correlated sources. Render a lateral
   join as `OUTER APPLY` or `CROSS APPLY`; wrap a conditioned source as
   `(SELECT alias.* FROM source alias WHERE condition) alias`.
4. Run the full PostgreSQL/SQLite suite and dialect compilation regressions.

### Task 2: Real SQL Server CI

**Files:** `tests/runtests.py`, `tox.ini`, `.github/workflows/tests.yml`.

1. Add `--sqlserver` settings, optional backend/driver tox factors, and SQL Server
   test gates. Keep the existing default PostgreSQL/SQLite environments.
2. Add service jobs for Django 4.2 and 6.1 on Linux x86-64; install Microsoft's
   ODBC driver, wait for a real SQL connection, and test the installed wheel.
3. Run actionlint and inspect tox's resolved dependencies/commands.
4. Use CI to execute the complete applicable suite. Investigate native/backend
   failures rather than silently disabling coverage or emulating SQL Server.

### Task 3: Documentation, review and publication

**Files:** `README.rst`, `CHANGES.rst`.

1. Document the backend, APPLY translation, native limitations and test commands.
2. Run Black, isort, flake8, RST lint, release-tool tests, build and twine.
3. Review changed functions for annotations and cognitive complexity; report
   that neither automated type nor complexity analysis is configured.
4. Obtain one whole-branch review. Fix material findings with regression tests.
5. Commit with normal hooks, push `codex/sql-server-support`, and open a PR
   against main with only a supported-platform table in its description.
6. Confirm CI passes. Leave tagging/publication for after this PR merges.
