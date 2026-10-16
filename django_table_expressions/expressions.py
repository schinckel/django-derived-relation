"""Typed projected columns and lazy table-source expressions."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from copy import copy
from typing import TYPE_CHECKING

from django.db.models import expressions
from django.db.models.expressions import Col, Expression
from django.db.models.sql.constants import LOUTER
from django.db.models.sql.query import Query

from .compat import _projected_expressions
from .fields import CompositeField

if TYPE_CHECKING:
    from django.db.backends.base.base import BaseDatabaseWrapper
    from django.db.models.expressions import BaseExpression
    from django.db.models.fields import Field
    from django.db.models.lookups import Lookup
    from django.db.models.query_utils import Q
    from django.db.models.sql.compiler import SQLCompiler


class DerivedCol(Col):
    """A typed projected column whose identity includes its output name."""

    def __init__(self, alias: str, column: str, output_field: Field) -> None:
        super().__init__(alias, output_field)
        self.column = column

    @property
    def name(self) -> str:
        return self.column

    def __reduce__(
        self,
    ) -> tuple[
        Callable[[str, str, Field, bool], DerivedCol], tuple[str, str, Field, bool]
    ]:
        # Model fields pickle as references to their canonical model field.
        # Preserve the projected column's independently inferred nullability.
        return _restore_derived_col, (
            self.alias,
            self.column,
            self.output_field,
            self.output_field.null,
        )

    def as_sql(
        self, compiler: SQLCompiler, connection: BaseDatabaseWrapper
    ) -> tuple[str, tuple[object, ...]]:
        return (
            ".".join(
                map(compiler.connection.ops.quote_name, (self.alias, self.column))
            ),
            (),
        )

    def relabeled_clone(self, relabels: dict[str, str]) -> DerivedCol:
        return self.__class__(
            relabels.get(self.alias, self.alias), self.column, self.output_field
        )

    def negated_null_conditions(
        self, condition: Lookup, rhs: object, query: Query
    ) -> list[tuple[Lookup, str]]:
        if condition.lookup_name == "isnull" or condition.rhs is None:
            return []
        columns = [self]
        if isinstance(rhs, Col):
            columns.append(rhs)
        conditions: list[tuple[Lookup, str]] = []
        for column in columns:
            table = query.alias_map.get(column.alias)
            if column.output_field.null or (table and table.join_type == LOUTER):
                include_none = (
                    column is self
                    and condition.lookup_name == "in"
                    and isinstance(condition.rhs, Iterable)
                    and not isinstance(condition.rhs, (str, bytes))
                    and any(value is None for value in condition.rhs)
                )
                conditions.append(
                    (
                        column.get_lookup("isnull")(column, bool(include_none)),
                        "OR" if include_none else "AND",
                    )
                )
        return conditions


def _restore_derived_col(
    alias: str, column: str, output_field: Field, nullable: bool
) -> DerivedCol:
    field = copy(output_field)
    field.null = nullable
    return DerivedCol(alias, column, field)


class TableSource(Expression):
    """Lazy resolved source shared by bare querysets and explicit joins."""

    contains_aggregate = False
    contains_over_clause = False

    def __init__(
        self,
        source: Query | BaseExpression,
        *,
        join_type: str | None = None,
        condition: Q | BaseExpression | None = None,
        source_db: str | None = None,
    ) -> None:
        self.source = source
        self.join_type = join_type
        self.condition = condition
        self.source_db = source_db
        self.scalar_function = not isinstance(source, Query) and not isinstance(
            source.output_field, CompositeField
        )
        self.prior_sources: frozenset[str] = frozenset()
        super().__init__()

    def resolve_expression(
        self, query: Query, *args: object, **kwargs: object
    ) -> TableSource:
        clone = super().resolve_expression(query, *args, **kwargs)
        clone._resolve_output()
        return clone

    def _resolve_output(self) -> None:
        source = self.source
        if isinstance(source, Query):
            self.projection = _projected_expressions(source)
        else:
            output = source.output_field
            output_fields = (
                output.fields if isinstance(output, CompositeField) else [output]
            )
            self.projection = {
                field.name or "": expressions.Value(None, output_field=field)
                for field in output_fields
            }
        fields: list[Field] = []
        for name, expression in self.projection.items():
            field = copy(expression.output_field)
            if name:
                field.name = name
                field.set_attributes_from_name(name)
            fields.append(field)
        output = CompositeField(*fields) if len(fields) > 1 else fields[0]
        self.output_field = output

    def get_source_expressions(self) -> list[BaseExpression]:
        return [self.source]

    def set_source_expressions(self, expressions: list[BaseExpression]) -> None:
        self.source = expressions[0]

    def as_sql(
        self, compiler: SQLCompiler, connection: BaseDatabaseWrapper
    ) -> tuple[str, tuple[object, ...]]:
        raise ValueError(
            "Select table-source columns individually, not the whole source."
        )
