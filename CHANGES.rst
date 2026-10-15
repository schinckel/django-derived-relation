Changelog
=========

Unreleased
----------

* Support ordinary and LATERAL joins on CockroachDB and YugabyteDB, and
  ordinary joins on Firebird and TiDB with matching third-party backends.
  Reject LATERAL joins on TiDB regardless of its MySQL compatibility version.

* Support ordinary derived joins on SQLite, MySQL, MariaDB, and Oracle, and
  LATERAL joins on MySQL 8.0.14 or later and Oracle.
* Preserve NULL results for unmatched MySQL and Oracle LEFT joins with
  constant or cast projections using ``NO_MERGE`` hints.
* Run MySQL, MariaDB, and Oracle regression jobs on Django 4.2 and 6.1,
  alongside the PostgreSQL and SQLite compatibility matrix.

0.2.0 (2026-10-15)
------------------

* Support Django 4.2 onward, including Python 3.11 with Django 4.2–5.2.

* Allow ``select_for_update()`` on ordinary and LATERAL sources, preserving
  Django's transaction checks and lock options and PostgreSQL's source-row
  locking restrictions.

* Allow registering ``DerivedRelation`` with ``annotate()`` as well as
  ``alias()``, without selecting the descriptor itself.
* Preserve native ``only()`` and ``defer()`` root-model column selection in
  derived sources, including implicit primary-key columns and selected
  annotations. Omitted columns remain unavailable through the derived alias.

0.1.0 (2026-10-13)
------------------

* Initial implementation of ``DerivedRelation`` for ordinary and LATERAL
  queryset joins on PostgreSQL, with named, typed projected columns.
* ``DerivedQuerySet`` and ``DerivedQuerySetMixin`` adapters for Django 5.2
  onward, without global monkey patches.
