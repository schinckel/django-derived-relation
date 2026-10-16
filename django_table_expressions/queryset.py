"""Opt-in querysets for derived table sources."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import NotSupportedError
from django.db.models import QuerySet

from .joins import Join
from .query import _query_class

if TYPE_CHECKING:
    from django.db.models import Model
    from django.db.models.sql.query import Query


class DerivedQuerySetMixin:
    """Add derived joins to a queryset; place before its base in the MRO."""

    def __init__(
        self,
        model: type[Model] | None = None,
        query: Query | None = None,
        using: str | None = None,
        hints: dict[str, object] | None = None,
    ) -> None:
        super().__init__(model=model, query=query, using=using, hints=hints)
        self.query.__class__ = _query_class(type(self.query))

    def _annotate(
        self, args: tuple[object, ...], kwargs: dict[str, object], select: bool = True
    ) -> QuerySet:
        clone = super()._annotate(args, kwargs, select)
        for value in kwargs.values():
            original = value.source if isinstance(value, Join) else value
            if isinstance(original, QuerySet):
                clone._for_write = clone._for_write or original._for_write
        return clone

    def update(self, **kwargs: object) -> int:
        if getattr(self.query, "_source_joins", {}):
            raise NotSupportedError(
                "Calling update() with table sources is not supported."
            )
        return super().update(**kwargs)

    def delete(self) -> tuple[int, dict[str, int]]:
        if getattr(self.query, "_source_joins", {}):
            raise NotSupportedError(
                "Calling delete() with table sources is not supported."
            )
        return super().delete()


class DerivedQuerySet(DerivedQuerySetMixin, QuerySet):
    """Convenience queryset with derived joins; no global classes are patched."""
