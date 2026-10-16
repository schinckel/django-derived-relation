# Package modules implementation plan

**Goal:** Split the backport into cohesive modules without changing query behaviour or public imports.

**Architecture:** Convert the module to a package with the approved module boundaries. Keep source interpretation and the Query factory together to avoid import cycles; retain public exports and serialization paths.

**Tech Stack:** Python 3.10+, Django 4.2+, setuptools, tox, unittest.

---

Spec: `docs/plans/2026-10-16-package-modules-design.md`.

Global constraints: use the existing PR #10 worktree and branch; no new backend
behaviour, release, version bump or merge. This is the refactor phase of the
existing green behavioural suite, so do not manufacture failing file-layout
tests. Existing behavioural tests must remain green.

### Task 1: Split the implementation and update packaging

**Files:** replace `django_derived_relation.py` with `django_derived_relation/{__init__,fields,lookups,expressions,joins,compiler,query,queryset,compat}.py`; modify `pyproject.toml`, `tests/derived_relations/tests.py`, `tests/derived_relations/test_composite_output.py`.

1. Run the existing SQLite suite before moving code. Expected: green baseline.
2. Capture an unevaluated PostgreSQL query pickle outside the tracked files for
   a before/after deserialization probe. Include tuple predicates and functions.
3. Move existing definitions intact, add explicit imports, keep the dependency
   direction in the approved design, and update setuptools from py-modules to
   package discovery restricted to django_derived_relation.
4. Adapt the module-file import test for a package. Extend the field clone test
   to reconstruct through its public deconstruction path.
5. Run SQLite, PostgreSQL and the before/after pickle probe. Expected: no query
   changes, no import errors, same results after unpickling.
6. Inspect AST function bodies against the previous module to check this remains
   a code move; format and lint. Expected: unchanged function implementations.

Interfaces: Task 2 consumes the package and metadata produced here.

### Task 2: Verify distribution and publish the PR update

**Files:** add `docs/plans/2026-10-16-package-modules-verification.md`; update PR #10 as needed.

1. Run available tox environments for Django 4.2, 5.0, 5.1, 5.2, 6.0, 6.1 and
   main against PostgreSQL and SQLite using rebuilt installed wheels.
   Expected: existing behavioural suite passes on every available environment.
2. Run release tests, black, isort, flake8, sphinx-lint, git diff --check, build
   wheel/sdist and strict twine metadata validation. Expected: all pass.
3. Inspect archive contents and import public symbols from an installed wheel
   outside the source tree. Check independent internal-module imports in fresh
   processes. Expected: complete package and no import cycles.
4. Review changed functions manually for typing and complexity; no automated
   checker is configured, so report that limitation.
5. Request one fresh review of the module split and serialization/packaging
   boundaries, fix material findings, then rerun relevant checks.
6. Commit with normal hooks and push the existing PR branch. Verify the remote
   SHA and CI status; preserve the worktree for continued PR review.

Review focus: circular imports, wheel/sdist contents, public import/deconstruction
paths, dynamic Query/Compiler factories, old/new query serialization, and the
older-Django tuple fallback. Do not re-review unchanged backend SQL behaviour.
