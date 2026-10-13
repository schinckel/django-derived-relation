Changelog
=========

Unreleased
----------

* Initial implementation of ``DerivedRelation`` for ordinary and LATERAL
  queryset joins on PostgreSQL, with named, typed projected columns.
* ``DerivedQuerySet`` and ``DerivedQuerySetMixin`` adapters for Django 5.2
  onward, without global monkey patches.
