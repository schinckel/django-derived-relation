Changelog
=========

Unreleased
----------

* Allow registering ``DerivedRelation`` with ``annotate()`` as well as
  ``alias()``, without selecting the descriptor itself.

0.1.0 (2026-10-13)
------------------

* Initial implementation of ``DerivedRelation`` for ordinary and LATERAL
  queryset joins on PostgreSQL, with named, typed projected columns.
* ``DerivedQuerySet`` and ``DerivedQuerySetMixin`` adapters for Django 5.2
  onward, without global monkey patches.
