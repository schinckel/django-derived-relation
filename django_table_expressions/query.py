"""Register, resolve and materialize named table sources."""

from __future__ import annotations

from collections.abc import Callable
from copy import copy
from functools import lru_cache
from typing import TYPE_CHECKING

from django.core.exceptions import FieldError
from django.db import NotSupportedError
from django.db.models import QuerySet, expressions
from django.db.models.expressions import Subquery
from django.db.models.query_utils import Q, refs_expression
from django.db.models.sql.compiler import SQLCompiler
from django.db.models.sql.constants import LOUTER
from django.db.models.sql.query import Query

from .compat import _composite_col_types, _projected_expressions
from .compiler import _compiler_class
from .expressions import DerivedCol, TableSource
from .joins import DerivedJoin, FunctionJoin, Join
from .lookups import Tuple

if TYPE_CHECKING:
    from django.db.backends.base.base import BaseDatabaseWrapper
    from django.db.models.expressions import BaseExpression, Col
    from django.db.models.sql.where import WhereNode


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


def _resolve_join_condition(query: Query, condition: Q | BaseExpression) -> WhereNode:
    if hasattr(query, "build_filtered_relation_q"):
        # Django 4.2 has a separate ON-clause builder that avoids join promotion.
        return query.build_filtered_relation_q(Q(condition), reuse=set())
    return query.build_filter(condition, split_subq=False, update_join_types=False)[0]


@lru_cache(maxsize=None)
def _query_class(base: type[Query]) -> type[Query]:
    if issubclass(base, DerivedQueryMixin):
        return base
    return type("DerivedQuery", (DerivedQueryMixin, base), {"_derived_base": base})


def _restore_query(base: type[Query], state: dict[str, object]) -> Query:
    query = object.__new__(_query_class(base))
    query.__dict__.update(state)
    return query


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
