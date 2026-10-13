Changelog
=========

Unreleased
----------

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
