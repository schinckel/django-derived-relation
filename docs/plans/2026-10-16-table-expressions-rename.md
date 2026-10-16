# Table expressions rename implementation plan

**Goal:** Rename the distribution, import package and existing GitHub repository to describe the DEP 24 table-expression API.

**Architecture:** Use `django-table-expressions` for the distribution and repository, and `django_table_expressions` for imports. Rename the existing repository to preserve history and GitHub redirects; retain the existing PR branch and PR #10.

**Tech Stack:** Python, Django, setuptools, tox, GitHub Actions, PyPI Trusted Publishing.

---

## Approved design

The user approved the new distribution/module names, then requested renaming
the existing repository with a redirect. The new names have no current PyPI
project listing or repository under schinckel. GitHub documents automatic web
and Git redirects when renaming an existing repository.

Update active documentation, packaging, imports, field deconstruction, release
URLs, and test-service database names together. Preserve historical changelog
entries and prior plans as records; explain the import migration in the README.
Old module imports and pickles are not promised across this breaking rename.
Keep version 0.2.0 unchanged: publishing a new release is outside this task.

Do not rename the local checkout or unrelated worktrees. The shared origin URL
will point at the renamed repository. PyPI's existing publisher configuration
cannot establish publishing authority for the new project/repository name;
document the exact pending publisher settings needed before the first release.

### Task 1: Rename the package and active references

**Files:** `django_derived_relation/` becomes `django_table_expressions/`;
`pyproject.toml`, `README.rst`, `CHANGES.rst`, `.github/workflows/release.yml`,
`.github/workflows/tests.yml`, `tests/runtests.py`, and package-importing tests.

1. Rename the package directory and replace imports, the public deconstruction
   path and setuptools package selection with `django_table_expressions`.
2. Update distribution/repository metadata and release URLs. Change active
   README examples and add a migration note identifying both old/new names.
   Keep the README explicit that the renamed project has no published release.
3. Update matching CI and test runner database defaults together. Avoid
   changing test model definitions or behaviour.
4. Add a changelog entry for the renamed distribution and import path.

### Task 2: Verify and update the existing repository/PR

1. Run installed-wheel PostgreSQL/SQLite tox environments for Django 4.2, 5.0,
   5.1, 5.2, 6.0, 6.1 and main. Existing pickle and field reconstruction tests
   exercise the renamed namespace; no file-placement tests are needed.
2. Run formatting, lint, release tests, whitespace checks, wheel/sdist builds
   and strict metadata validation. Inspect the archives for the new namespace
   and import the installed wheel outside the checkout.
3. No type or complexity analyser is configured. Review affected functions
   manually and verify the rename does not alter control flow or annotations.
4. Rename `schinckel/django-derived-relation` to
   `schinckel/django-table-expressions`, retaining repository id 1416424083.
   Verify old web URLs redirect and PR #10 survives; update origin and the
   attached PR URL.
5. Commit using normal hooks, push the existing branch, and update the PR body
   to use the new package/repository names. Check the remote SHA and CI status.

## Release prerequisite

Before releasing, configure a PyPI pending publisher for project
`django-table-expressions`, owner `schinckel`, repository
`django-table-expressions`, workflow `release.yml`, environment `pypi`.
Do not modify PyPI account security settings as part of the code rename.

## References

- https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository
- https://docs.pypi.org/trusted-publishers/troubleshooting/
- https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/

## Verification

- All 156 tests pass per PostgreSQL/SQLite installed-wheel run on Django 4.2,
  5.0, 5.1, 5.2, 6.0, 6.1 and development Django, with existing backend skips.
- Wheel and source archive contain only the new package namespace; public
  imports, installed metadata and field reconstruction work outside the checkout.
- Package ASTs match the prior implementation after normalising the renamed
  deconstruction path. Function control flow and annotations are unchanged.
- Black, isort, flake8, sphinx-lint, five release tests, whitespace checks,
  distribution builds and strict twine validation pass.
- No automated type or complexity analyser is configured; both were reviewed
  manually. Automated verification of those properties is not claimed.
- GitHub retains repository id 1416424083 and open PR #10. The old repository
  web URL returns HTTP 301 pointing to the renamed repository. Origin now uses
  the new SSH URL. No new repository, version tag or release was created.
- PyPI pending publisher configuration remains a prerequisite for publication.
