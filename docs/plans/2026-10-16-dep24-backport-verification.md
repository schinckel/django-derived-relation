# DEP 24 backport verification

Date: 2026-10-16. Implementation branch: `codex/dep24-backport`.

## Delivered behaviour

- Bare multi-column queryset aliases and optional explicit `Join` controls share
  source metadata, lazy materialisation and typed column resolution.
- Queryset correlation is inferred, including nested outer references.
- PostgreSQL scalar/composite functions and SQLite numeric JSON table functions
  use the same source path with backend-specific rendering.
- Existing explicit LEFT/INNER, ON, null-extension, locking, custom-manager and
  compiler-dispatch cases were migrated. The old descriptor was removed.
- Unused aliases do not multiply rows; ordinary scalar querysets, scalar
  `Subquery`, `Exists`, and scalar combined subqueries retain native behaviour.

The implementation started with Django 5.2 as requested. Django 4.2 then needed
local tuple lookups and compiler output aliases. Output aliasing preserves the
native select structure so row-lock `OF` targets remain valid. No global Django
patches were introduced.

Explicit `Join` retains bare model `only()`/`defer()` behaviour. Bare DEP sources
require projections. Alias replacement follows native annotation behaviour;
references already resolved to an earlier source keep their own join.

## Execution evidence

The installed-wheel tox matrix ran 156 tests per PostgreSQL and SQLite run:

| Python | Django | PostgreSQL | SQLite |
| --- | --- | --- | --- |
| 3.11 | 4.2.30 | Pass | Pass |
| 3.11 | 5.0.14 | Pass | Pass |
| 3.11 | 5.1.15 | Pass | Pass |
| 3.11 | 5.2.18 | Pass | Pass |
| 3.13 | 6.0.9 | Pass | Pass |
| 3.13 | 6.1.2 | Pass | Pass |
| 3.13 | 6.2 development | Pass | Pass |

Expected capability/version skips are included in each run. PostgreSQL executes
correlation, tuple membership, DISTINCT ON, locking and function cases. SQLite
executes ordinary sources, local tuple compatibility and implicit function
correlation. There are 52 more behavioural tests than the 104-test base suite.

The final Django 5.2 suite also passes on MySQL 8.4, MariaDB 11.4 and Oracle
Free 23.26.3, with backend-inapplicable tests skipped. Earlier migration checks
passed those backends on Django 6.1, and MySQL/MariaDB on Django 4.2.

Five spatial execution checks pass on PostgreSQL 17.5/PostGIS 3.5.2 with Django
5.2: geometry conversion, spatial ON conditions, correlated slicing, distance
and area measures, and transformed geometry outputs. These used the existing
spatial probe fixtures adapted to `Join`; they do not merge the separate
additional-backends PR into this branch.

Local Oracle 4.2 execution is unverified. The cached `cx_Oracle` runtime crashed
in native connection creation, including a minimal Django connection with no
adapter imported. Oracle 5.2/6.1 use `oracledb` and pass. The configured Oracle
4.2 CI job uses its separate XE21/x86 client runtime; do not call it locally
verified based on these runs.

## Review and validation

A fresh whole-branch review identified four issues, each reproduced before its
fix: compilation changing later projection semantics, DISTINCT ON pruning its
source, scalar UNION subqueries being rejected, and missing ON-dependency
validation. New regressions pass, including cycles introduced by alias
reassignment. Compiler join decisions now affect a clone of the query.

Black, isort, flake8, sphinx-lint, whitespace checks, five release-metadata tests,
wheel/sdist builds and strict metadata validation pass. Semantic diff and manual
complexity/annotation reviews were performed. No repository type checker or
cognitive-complexity analyser is configured; automated verification of those
properties remains unavailable.

## Boundaries

Explicit join controls are a backport extension, not an accepted core API.
Combined *table* sources, Boolean queryset combinations with sources, arbitrary
function portability and scalar-subquery reuse are not implemented. Function
joins outside PostgreSQL/SQLite fail explicitly. The package version and release
tags were not changed; no release was published.
