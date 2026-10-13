Changelog
=========

Unreleased
----------

* Initial implementation of ``DerivedRelation`` for ordinary and LATERAL
  queryset joins on PostgreSQL, with named, typed projected columns.
* ``DerivedQuerySet`` and ``DerivedQuerySetMixin`` adapters for Django 5.2
  onward, without global monkey patches.
* Import-time rejection when Django provides its own ``DerivedRelation``.
* Documentation covering grouped results, historical rates, and geographic
  matches.
* Automated Python/Django compatibility testing and tag-triggered PyPI
  publishing through Trusted Publishing.
