"""Named derived-query sources and their projected columns."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from copy import copy
from functools import lru_cache
from typing import TYPE_CHECKING, TypeAlias

from django.core.exceptions import EmptyResultSet, FieldError, FullResultSet
from django.db import NotSupportedError, models
from django.db.models import Model, QuerySet, expressions
from django.db.models.expressions import Col, Expression, RawSQL, Subquery
from django.db.models.lookups import Lookup
from django.db.models.query_utils import Q, refs_expression
from django.db.models.sql.compiler import SQLCompiler
from django.db.models.sql.constants import INNER, LOUTER
from django.db.models.sql.datastructures import BaseTable
from django.db.models.sql.query import Query
from django.db.models.sql.where import WhereNode

_SQLParams: TypeAlias = tuple[object, ...] | list[object]
_SQLFragment: TypeAlias = tuple[str, _SQLParams]
_SelectList: TypeAlias = list[tuple["BaseExpression", _SQLFragment, str | None]]
_OrderList: TypeAlias = list[tuple["BaseExpression", tuple[str, _SQLParams, bool]]]

try:
    from django.db.models.fields.tuple_lookups import Tuple, tuple_lookups
except ImportError:
    tuple_lookups = {}

    class Tuple(expressions.Func):
        """Typed row expression for Django versions before tuple expressions."""

        function = ""
        allows_composite_expressions = True

        def __len__(self) -> int:
            return len(self.source_expressions)

        def __iter__(self) -> Iterator[BaseExpression]:
            return iter(self.source_expressions)

    class _TupleLookup(Lookup):
        """Local tuple lookups using ordinary Boolean lookup expressions."""

        def get_prep_lookup(self) -> object:
            if self.lookup_name == "isnull":
                if not isinstance(self.rhs, bool):
                    raise ValueError("Tuple isnull requires a boolean.")
                return self.rhs
            if not isinstance(self.rhs, (tuple, list, Tuple)):
                raise ValueError("Tuple lookups require tuple values.")
            rows = self.rhs if self.lookup_name == "in" else [self.rhs]
            if any(
                not isinstance(row, (tuple, list, Tuple)) or len(row) != len(self.lhs)
                for row in rows
            ):
                raise ValueError("Tuple lookup requires one value per output column.")
            return self.rhs

        def _comparison(self, values: Iterable[object], operation: str) -> WhereNode:
            columns = list(self.lhs)
            values = list(values)
            if len(values) != len(columns):
                raise ValueError("Tuple lookup requires one value per output column.")
            equals = [
                column.get_lookup("exact")(column, value)
                for column, value in zip(columns, values)
            ]
            if operation == "exact":
                return WhereNode(equals, connector="AND")
            branches = []
            strict = "gt" if operation.startswith("g") else "lt"
            for index, (column, value) in enumerate(zip(columns, values)):
                operator = operation if index == len(columns) - 1 else strict
                branches.append(
                    WhereNode(
                        [*equals[:index], column.get_lookup(operator)(column, value)],
                        connector="AND",
                    )
                )
            return WhereNode(branches, connector="OR")

        def as_sql(
            self, compiler: SQLCompiler, connection: BaseDatabaseWrapper
        ) -> tuple[str, tuple[object, ...]]:
            if self.lookup_name == "isnull":
                if not isinstance(self.rhs, bool):
                    raise ValueError("Tuple isnull requires a boolean.")
                clause = WhereNode(
                    [
                        column.get_lookup("isnull")(column, self.rhs)
                        for column in self.lhs
                    ],
                    connector="OR" if self.rhs else "AND",
                )
            elif self.lookup_name == "in":
                clause = WhereNode(
                    [
                        self._comparison(values, "exact")
                        for values in self.rhs
                        if all(value is not None for value in values)
                    ],
                    connector="OR",
                )
                if not clause.children:
                    raise EmptyResultSet
            else:
                if not isinstance(self.rhs, (tuple, list, Tuple)):
                    raise NotSupportedError(
                        "This tuple lookup requires tuple values on older Django."
                    )
                clause = self._comparison(self.rhs, self.lookup_name)
            sql, params = compiler.compile(clause)
            return sql, tuple(params)

    class _TupleExact(_TupleLookup):
        lookup_name = "exact"

    class _TupleIn(_TupleLookup):
        lookup_name = "in"

    class _TupleIsNull(_TupleLookup):
        lookup_name = "isnull"

    class _TupleGreaterThan(_TupleLookup):
        lookup_name = "gt"

    class _TupleGreaterThanOrEqual(_TupleLookup):
        lookup_name = "gte"

    class _TupleLessThan(_TupleLookup):
        lookup_name = "lt"

    class _TupleLessThanOrEqual(_TupleLookup):
        lookup_name = "lte"

    tuple_lookups = {
        lookup.lookup_name: lookup
        for lookup in (
            _TupleExact,
            _TupleIn,
            _TupleIsNull,
            _TupleGreaterThan,
            _TupleGreaterThanOrEqual,
            _TupleLessThan,
            _TupleLessThanOrEqual,
        )
    }

if TYPE_CHECKING:
    from django.db.backends.base.base import BaseDatabaseWrapper
    from django.db.models.expressions import BaseExpression
    from django.db.models.fields import Field


_composite_col_types: tuple[type[BaseExpression], ...] = (
    (expressions.ColPairs,) if hasattr(expressions, "ColPairs") else ()
)


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


class _SourceTupleIn(tuple_lookups["in"]):
    """Accept expression tuples on Django versions that only count ColPairs."""

    def get_prep_lookup(self) -> object:
        if isinstance(self.rhs, Query):
            if len(_projected_expressions(self.rhs)) != len(self.lhs):
                raise ValueError("Tuple subquery must select one field per component.")
            self.rhs.clear_ordering(clear_default=True)
            return self.rhs
        return super().get_prep_lookup()

    def as_sql(
        self, compiler: SQLCompiler, connection: BaseDatabaseWrapper
    ) -> tuple[str, tuple[object, ...]]:
        if not isinstance(self.rhs, Query):
            return super().as_sql(compiler, connection)
        if getattr(self.rhs, "_db", None) not in {None, connection.alias}:
            raise ValueError("Tuple subqueries cannot use a different database.")
        lhs, lhs_params = compiler.compile(self.lhs)
        rhs, rhs_params = compiler.compile(self.rhs)
        return f"{lhs} IN {rhs}", (*lhs_params, *rhs_params)


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


def _table_source(value: object) -> TableSource | None:
    explicit = isinstance(value, Join)
    original = value.source if explicit else value
    if isinstance(original, expressions.Exists):
        return None
    query = original.query if isinstance(original, (QuerySet, Subquery)) else original
    if not isinstance(query, Query):
        if getattr(query, "table_source", False):
            return TableSource(
                query,
                join_type=value.join_type if explicit else None,
                condition=value.condition if explicit else None,
            )
        if explicit:
            raise TypeError("Join requires a queryset or a table-source expression.")
        return None
    source = _query_source(query, explicit)
    if source is None:
        return None
    return TableSource(
        source,
        join_type=value.join_type if explicit else None,
        condition=value.condition if explicit else None,
        source_db=getattr(original, "_db", None),
    )


def _query_source(query: Query, explicit: bool) -> Query | None:
    """Clone and describe a projected queryset without resolving outer bindings."""
    if (
        not explicit
        and not query.default_cols
        and len(_projected_expressions(query)) < 2
    ):
        return None
    if query.combinator:
        raise NotSupportedError("Combined table-source querysets are not supported.")
    if explicit and query.default_cols:
        query = query.clone()
        if query.select_related:
            raise ValueError(
                "Table sources using select_related() require an explicit "
                "values() projection."
            )
        names = tuple(
            col.target.attname
            for col in SQLCompiler(query, None, None).get_default_columns(
                query.get_select_mask()
            )
        )
        query.set_values((*names, *query.annotation_select))
    projection = _projected_expressions(query)
    if any(
        isinstance(expression, _composite_col_types)
        for expression in projection.values()
    ):
        raise ValueError("Table sources require individually named columns.")
    if query.default_cols or not projection:
        raise ValueError(
            "Table-source querysets require an explicit values() projection."
        )
    if any(not name for name in projection):
        raise ValueError("Table-source column names must be nonempty.")
    source = query.clone()
    source.__class__ = _query_class(type(source))
    if not hasattr(source, "selected"):
        source._table_output_names = tuple(projection)
    return source


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


def _resolve_join_condition(query: Query, condition: Q | BaseExpression) -> WhereNode:
    if hasattr(query, "build_filtered_relation_q"):
        # Django 4.2 has a separate ON-clause builder that avoids join promotion.
        return query.build_filtered_relation_q(Q(condition), reuse=set())
    return query.build_filter(condition, split_subq=False, update_join_types=False)[0]


@lru_cache(maxsize=None)
def _compiler_class(base: type[SQLCompiler]) -> type[SQLCompiler]:
    return type("DerivedCompiler", (DerivedCompilerMixin, base), {})


@lru_cache(maxsize=None)
def _query_class(base: type[Query]) -> type[Query]:
    if issubclass(base, DerivedQueryMixin):
        return base
    return type("DerivedQuery", (DerivedQueryMixin, base), {"_derived_base": base})


def _supports_lateral_joins(connection: BaseDatabaseWrapper) -> bool:
    if connection.vendor == "mysql":
        return not connection.mysql_is_mariadb and connection.mysql_version >= (
            8,
            0,
            14,
        )
    return connection.vendor in {"postgresql", "oracle"}


def _restore_query(base: type[Query], state: dict[str, object]) -> Query:
    query = object.__new__(_query_class(base))
    query.__dict__.update(state)
    return query


class DerivedCompilerMixin:
    def get_select(
        self, *args: object, **kwargs: object
    ) -> tuple[_SelectList, dict[str, object] | None, dict[str, int]]:
        select, klass_info, annotation_map = super().get_select(*args, **kwargs)
        names = getattr(self.query, "_table_output_names", None)
        if names is not None:
            select = [
                (expression, sql, name)
                for (expression, sql, _), name in zip(select, names, strict=True)
            ]
        return select, klass_info, annotation_map

    def pre_sql_setup(
        self, *args: object, **kwargs: object
    ) -> tuple[_SelectList, _OrderList, list[_SQLFragment]]:
        result = super().pre_sql_setup(*args, **kwargs)
        self._source_ordering = [expression for expression, _ in result[1]]
        return result

    def get_from_clause(self) -> tuple[list[str], list[object]]:
        selected = [expression for expression, *_ in self.select]
        if any(
            isinstance(expression.output_field, CompositeField)
            for expression in selected
        ):
            raise ValueError(
                "Select table-source columns individually, not the whole source."
            )
        ordering = getattr(self, "_source_ordering", [])
        distinct = getattr(self, "_source_distinct", [])
        required = {
            col.alias for col in self.query._gen_cols(selected + ordering + distinct)
        }
        required.update(getattr(self.query, "_retained_source_aliases", ()))
        expressions = selected + ordering + [self.where, self.having, self.qualify]
        if isinstance(self.query.group_by, tuple):
            expressions.extend(self.query.group_by)
        needed = {col.alias for col in self.query._gen_cols(expressions)} | required
        needed = self.query._source_dependencies(needed)
        for alias, candidate in self.query.alias_map.items():
            if not getattr(candidate, "lazy_source", False):
                continue
            self.query.alias_refcount[alias] = int(alias in needed)
            node = self.query.alias_map[alias]
            if alias in required:
                self.query.alias_map[alias] = node.demote()
        return super().get_from_clause()

    def collapse_group_by(
        self, expressions: list[BaseExpression], having: list[BaseExpression]
    ) -> list[BaseExpression]:
        if any(isinstance(expression, DerivedCol) for expression in expressions):
            return expressions
        return super().collapse_group_by(expressions, having)

    def get_distinct(self) -> tuple[list[str], list[tuple[object, ...]]]:
        result, params = [], []
        self._source_distinct = []
        fields = self.query.distinct_fields
        try:
            for name in fields:
                if self.query._source_path(name):
                    expression = self.query.resolve_ref(name)
                    self._source_distinct.append(expression)
                    sql, column_params = self.compile(expression)
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
    _source_condition_stack: tuple[tuple[str, frozenset[str]], ...] = ()

    def _source_dependencies(self, aliases: set[str]) -> set[str]:
        """Retain transitive ON/correlation dependencies of referenced sources."""
        needed = aliases.copy()
        pending = list(needed)
        while pending:
            node = self.alias_map.get(pending.pop())
            if not isinstance(node, DerivedJoin):
                continue
            dependencies = {
                column.alias for column in node.get_external_cols()
            } - needed
            needed.update(dependencies)
            pending.extend(dependencies)
        return needed

    def clone(self) -> Query:
        clone = super().clone()
        clone._source_joins = getattr(self, "_source_joins", {}).copy()
        return clone

    def change_aliases(self, change_map: dict[str, str]) -> None:
        super().change_aliases(change_map)
        self._source_joins = {
            name: change_map.get(alias, alias)
            for name, alias in getattr(self, "_source_joins", {}).items()
        }
        self._retained_source_aliases = {
            change_map.get(alias, alias)
            for alias in getattr(self, "_retained_source_aliases", ())
        }

    def _selected_source_aliases(self) -> set[str]:
        columns = self._gen_cols([*self.select, *self.annotation_select.values()])
        aliases = {
            alias
            for alias, node in self.alias_map.items()
            if getattr(node, "lazy_source", False)
        }
        return {col.alias for col in columns} & aliases

    def get_aggregation(
        self, using: str, aggregate_exprs: dict[str, BaseExpression]
    ) -> dict[str, object]:
        self._retained_source_aliases = self._selected_source_aliases()
        return super().get_aggregation(using, aggregate_exprs)

    def exists(self, limit: bool = True) -> Query:
        clone = self.clone()
        clone._retained_source_aliases = clone._selected_source_aliases()
        return super(DerivedQueryMixin, clone).exists(limit=limit)

    def add_ordering(self, *ordering: str | BaseExpression) -> None:
        resolved: list[str | BaseExpression] = []
        for item in ordering:
            if isinstance(item, str) and self._source_path(item.removeprefix("-")):
                name, source, component, rest = self._source_path(
                    item.removeprefix("-")
                )
                names = [item.removeprefix("-")]
                if component is None and not source.scalar_function and not rest:
                    names = [f"{name}__{column}" for column in source.projection]
                resolved.extend(
                    (
                        expressions.F(name).desc()
                        if item.startswith("-")
                        else expressions.F(name).asc()
                    )
                    for name in names
                )
                continue
            resolved.append(item)
        super().add_ordering(*resolved)

    def add_annotation(
        self, annotation: BaseExpression, alias: str, select: bool = True
    ) -> None:
        source = _table_source(annotation)
        self._source_joins = getattr(self, "_source_joins", {}).copy()
        self._source_joins.pop(alias, None)
        if source is not None:
            source.prior_sources = frozenset(self.annotations)
            fields = self.model._meta.get_fields()
            names = {
                name
                for field in fields
                for name in (field.name, getattr(field, "attname", field.name))
            }
            if alias == "pk" or alias in names:
                raise ValueError(
                    f"The source name {alias!r} conflicts with a model field."
                )
            if any(
                name.startswith(alias + "__") and not isinstance(value, DerivedCol)
                for name, value in self.annotations.items()
            ):
                raise ValueError("Table-source columns conflict with aliases.")
            if "__" in alias or not alias:
                raise ValueError(
                    "Table-source aliases must be nonempty and cannot contain '__'."
                )
            if select and not source.scalar_function:
                raise ValueError(
                    "Register multi-column table sources with alias(), not annotate()."
                )
            annotation = source
        super().add_annotation(annotation, alias, select=select and source is None)
        if source is not None and select:
            column = self.resolve_ref(alias)
            super().add_annotation(column, alias, select=True)

    def _source_path(
        self, name: str
    ) -> tuple[str, TableSource, str | None, list[str]] | None:
        parts = name.split("__")
        source = self.annotations.get(parts[0])
        if not isinstance(source, TableSource):
            return None
        for index in range(len(parts), 1, -1):
            component = "__".join(parts[1:index])
            if component in source.projection:
                return parts[0], source, component, parts[index:]
        return parts[0], source, None, parts[1:]

    def _source_column(
        self, name: str, source: TableSource, component: str | None
    ) -> BaseExpression:
        if component is None:
            if source.scalar_function:
                component = next(iter(source.projection))
            else:
                return Tuple(
                    *(
                        self._source_column(name, source, column)
                        for column in source.projection
                    ),
                    output_field=source.output_field,
                )
        alias = self._materialize_source(name, source)
        field = copy(source.projection[component].output_field)
        field.null = True
        column = (
            component
            if isinstance(source.source, Query)
            else field.db_column or component or alias
        )
        return DerivedCol(alias, column, field)

    def _materialize_source(self, name: str, source: TableSource) -> str:
        self._check_source_dependency(name)
        aliases = getattr(self, "_source_joins", {}).copy()
        if name not in aliases:
            parent = self.get_initial_alias()
            alias, _ = self.table_alias(name, create=True)
            is_query = isinstance(source.source, Query)
            join_class = DerivedJoin if is_query else FunctionJoin
            node = join_class(
                name,
                alias,
                parent,
                source.source,
                None,
                source.join_type or LOUTER,
                bool(source.source.get_external_cols()) if is_query else False,
                source.source_db,
                inferred=source.join_type is None,
            )
            node.lazy_source = True
            self.alias_map[alias] = node
            aliases[name] = alias
            self._source_joins = aliases
            if source.condition is not None:
                node.condition = self._resolve_source_condition(
                    name, source, source.condition
                )
                self.alias_map[alias] = self.alias_map.pop(alias)
        alias = aliases[name]
        self.ref_alias(alias)
        return alias

    def _check_source_dependency(self, name: str) -> None:
        if not self._source_condition_stack:
            return
        owner, allowed = self._source_condition_stack[-1]
        if name == owner:
            return
        if any(name == ancestor for ancestor, _ in self._source_condition_stack):
            raise ValueError("Circular table-source dependencies are unsupported.")
        if name not in allowed:
            raise ValueError("Forward table-source dependencies are unsupported.")

    def _resolve_source_condition(
        self, name: str, source: TableSource, condition: Q | BaseExpression
    ) -> WhereNode:
        previous = self._source_condition_stack
        self._source_condition_stack = (*previous, (name, source.prior_sources))
        try:
            return _resolve_join_condition(self, condition)
        finally:
            self._source_condition_stack = previous

    def solve_lookup_type(
        self, lookup: str, summarize: bool = False
    ) -> tuple[list[str], list[str], BaseExpression | bool]:
        if path := self._source_path(lookup):
            name, source, component, lookups = path
            return lookups, [], self._source_column(name, source, component)
        return super().solve_lookup_type(lookup, summarize)

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
        # Compilation resolves lazy aliases and selection-dependent join types.
        # Keep those decisions off the reusable Query and its subsequent clones.
        query = self.clone()
        compiler = super(DerivedQueryMixin, query).get_compiler(
            using, connection, elide_empty
        )
        compiler.__class__ = _compiler_class(type(compiler))
        return compiler

    def set_values(self, fields: tuple[str, ...]) -> None:
        for name in fields:
            if self._source_path(name):
                super().add_annotation(self.resolve_ref(name), name, select=True)
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
        root = name.split("__", 1)[0]
        if root in self.annotations:
            self._check_source_dependency(root)
        if path := self._source_path(name):
            if not allow_joins:
                raise FieldError(
                    "Joined field references are not permitted in this query"
                )
            root, source, component, transforms = path
            expression = self._source_column(root, source, component)
            for transform in transforms:
                expression = self.try_transform(expression, transform)
            return expression
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
        if isinstance(filter_expr, tuple) and isinstance(filter_expr[1], QuerySet):
            path = self._source_path(filter_expr[0])
            if path and path[2] is None:
                value = filter_expr[1]
                resolved = value.query.resolve_expression(self)
                resolved._db = value._db
                filter_expr = (filter_expr[0], resolved)
        clause, joins = super().build_filter(filter_expr, *args, **kwargs)
        columns = list(self._gen_cols([clause]))
        source_aliases = {
            alias
            for alias, node in self.alias_map.items()
            if getattr(node, "lazy_source", False)
        }
        used_sources = {col.alias for col in columns} & source_aliases
        if used_sources and not isinstance(filter_expr, Q):
            condition = clause.children[0]
            negated = kwargs.get("current_negated", False)
            require_outer = (
                getattr(condition, "lookup_name", None) == "isnull"
                and condition.rhs is True
                and not negated
            ) or bool(negated)
            if not require_outer:
                joins = set(joins) | used_sources
        if isinstance(filter_expr, tuple) and kwargs.get("current_negated"):
            self._negate_source_filter(filter_expr[0], clause)
        return clause, joins

    def _negate_source_filter(self, name: str, clause: WhereNode) -> None:
        """Keep nullable source outputs when a queryset predicate is negated."""
        if path := self._source_path(name):
            expression = self._source_column(*path[:3])
        else:
            root, _ = refs_expression(name.split("__"), self.annotations)
            expression = self.annotations.get(root)
        condition = clause.children[0]
        if isinstance(expression, DerivedCol):
            for null_condition, connector in expression.negated_null_conditions(
                condition, condition.rhs, self
            ):
                clause.add(null_condition, connector)
        elif isinstance(expression, Tuple):
            if condition.lookup_name != "isnull" and condition.rhs is not None:
                clause.add(expression.get_lookup("isnull")(expression, False), "AND")

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
        for source in self.alias_map.values():
            if isinstance(source, DerivedJoin):
                columns.extend(
                    col
                    for col in source.get_external_cols()
                    if col.alias in self.external_aliases
                )
        return columns

    def combine(self, rhs: Query, connector: str) -> None:
        if any(
            getattr(node, "lazy_source", False)
            for query in (self, rhs)
            for node in query.alias_map.values()
        ) or any(
            isinstance(value, TableSource)
            for query in (self, rhs)
            for value in query.annotations.values()
        ):
            raise NotSupportedError(
                "Boolean queryset combinations with table sources are not "
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
