"""Named derived-query sources and their projected columns."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from copy import copy
from functools import lru_cache
from typing import TYPE_CHECKING

from django.core.exceptions import EmptyResultSet, FieldError, FullResultSet
from django.db import NotSupportedError, models
from django.db.models import Model, QuerySet
from django.db.models.expressions import Col, ColPairs
from django.db.models.fields import composite
from django.db.models.query_utils import Q, refs_expression
from django.db.models.sql.constants import INNER, LOUTER
from django.db.models.sql.datastructures import BaseTable
from django.db.models.sql.query import Query

if hasattr(models, "DerivedRelation"):
    raise ImportError(
        "Django already provides DerivedRelation. "
        "Use django.db.models.DerivedRelation and replace DerivedQuerySet "
        "with django.db.models.QuerySet."
    )


if TYPE_CHECKING:
    from django.db.backends.base.base import BaseDatabaseWrapper
    from django.db.models.expressions import BaseExpression
    from django.db.models.fields import Field
    from django.db.models.lookups import Lookup
    from django.db.models.sql.compiler import SQLCompiler
    from django.db.models.sql.where import WhereNode


class DerivedCol(Col):
    """A typed projected column whose identity includes its output name."""

    def __init__(self, alias: str, column: str, output_field: Field) -> None:
        super().__init__(alias, output_field)
        self.column = column

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
    ) -> None:
        super().__init__(name, alias)
        self.parent_alias = parent_alias
        self.source = source
        self.condition = condition
        self.join_type = join_type
        self.lateral = lateral
        self.nullable = join_type == LOUTER
        self.source_db = source_db

    def as_sql(
        self, compiler: SQLCompiler, connection: BaseDatabaseWrapper
    ) -> tuple[str, tuple[object, ...]]:
        if self.source_db is not None and self.source_db != connection.alias:
            raise ValueError(
                "DerivedRelation cannot join queries on different databases."
            )
        if not getattr(
            connection.features,
            "supports_derived_joins",
            connection.vendor == "postgresql",
        ):
            raise NotSupportedError("This backend does not support DerivedRelation.")
        if self.lateral and not getattr(
            connection.features,
            "supports_lateral_joins",
            connection.vendor == "postgresql",
        ):
            raise NotSupportedError("This backend does not support LATERAL joins.")
        source = _clone_source_query(self.source)
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
                f"{compiler.connection.ops.quote_name('__derived_empty')} WHERE FALSE)"
            )
        on_sql, on_params = "TRUE", ()
        if self.condition is not None:
            try:
                on_sql, on_params = compiler.compile(self.condition)
            except FullResultSet:
                pass
            except EmptyResultSet:
                on_sql = "FALSE"
        lateral = " LATERAL" if self.lateral else ""
        alias_sql = compiler.connection.ops.quote_name(self.table_alias)
        if self.source.combinator:
            # Django may elide the first operand, so declare its names by position.
            assert self.source.selected is not None
            names = ", ".join(
                compiler.connection.ops.quote_name(column)
                for column in self.source.selected
            )
            alias_sql += f"({names})"
        return (
            f"{self.join_type}{lateral} {sql} " f"{alias_sql} ON ({on_sql})",
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
        return self

    def demote(self) -> DerivedJoin:
        return self

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
        if clone.condition is not None:
            clone.condition.resolve_expression(query, *args, **kwargs)
        return clone


class DerivedRelation:
    """Join a named queryset projection, optionally with left correlation."""

    def __init__(
        self,
        queryset: QuerySet,
        *,
        condition: Q | BaseExpression | None = None,
        join_type: str = "left",
        lateral: bool = False,
    ) -> None:

        if not isinstance(queryset, QuerySet):
            raise TypeError("DerivedRelation requires a QuerySet source.")
        if join_type not in {"left", "inner"}:
            raise ValueError("DerivedRelation join_type must be 'left' or 'inner'.")
        if not isinstance(lateral, bool):
            raise TypeError("DerivedRelation lateral must be a boolean.")
        if condition is not None and not (
            isinstance(condition, Q) or getattr(condition, "conditional", False)
        ):
            raise TypeError(
                "DerivedRelation condition must be Q or a boolean expression."
            )
        self.query = queryset.query.clone()
        self.condition = condition
        self.join_type = INNER if join_type == "inner" else LOUTER
        self.lateral = lateral
        self.source_db = queryset._db
        self.source_for_write: bool = queryset._for_write or _has_source_locks(
            self.query
        )

    def resolve_expression(self, *args: object, **kwargs: object) -> None:
        raise TypeError(
            "DerivedRelation must be registered with alias() or annotate(); "
            "it cannot be used as a scalar expression."
        )

    def _projection(self) -> Query:
        source = _clone_source_query(self.query)
        if not source.selected:
            raise ValueError("DerivedRelation requires at least one projected column.")
        if any(not name or "__" in name or "." in name for name in source.selected):
            raise ValueError(
                "DerivedRelation column names must be nonempty and cannot "
                "contain '__' or '.'."
            )
        return source

    def add_to_query(self, query: Query, name: str) -> None:
        query.check_alias(name)
        if not name or "__" in name or "." in name:
            raise ValueError(
                "DerivedRelation names must be nonempty and cannot contain '__' or '.'."
            )
        fields = query.model._meta.get_fields()
        model_names = {
            name
            for field in fields
            for name in (field.name, getattr(field, "attname", field.name))
        }
        if name == "pk" or name in model_names:
            raise ValueError(
                f"The relation name {name!r} conflicts with a model field."
            )
        if (
            name in query.derived_relations
            or name in query.annotations
            or name in query._filtered_relations
        ):
            raise ValueError(f"The relation name {name!r} is already in use.")
        parent_alias = query.get_initial_alias()
        source = self._projection().resolve_expression(query)
        if not self.lateral and source.get_external_cols():
            raise ValueError("Correlated DerivedRelation sources require lateral=True.")
        alias, _ = query.table_alias(name, create=True)
        columns = self._columns(source, alias, name)
        conflicts = columns.keys() & query.annotations.keys()
        if conflicts:
            raise ValueError(
                f"DerivedRelation columns conflict with aliases: {conflicts}."
            )
        selected_annotations = set(query.annotation_select)
        query.annotations.update(columns)
        query.set_annotation_mask(selected_annotations)
        join = DerivedJoin(
            name,
            alias,
            parent_alias,
            source,
            None,
            self.join_type,
            self.lateral,
            self.source_db,
        )
        query.alias_map[alias] = join
        query.derived_relations = query.derived_relations | {name}
        if self.condition is not None:
            join.condition = query.build_filter(
                self.condition, split_subq=False, update_join_types=False
            )[0]
        # ON needs this source's namespace during resolution, but any ordinary
        # relationship dependencies it introduces must precede the source.
        query.alias_map[alias] = query.alias_map.pop(alias)

    def _columns(self, source: Query, alias: str, name: str) -> dict[str, DerivedCol]:
        columns: dict[str, DerivedCol] = {}
        for column, expression in source.selected.items():
            if isinstance(expression, int):
                expression = source.select[expression]
            elif isinstance(expression, str):
                expression = source.annotations[expression]
            if isinstance(expression, ColPairs):
                raise ValueError("DerivedRelation requires individually named columns.")
            field = copy(expression.output_field)
            field.null = (
                self.join_type == LOUTER
                or field.null
                # Later set-operation operands can supply NULL values.
                or bool(source.combinator)
                or not isinstance(expression, Col)
                or expression.alias not in source.alias_map
                or source.alias_map[expression.alias].join_type == LOUTER
            )
            columns[f"{name}__{column}"] = DerivedCol(alias, column, field)
        return columns


@lru_cache(maxsize=None)
def _compiler_class(base: type[SQLCompiler]) -> type[SQLCompiler]:
    return type("DerivedCompiler", (DerivedCompilerMixin, base), {})


@lru_cache(maxsize=None)
def _query_class(base: type[Query]) -> type[Query]:
    if issubclass(base, DerivedQueryMixin):
        return base
    return type("DerivedQuery", (DerivedQueryMixin, base), {"_derived_base": base})


def _restore_query(base: type[Query], state: dict[str, object]) -> Query:
    query = object.__new__(_query_class(base))
    query.__dict__.update(state)
    return query


def _has_source_locks(query: Query) -> bool:
    """Find locks whose write-routing flag a compound queryset can lose."""
    return (
        query.select_for_update
        or any(_has_source_locks(branch) for branch in query.combined_queries)
        or any(
            _has_source_locks(join.source)
            for join in query.alias_map.values()
            if isinstance(join, DerivedJoin)
        )
    )


def _clone_source_query(query: Query, fields: tuple[str, ...] | None = None) -> Query:
    """Validate and adapt a cloned source tree without changing its operands."""
    if query.select_related:
        raise ValueError(
            "DerivedRelation sources using select_related() "
            "require an explicit values() projection."
        )
    clone = query.clone()
    # Alias relabeling edits table_map lists; Query.clone() only copies the map.
    clone.table_map = {table: aliases[:] for table, aliases in clone.table_map.items()}
    clone.__class__ = _query_class(type(clone))
    clone._derived_source_has_select_fields = query.has_select_fields
    if clone.selected is not None:
        clone.selected = clone.selected.copy()
    if fields is not None and clone.selected is None:
        clone.set_values(fields)
    # Propagate explicit values() projections as Django does. An inferred
    # projection must not clear a later operand's independent deferred mask.
    selected = tuple(clone.selected) if clone.selected is not None else None
    clone.combined_queries = tuple(
        _clone_source_query(branch, selected) for branch in clone.combined_queries
    )
    if clone.selected is None:
        # Match each operand's native selection, including implicit primary
        # key components, before set_values() clears deferred loading.
        select_mask = set(composite.unnest(clone.get_select_mask()))
        names = clone.values_select or tuple(
            field.attname
            for field in clone.model._meta.concrete_fields
            if not select_mask or field in select_mask
        )
        clone.set_values((*names, *clone.annotation_select))
    return clone


class DerivedCompilerMixin:
    def collapse_group_by(
        self, expressions: list[BaseExpression], having: list[BaseExpression]
    ) -> list[BaseExpression]:
        if any(isinstance(expression, DerivedCol) for expression in expressions):
            return expressions
        return super().collapse_group_by(expressions, having)

    def get_distinct(self) -> tuple[list[str], list[tuple[object, ...]]]:
        result, params = [], []
        fields = self.query.distinct_fields
        try:
            for name in fields:
                if name.split("__")[0] in self.query.derived_relations:
                    sql, column_params = self.compile(self.query.resolve_ref(name))
                    result.append(sql)
                    params.append(column_params)
                else:
                    self.query.distinct_fields = (name,)
                    sql, column_params = super().get_distinct()
                    result.extend(sql)
                    params.extend(column_params)
        finally:
            self.query.distinct_fields = fields
        return result, params


class DerivedQueryMixin:
    derived_relations: frozenset[str] = frozenset()
    _derived_source_has_select_fields: bool | None = None

    @property
    def has_select_fields(self) -> bool:
        # Named source columns are an internal projection. Preserve Django's
        # ordering rules for originally bare operands, including hidden columns.
        if self._derived_source_has_select_fields is not None:
            return self._derived_source_has_select_fields
        return super().has_select_fields

    def clone(self) -> Query:
        clone = super().clone()
        # Compilation can add compound-ordering columns to selected and joins
        # to table_map. Keep those changes on the compiler's private clone.
        if clone.selected is not None:
            clone.selected = clone.selected.copy()
        clone.table_map = {
            table: aliases[:] for table, aliases in clone.table_map.items()
        }
        return clone

    def __reduce__(self) -> tuple[
        Callable[[type[Query], dict[str, object]], Query],
        tuple[type[Query], dict[str, object]],
    ]:
        return _restore_query, (self._derived_base, self.__dict__)

    def get_compiler(
        self,
        using: str | None = None,
        connection: BaseDatabaseWrapper | None = None,
        elide_empty: bool = True,
    ) -> SQLCompiler:
        compiler = super().get_compiler(using, connection, elide_empty)
        compiler.__class__ = _compiler_class(type(compiler))
        return compiler

    def set_values(self, fields: tuple[str, ...]) -> None:
        selected = set(self.annotation_select)
        selected.update(
            name
            for name in fields
            if isinstance(self.annotations.get(name), DerivedCol)
        )
        self.set_annotation_mask(selected)
        super().set_values(fields)

    def resolve_ref(
        self,
        name: str,
        allow_joins: bool = True,
        reuse: set[str] | None = None,
        summarize: bool = False,
    ) -> BaseExpression:
        root, transforms = refs_expression(name.split("__"), self.annotations)
        expression = self.annotations.get(root)
        if isinstance(expression, DerivedCol):
            expression = expression.copy()
            if not allow_joins:
                raise FieldError(
                    "Joined field references are not permitted in this query"
                )
            for transform in transforms:
                expression = self.try_transform(expression, transform)
            return expression
        return super().resolve_ref(name, allow_joins, reuse, summarize)

    def build_filter(
        self, filter_expr: object, *args: object, **kwargs: object
    ) -> tuple[WhereNode, set[str]]:
        clause, joins = super().build_filter(filter_expr, *args, **kwargs)
        if isinstance(filter_expr, tuple) and kwargs.get("current_negated"):
            root, _ = refs_expression(filter_expr[0].split("__"), self.annotations)
            expression = self.annotations.get(root)
            if isinstance(expression, DerivedCol):
                condition = clause.children[0]
                for null_condition, connector in expression.negated_null_conditions(
                    condition, condition.rhs, self
                ):
                    clause.add(null_condition, connector)
        return clause, joins

    def resolve_expression(
        self, query: Query, *args: object, **kwargs: object
    ) -> Query:
        clone = super().resolve_expression(query, *args, **kwargs)
        for alias, source in clone.alias_map.items():
            if isinstance(source, DerivedJoin):
                clone.alias_map[alias] = source.resolve_external(query, *args, **kwargs)
        return clone

    def get_external_cols(self) -> list[Col]:
        columns = super().get_external_cols()
        for branch in self.combined_queries:
            columns.extend(branch.get_external_cols())
        for source in self.alias_map.values():
            if isinstance(source, DerivedJoin):
                columns.extend(
                    col
                    for col in source.get_external_cols()
                    if col.alias in self.external_aliases
                )
        return columns

    def combine(self, rhs: Query, connector: str) -> None:
        if self.derived_relations or getattr(rhs, "derived_relations", ()):
            raise NotSupportedError(
                "Boolean queryset combinations with DerivedRelation are not "
                "supported; use Q conditions."
            )
        super().combine(rhs, connector)


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
        clone = super()._annotate(args, {}, select)
        names = {
            name
            for field in self.model._meta.get_fields()
            for name in (field.name, getattr(field, "attname", field.name))
        }
        for name, value in kwargs.items():
            if name.split("__")[0] in clone.query.derived_relations:
                raise ValueError(f"The relation name {name!r} is already in use.")
            if isinstance(value, DerivedRelation):
                if name in names:
                    raise ValueError(
                        f"The relation name {name!r} conflicts with a model field."
                    )
                value.add_to_query(clone.query, name)
                clone._for_write = clone._for_write or value.source_for_write
            else:
                clone = super(DerivedQuerySetMixin, clone)._annotate(
                    (), {name: value}, select
                )
        return clone

    def update(self, **kwargs: object) -> int:
        if self.query.derived_relations:
            raise NotSupportedError(
                "Calling update() with DerivedRelation is not supported."
            )
        return super().update(**kwargs)

    def delete(self) -> tuple[int, dict[str, int]]:
        if self.query.derived_relations:
            raise NotSupportedError(
                "Calling delete() with DerivedRelation is not supported."
            )
        return super().delete()


class DerivedQuerySet(DerivedQuerySetMixin, QuerySet):
    """Convenience queryset with derived joins; no global classes are patched."""
