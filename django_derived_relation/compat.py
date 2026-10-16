"""Inspect query projections across supported Django versions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import expressions
from django.db.models.expressions import RawSQL
from django.db.models.sql.query import Query

if TYPE_CHECKING:
    from django.db.models.expressions import BaseExpression
_composite_col_types: tuple[type[BaseExpression], ...] = (
    (expressions.ColPairs,) if hasattr(expressions, "ColPairs") else ()
)


def _projected_expressions(source: Query) -> dict[str, BaseExpression]:
    """Read selected expressions across Django's projection representations."""
    selection = getattr(source, "selected", None)
    if selection is None:
        return {
            **{name: RawSQL(*args) for name, args in source.extra_select.items()},
            **dict(zip(source.values_select, source.select, strict=True)),
            **source.annotation_select,
        }
    projection: dict[str, BaseExpression] = {}
    for column, expression in selection.items():
        if isinstance(expression, int):
            expression = source.select[expression]
        elif isinstance(expression, str):
            expression = source.annotations[expression]
        projection[column] = expression
    return projection
