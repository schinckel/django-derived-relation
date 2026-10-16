"""Named output metadata for multi-column table expressions."""

from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING

from django.db import models

from .lookups import _SourceTupleIn, tuple_lookups

if TYPE_CHECKING:
    from django.db.models import Model
    from django.db.models.fields import Field
    from django.db.models.lookups import Lookup


class CompositeField(models.Field):
    """Named output metadata for a table expression, never a model field."""

    is_composite = True

    def __init__(self, *fields: Field, **named_fields: Field) -> None:
        if fields and named_fields:
            raise TypeError("Use positional named fields or keyword fields, not both.")
        self.fields = tuple(copy(field) for field in fields or named_fields.values())
        if named_fields:
            for name, field in zip(named_fields, self.fields, strict=True):
                field.name = name
                field.set_attributes_from_name(name)
        names = [field.name for field in self.fields]
        if len(names) < 2 or any(not name for name in names):
            raise ValueError("CompositeField requires at least two named fields.")
        if len(set(names)) != len(names):
            raise ValueError("CompositeField output names must be unique.")
        if any(isinstance(field, CompositeField) for field in self.fields):
            raise ValueError("Nested CompositeField outputs are not supported.")
        super().__init__()

    def deconstruct(self) -> tuple[str | None, str, list[object], dict[str, object]]:
        return (
            self.name,
            "django_derived_relation.CompositeField",
            list(self.fields),
            {},
        )

    def get_lookup(self, lookup_name: str) -> type[Lookup] | None:
        if lookup_name == "in":
            return _SourceTupleIn
        return tuple_lookups.get(lookup_name) or super().get_lookup(lookup_name)

    def contribute_to_class(
        self, cls: type[Model], name: str, private_only: bool = False
    ) -> None:
        raise TypeError(
            "CompositeField describes expression output, not a model field."
        )
