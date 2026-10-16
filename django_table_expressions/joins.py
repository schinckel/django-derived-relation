"""Explicit join controls and backend SQL for table sources."""

from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING

from django.core.exceptions import EmptyResultSet, FullResultSet
from django.db import NotSupportedError
from django.db.models import QuerySet
from django.db.models.query_utils import Q
from django.db.models.sql.constants import INNER, LOUTER
from django.db.models.sql.datastructures import BaseTable
from django.db.models.sql.query import Query

from .fields import CompositeField

if TYPE_CHECKING:
    from django.db.backends.base.base import BaseDatabaseWrapper
    from django.db.models.expressions import BaseExpression, Col
    from django.db.models.sql.compiler import SQLCompiler
    from django.db.models.sql.where import WhereNode


class Join:
    """Explicit table-source interpretation and join controls."""

    def __init__(
        self,
        source: QuerySet | BaseExpression,
        *,
        join_type: str,
        condition: Q | BaseExpression | None = None,
    ) -> None:
        if not isinstance(source, QuerySet) and not getattr(
            source, "table_source", False
        ):
            raise TypeError("Join requires a queryset or a table-source expression.")
        if join_type not in {"left", "inner"}:
            raise ValueError("Join join_type must be 'left' or 'inner'.")
        if condition is not None and not (
            isinstance(condition, Q) or getattr(condition, "conditional", False)
        ):
            raise TypeError("Join condition must be Q or a boolean expression.")
        self.source = source
        self.join_type = LOUTER if join_type == "left" else INNER
        self.condition = condition

    @property
    def default_alias(self) -> str:
        raise TypeError("Join requires an explicit alias.")

    def resolve_expression(self, *args: object, **kwargs: object) -> None:
        raise TypeError("Join must be registered with alias().")


class DerivedJoin(BaseTable):
    """A retained FROM source with an explicitly chosen join type."""

    def __init__(
        self,
        name: str,
        alias: str,
        parent_alias: str,
        source: Query,
        condition: WhereNode | None,
        join_type: str,
        lateral: bool,
        source_db: str | None = None,
        inferred: bool = False,
    ) -> None:
        super().__init__(name, alias)
        self.parent_alias = parent_alias
        self.source = source
        self.condition = condition
        self.join_type = join_type
        self.lateral = lateral
        self.nullable = join_type == LOUTER
        self.source_db = source_db
        self.inferred = inferred

    def as_sql(
        self, compiler: SQLCompiler, connection: BaseDatabaseWrapper
    ) -> tuple[str, tuple[object, ...]]:
        if self.source_db is not None and self.source_db != connection.alias:
            raise ValueError(
                "Table sources cannot join queries on different databases."
            )
        if not getattr(
            connection.features,
            "supports_derived_joins",
            connection.vendor in {"postgresql", "sqlite", "mysql", "oracle"},
        ):
            raise NotSupportedError("This backend does not support derived joins.")
        if self.lateral and not getattr(
            connection.features,
            "supports_lateral_joins",
            _supports_lateral_joins(connection),
        ):
            raise NotSupportedError("This backend does not support LATERAL joins.")
        source = self.source.clone()
        try:
            sql, params = compiler.compile(source)
        except EmptyResultSet:
            # A LEFT join must emit outer rows even with a known-empty source.
            source.subquery = False
            sql, params = source.get_compiler(
                connection=connection, elide_empty=False
            ).as_sql()
            # Aggregates without GROUP BY can emit a row despite WHERE FALSE.
            # Preserve the source's EmptyResultSet contract for every shape.
            sql = (
                f"(SELECT * FROM ({sql}) "
                f"{compiler.connection.ops.quote_name('__derived_empty')} WHERE 1=0)"
            )
        if connection.vendor == "oracle":
            # View merging can lift constants above an outer join and turn
            # unmatched NULL projections into their non-NULL source values.
            sql = f"(SELECT /*+ NO_MERGE */ * FROM {sql})"
        elif connection.vendor == "mysql" and not connection.mysql_is_mariadb:
            # MySQL can also lift constant expressions across LEFT joins.
            # Target the wrapper's local alias without introducing a name
            # that could shadow a correlated outer table.
            alias = connection.ops.quote_name(self.table_alias)
            sql = f"(SELECT /*+ NO_MERGE({alias}) */ * FROM {sql} {alias})"
        on_sql, on_params = "1=1", ()
        if self.condition is not None:
            try:
                on_sql, on_params = compiler.compile(self.condition)
            except FullResultSet:
                pass
            except EmptyResultSet:
                on_sql = "1=0"
        lateral = " LATERAL" if self.lateral else ""
        if self.inferred and self.join_type == INNER and self.condition is None:
            return (
                f"CROSS JOIN{lateral} {sql} "
                f"{compiler.connection.ops.quote_name(self.table_alias)}",
                tuple(params),
            )
        return (
            f"{self.join_type}{lateral} {sql} "
            f"{compiler.connection.ops.quote_name(self.table_alias)} ON ({on_sql})",
            (*params, *on_params),
        )

    def relabeled_clone(self, change_map: dict[str, str]) -> DerivedJoin:
        clone = copy(self)
        clone.parent_alias = change_map.get(self.parent_alias, self.parent_alias)
        clone.table_alias = change_map.get(self.table_alias, self.table_alias)
        clone.source = self.source.relabeled_clone(change_map)
        if self.condition is not None:
            clone.condition = self.condition.relabeled_clone(change_map)
        return clone

    def promote(self) -> DerivedJoin:
        clone = copy(self)
        if self.inferred:
            clone.join_type = LOUTER
        return clone

    def demote(self) -> DerivedJoin:
        clone = copy(self)
        if self.inferred:
            clone.join_type = INNER
        return clone

    @property
    def identity(self) -> tuple[object, ...]:
        return (
            *super().identity,
            self.source,
            self.condition,
            self.join_type,
            self.lateral,
            self.source_db,
        )

    def get_external_cols(self) -> list[Col]:

        columns = self.source.get_external_cols()
        if self.condition is not None:
            columns.extend(Query._gen_cols([self.condition], include_external=True))
        return columns

    def resolve_external(
        self, query: Query, *args: object, **kwargs: object
    ) -> DerivedJoin:
        clone = self.relabeled_clone({})
        clone.source = clone.source.resolve_expression(query, *args, **kwargs)
        if isinstance(clone.source, Query):
            clone.lateral = bool(clone.source.get_external_cols())
        if clone.condition is not None:
            clone.condition = clone.condition.resolve_expression(query, *args, **kwargs)
        return clone


class FunctionJoin(DerivedJoin):
    """Compile an opted-in function as a source, preserving its vendor method."""

    def get_external_cols(self) -> list[Col]:
        columns = list(Query._gen_cols([self.source]))
        if self.condition is not None:
            columns.extend(Query._gen_cols([self.condition]))
        return columns

    def as_sql(
        self, compiler: SQLCompiler, connection: BaseDatabaseWrapper
    ) -> tuple[str, tuple[object, ...]]:
        if connection.vendor not in {"postgresql", "sqlite"}:
            raise NotSupportedError("Table functions are unverified on this backend.")
        sql, params = compiler.compile(self.source)
        quote = connection.ops.quote_name
        alias = quote(self.table_alias)
        lateral = " LATERAL" if connection.vendor == "postgresql" else ""
        if connection.vendor == "postgresql":
            output = self.source.output_field
            fields = output.fields if isinstance(output, CompositeField) else [output]
            names = [
                field.db_column or field.name or self.table_alias for field in fields
            ]
            alias += "(" + ", ".join(quote(name) for name in names) + ")"
        if self.join_type == INNER and self.condition is None:
            return f"CROSS JOIN{lateral} {sql} {alias}", tuple(params)
        on_sql, on_params = "1=1", ()
        if self.condition is not None:
            try:
                on_sql, on_params = compiler.compile(self.condition)
            except FullResultSet:
                pass
            except EmptyResultSet:
                on_sql = "1=0"
        return (
            f"{self.join_type}{lateral} {sql} {alias} ON ({on_sql})",
            (*params, *on_params),
        )


def _supports_lateral_joins(connection: BaseDatabaseWrapper) -> bool:
    if connection.vendor == "mysql":
        return not connection.mysql_is_mariadb and connection.mysql_version >= (
            8,
            0,
            14,
        )
    return connection.vendor in {"postgresql", "oracle"}
