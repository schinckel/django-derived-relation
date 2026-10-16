# Package module verification

Date: 2026-10-16.

This record covers the structural refactor following the DEP 24 backport.
Backend SQL and query behaviour have not changed. AST comparison confirms that
all 77 function definitions, bodies and annotations match the previous module.

## Behaviour and compatibility

The installed-wheel tox matrix passed against PostgreSQL and SQLite, with 156
tests per run and the existing backend-dependent skips:

| Python | Django |
| --- | --- |
| 3.11 | 4.2.30, 5.0.14, 5.1.15, 5.2.18 |
| 3.13 | 6.0.9, 6.1.2, 6.2 development |

The before/after serialization probes also passed:

- Four queries pickled before the split on Django 5.2 load and produce the same
  SQL and parameter values after it: correlated LEFT join, whole-row predicate,
  scalar table function and composite-output table function.
- A pre-split Django 4.2 tuple query loads and compiles. This caught missing
  legacy tuple lookup globals; compatibility aliases now preserve those names.
- Existing query/queryset pickle round-trip tests pass in the full matrix.
- `CompositeField` reconstructs through its unchanged public deconstruction
  path, including component names and physical column names.
- All eight internal modules import in fresh processes.

## Distribution and static checks

- Built wheel and source archive contain all nine package modules and omit the
  deleted single-file module. The wheel excludes the tests and tools packages.
- Public imports and field cloning work from the installed wheel outside the
  checkout on Django 4.2 and 5.2.
- Black, isort, flake8, sphinx-lint, release metadata tests (5 tests), strict
  twine metadata validation and whitespace checks pass.
- Semantic diff and a fresh read-only review found no correctness issues.
- No automated type checker or cognitive complexity analyser is configured.
  Typing and complexity were reviewed manually; moved function annotations and
  control flow are unchanged. Automated typing/complexity checks are not claimed.

The prior backend execution evidence remains in
`2026-10-16-dep24-backport-verification.md`. This refactor does not add backend
support, publish a release or merge the PR.
