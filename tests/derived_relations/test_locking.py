"""Exercise source-row locks using independent PostgreSQL connections."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal
from unittest import skipUnless
from unittest.mock import patch

import psycopg
from django.core.exceptions import FieldError
from django.db import NotSupportedError, OperationalError, connection, transaction
from django.db.models import Count, F, Model, OuterRef, Q, QuerySet
from django.db.transaction import TransactionManagementError
from django.test import (
    SimpleTestCase,
    TransactionTestCase,
    override_settings,
    skipUnlessDBFeature,
)
from psycopg import sql

from django_derived_relation import Join

from .models import Child, Parent


class LockRoutingRouter:
    def db_for_read(self, model: type[Model], **hints: object) -> str:
        return "replica"

    def db_for_write(self, model: type[Model], **hints: object) -> str:
        return "primary"


@override_settings(DATABASE_ROUTERS=[LockRoutingRouter()])
class DerivedRelationLockRoutingTests(SimpleTestCase):
    def test_locking_source_uses_write_router_without_locking_outer_query(self) -> None:
        source = Child.objects.select_for_update().values("id")
        original = Parent.objects.all()
        for method in ("alias",):
            with self.subTest(method=method):
                joined = getattr(original, method)(
                    locked=Join(source, join_type="left")
                )
                self.assertEqual(joined.db, "primary")
                self.assertFalse(joined.query.select_for_update)
                self.assertEqual(original.db, "replica")

    def test_unlocked_source_preserves_read_routing(self) -> None:
        joined = Parent.objects.alias(
            locked=Join(Child.objects.values("id"), join_type="left")
        )
        self.assertEqual(joined.db, "replica")

    def test_nested_locking_source_uses_write_router(self) -> None:
        source = Child.objects.alias(
            nested=Join(
                Child.objects.select_for_update().values("id"), join_type="left"
            )
        ).values("id")
        joined = Parent.objects.alias(locked=Join(source, join_type="left"))
        self.assertEqual(joined.db, "primary")
        self.assertFalse(joined.query.select_for_update)

    def test_explicit_outer_database_keeps_native_precedence(self) -> None:
        source = Child.objects.select_for_update().values("id")
        joined = Parent.objects.using("chosen").alias(
            locked=Join(source, join_type="left")
        )
        self.assertEqual(joined.db, "chosen")
        self.assertEqual(joined.using("another").db, "another")
        self.assertFalse(joined.query.select_for_update)

    def test_explicit_source_database_does_not_choose_outer_database(self) -> None:
        source = Child.objects.using("chosen").select_for_update().values("id")
        joined = Parent.objects.alias(locked=Join(source, join_type="left"))
        self.assertEqual(joined.db, "primary")
        self.assertEqual(joined.using("chosen").db, "chosen")

    def test_outer_locking_flag_and_write_routing_are_preserved(self) -> None:
        joined = Parent.objects.select_for_update().alias(
            locked=Join(Child.objects.values("id"), join_type="left")
        )
        self.assertEqual(joined.db, "primary")
        self.assertTrue(joined.query.select_for_update)


@skipUnless(connection.vendor == "postgresql", "PostgreSQL row locks are required")
class DerivedRelationLockingTests(TransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.parent: Parent = Parent.objects.create(name="one")
        self.first: Child = Child.objects.create(
            parent=self.parent, amount=10, sequence=1
        )
        self.second: Child = Child.objects.create(
            parent=self.parent, amount=20, sequence=2
        )
        with connection.cursor() as cursor:
            cursor.execute("SHOW lock_timeout")
            self.lock_timeout: str = cursor.fetchone()[0]
            cursor.execute("SET lock_timeout = '2s'")
        self.addCleanup(self._restore_lock_timeout)

    def _restore_lock_timeout(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('lock_timeout', %s, false)", [self.lock_timeout]
            )

    @contextmanager
    def _peer(self) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
        # The runner replaces NAME with its test database before tests run.
        # Never reconstruct a connection from environment or production settings.
        with psycopg.connect(
            **connection.get_connection_params(), autocommit=True
        ) as peer:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database(), pg_backend_pid()")
                database, backend_pid = cursor.fetchone()
            peer_database, peer_pid = peer.execute(
                "SELECT current_database(), pg_backend_pid()"
            ).fetchone()
            self.assertEqual(peer_database, database)
            self.assertNotEqual(peer_pid, backend_pid)
            peer.execute("SET lock_timeout = '2s'")
            yield peer

    def _lock(
        self,
        peer: psycopg.Connection[tuple[object, ...]],
        instance: Parent | Child,
        mode: Literal["UPDATE", "KEY SHARE"] = "UPDATE",
    ) -> None:
        statement = sql.SQL(
            "SELECT {pk} FROM {table} WHERE {pk} = %s FOR " + mode + " NOWAIT"
        ).format(
            pk=sql.Identifier(instance._meta.pk.column),
            table=sql.Identifier(instance._meta.db_table),
        )
        self.assertEqual(
            peer.execute(statement, [instance.pk]).fetchone(), (instance.pk,)
        )

    def _assert_locked(
        self, peer: psycopg.Connection[tuple[object, ...]], instance: Parent | Child
    ) -> None:
        with self.assertRaises(psycopg.errors.LockNotAvailable):
            with peer.transaction():
                self._lock(peer, instance)

    def _assert_unlocked(
        self, peer: psycopg.Connection[tuple[object, ...]], instance: Parent | Child
    ) -> None:
        with peer.transaction():
            self._lock(peer, instance)

    def _source(self, lateral: bool) -> QuerySet[Child]:
        source = Child.objects.order_by("sequence")
        if lateral:
            source = source.filter(parent_id=OuterRef("pk"))
        else:
            source = source.filter(parent=self.parent)
        return source

    def _rows(self, source: QuerySet[Child], lateral: bool = False) -> QuerySet[Parent]:
        condition = None if lateral else Q(pk=F("locked__parent_id"))
        return (
            Parent.objects.filter(pk=self.parent.pk)
            .alias(locked=Join(source, condition=condition, join_type="left"))
            .order_by("locked__id")
            .values_list("locked__id", flat=True)
        )

    def _native_rows(self, source: QuerySet[Child]) -> list[int | None]:
        source_sql, params = source.query.get_compiler(
            connection=connection, elide_empty=False
        ).as_sql()
        quote = connection.ops.quote_name
        parent_table = quote(Parent._meta.db_table)
        parent_pk = quote(Parent._meta.pk.column)
        child_parent = quote(Child._meta.get_field("parent").column)
        child_pk = quote(Child._meta.pk.column)
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT locked.{child_pk} FROM {parent_table} outer_parent "
                f"LEFT JOIN ({source_sql}) locked "
                f"ON outer_parent.{parent_pk} = locked.{child_parent} "
                f"WHERE outer_parent.{parent_pk} = %s ORDER BY locked.{child_pk}",
                (*params, self.parent.pk),
            )
            return [row[0] for row in cursor.fetchall()]

    def test_source_locks_child_rows_without_locking_outer_parent(self) -> None:
        for lateral in (False, True):
            with self.subTest(lateral=lateral), self._peer() as peer:
                with transaction.atomic():
                    rows = self._rows(
                        self._source(lateral).select_for_update(), lateral
                    )
                    self.assertEqual(list(rows), [self.first.pk, self.second.pk])
                    self._assert_locked(peer, self.first)
                    self._assert_locked(peer, self.second)
                    self._assert_unlocked(peer, self.parent)
                self._assert_unlocked(peer, self.first)

    @skipUnlessDBFeature("has_select_for_update_of")
    def test_deferred_source_preserves_projection_and_locks(self) -> None:
        for source in (
            self._source(True).only("amount"),
            self._source(True).defer("parent", "sequence"),
        ):
            with (
                self.subTest(deferred=source.query.deferred_loading),
                self._peer() as peer,
            ):
                joined = Parent.objects.filter(pk=self.parent.pk).alias(
                    locked=Join(
                        source.select_for_update(of=("self",)), join_type="left"
                    )
                )
                with transaction.atomic():
                    self.assertEqual(
                        list(
                            joined.order_by("locked__amount").values_list(
                                "locked__amount", flat=True
                            )
                        ),
                        [10, 20],
                    )
                    self._assert_locked(peer, self.first)
                    self._assert_locked(peer, self.second)
                    self._assert_unlocked(peer, self.parent)
                    with self.assertRaises(FieldError):
                        joined.values("locked__parent_id")

    def test_evaluation_outside_atomic_raises_native_transaction_error(self) -> None:
        self.assertTrue(connection.get_autocommit())
        for lateral in (False, True):
            with self.subTest(lateral=lateral):
                rows = self._rows(self._source(lateral).select_for_update(), lateral)
                with self.assertRaises(TransactionManagementError):
                    list(rows)

    @skipUnlessDBFeature("has_select_for_update_nowait")
    def test_nowait_reports_independently_held_source_lock(self) -> None:
        for lateral in (False, True):
            with self.subTest(lateral=lateral), self._peer() as peer:
                with peer.transaction():
                    self._lock(peer, self.first)
                    source = self._source(lateral).select_for_update(nowait=True)
                    with self.assertRaises(OperationalError) as error:
                        with transaction.atomic():
                            list(self._rows(source, lateral))
                    self.assertIsInstance(
                        error.exception.__cause__, psycopg.errors.LockNotAvailable
                    )
                with transaction.atomic():
                    self.assertEqual(
                        list(self._rows(source, lateral)),
                        [self.first.pk, self.second.pk],
                    )

    @skipUnlessDBFeature("has_select_for_update_skip_locked")
    def test_skip_locked_omits_independently_held_source_row(self) -> None:
        for lateral in (False, True):
            with self.subTest(lateral=lateral), self._peer() as peer:
                with peer.transaction():
                    self._lock(peer, self.first)
                    with transaction.atomic():
                        source = self._source(lateral).select_for_update(
                            skip_locked=True
                        )
                        self.assertEqual(
                            list(self._rows(source, lateral)), [self.second.pk]
                        )
                        self._assert_locked(peer, self.second)
                    self._assert_unlocked(peer, self.second)

    @skipUnlessDBFeature("has_select_for_update_skip_locked")
    def test_left_join_retains_outer_row_when_no_source_row_can_lock(self) -> None:
        for lateral in (False, True):
            with self.subTest(lateral=lateral), self._peer() as peer:
                with peer.transaction():
                    self._lock(peer, self.first)
                    self._lock(peer, self.second)
                    with transaction.atomic():
                        source = self._source(lateral).select_for_update(
                            skip_locked=True
                        )
                        self.assertEqual(list(self._rows(source, lateral)), [None])
                        self._assert_unlocked(peer, self.parent)

    def test_source_locks_release_on_rollback(self) -> None:
        for lateral in (False, True):
            with self.subTest(lateral=lateral), self._peer() as peer:
                with transaction.atomic():
                    self.assertEqual(
                        list(
                            self._rows(
                                self._source(lateral).select_for_update(), lateral
                            )
                        ),
                        [self.first.pk, self.second.pk],
                    )
                    self._assert_locked(peer, self.first)
                    transaction.set_rollback(True)
                self._assert_unlocked(peer, self.first)

    @skipUnlessDBFeature("has_select_for_update_of")
    def test_of_self_locks_only_source_model_with_selected_field(self) -> None:
        for lateral in (False, True):
            with self.subTest(lateral=lateral), self._peer() as peer:
                source = (
                    self._source(lateral)
                    .values("id", "parent_id", parent_name=F("parent__name"))
                    .select_for_update(of=("self",))
                )
                with transaction.atomic():
                    self.assertEqual(
                        list(self._rows(source, lateral)),
                        [self.first.pk, self.second.pk],
                    )
                    self._assert_locked(peer, self.first)
                    self._assert_unlocked(peer, self.parent)

    @skipUnlessDBFeature("has_select_for_update_of")
    def test_invalid_of_field_retains_native_field_error(self) -> None:
        source = (
            self._source(False)
            .values("id", "parent_id")
            .select_for_update(of=("unknown",))
        )
        with transaction.atomic(), self.assertRaises(FieldError):
            list(self._rows(source))

    @skipUnlessDBFeature("has_select_for_no_key_update")
    def test_no_key_source_lock_allows_key_share_and_blocks_update(self) -> None:
        for lateral in (False, True):
            with self.subTest(lateral=lateral), self._peer() as peer:
                with transaction.atomic():
                    source = self._source(lateral).select_for_update(no_key=True)
                    self.assertEqual(
                        list(self._rows(source, lateral)),
                        [self.first.pk, self.second.pk],
                    )
                    with peer.transaction():
                        self._lock(peer, self.first, "KEY SHARE")
                    self._assert_locked(peer, self.first)

    @skipUnlessDBFeature("supports_select_for_update_with_limit")
    def test_sliced_source_locks_only_visited_row(self) -> None:
        for lateral in (False, True):
            with self.subTest(lateral=lateral), self._peer() as peer:
                with transaction.atomic():
                    source = self._source(lateral).select_for_update()[:1]
                    self.assertEqual(list(self._rows(source, lateral)), [self.first.pk])
                    self._assert_locked(peer, self.first)
                    self._assert_unlocked(peer, self.second)

    def test_known_empty_source_keeps_left_row_and_acquires_no_locks(self) -> None:
        for lateral in (False, True):
            for empty in ("none", "in", "slice"):
                with self.subTest(lateral=lateral, empty=empty), self._peer() as peer:
                    source = self._source(lateral).select_for_update()
                    if empty == "none":
                        source = source.none()
                    elif empty == "in":
                        source = source.filter(pk__in=[])
                    else:
                        source = source[:0]
                    with transaction.atomic():
                        self.assertEqual(list(self._rows(source, lateral)), [None])
                        self._assert_unlocked(peer, self.first)
                        self._assert_unlocked(peer, self.second)

    def test_known_empty_source_retains_transaction_check(self) -> None:
        source = self._source(False).none().select_for_update()
        with self.assertRaises(TransactionManagementError):
            list(self._rows(source))

    def test_sliced_and_empty_sources_match_native_subquery_locks(self) -> None:
        source = self._source(False).select_for_update()
        cases = (
            (source[:1], [self.first.pk]),
            (source[:0], [None]),
            (source.none(), [None]),
            (source.filter(pk__in=[]), [None]),
        )
        for candidate, expected in cases:
            for evaluate in (self._native_rows, self._rows):
                with self.subTest(expected=expected, evaluate=evaluate.__name__):
                    with self._peer() as peer, transaction.atomic():
                        self.assertEqual(list(evaluate(candidate)), expected)
                        if expected == [self.first.pk]:
                            self._assert_locked(peer, self.first)
                        else:
                            self._assert_unlocked(peer, self.first)
                        self._assert_unlocked(peer, self.second)

    def test_grouped_source_retains_postgresql_locking_error(self) -> None:
        source = (
            self._source(False)
            .order_by()
            .values("id", "parent_id")
            .annotate(total=Count("pk"))
            .select_for_update()
        )
        with self.assertRaises(NotSupportedError) as error:
            with transaction.atomic():
                list(self._rows(source))
        self.assertIsInstance(
            error.exception.__cause__, psycopg.errors.FeatureNotSupported
        )

    def test_unsupported_lock_options_retain_native_compiler_errors(self) -> None:
        options = (
            ("has_select_for_update_nowait", {"nowait": True}),
            ("has_select_for_update_skip_locked", {"skip_locked": True}),
            ("has_select_for_update_of", {"of": ("self",)}),
            ("has_select_for_no_key_update", {"no_key": True}),
        )
        for feature, option in options:
            with self.subTest(feature=feature):
                source = self._source(False).select_for_update(**option)
                with patch.object(connection.features, feature, False):
                    with transaction.atomic(), self.assertRaises(NotSupportedError):
                        list(self._rows(source))
