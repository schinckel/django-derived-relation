from django.contrib.gis.db.models import PointField
from django.contrib.gis.db.models.functions import Area, Distance, Transform
from django.contrib.gis.geos import Point, Polygon
from django.contrib.gis.measure import A, D
from django.db.models import ExpressionWrapper, F, OuterRef, Q
from django.test import TestCase

from django_derived_relation import DerivedRelation

from .models import Place, Region


class SpatialDerivedRelationTests(TestCase):
    west: Place
    low: Region
    high: Region

    @classmethod
    def setUpTestData(cls) -> None:
        cls.west = Place.objects.create(name="west", point=Point(0, 0, srid=3857))
        Place.objects.create(name="mid", point=Point(10, 0, srid=3857))
        Place.objects.create(name="east", point=Point(20, 0, srid=3857))
        Place.objects.create(name="far", point=Point(100, 100, srid=3857))
        cls.low = Region.objects.create(
            name="low", shape=Polygon.from_bbox((-1, -1, 11, 1))
        )
        cls.high = Region.objects.create(
            name="high", shape=Polygon.from_bbox((9, -1, 21, 1))
        )

    def test_ordinary_spatial_on_condition_and_geometry_conversion(self) -> None:
        joined = Place.objects.alias(
            region=DerivedRelation(
                Region.objects.values("name", "shape"),
                condition=Q(region__shape__covers=F("point")),
            )
        )
        rows = list(
            joined.order_by("name", "region__name").values_list(
                "name", "region__name", "region__shape"
            )
        )
        self.assertEqual(
            [(name, region) for name, region, shape in rows],
            [
                ("east", "high"),
                ("far", None),
                ("mid", "high"),
                ("mid", "low"),
                ("west", "low"),
            ],
        )
        for name, region, shape in rows:
            if region is None:
                self.assertIsNone(shape)
            else:
                expected = self.low.shape if region == "low" else self.high.shape
                self.assertIsInstance(shape, Polygon)
                self.assertEqual(shape.srid, 3857)
                self.assertTrue(shape.equals_exact(expected))

    def test_correlated_spatial_filter_and_slice(self) -> None:
        source = (
            Region.objects.filter(shape__covers=OuterRef("point"))
            .order_by("name")
            .values("name", "shape")[:1]
        )
        joined = Place.objects.alias(region=DerivedRelation(source, lateral=True))
        self.assertEqual(
            list(joined.order_by("name").values_list("name", "region__name")),
            [("east", "high"), ("far", None), ("mid", "high"), ("west", "low")],
        )
        self.assertEqual(joined.filter(region__shape__covers=F("point")).count(), 3)

    def test_nearest_geometry_and_distance_measure(self) -> None:
        origin = ExpressionWrapper(
            OuterRef("point"), output_field=PointField(srid=3857)
        )
        source = (
            Place.objects.filter(point__dwithin=(OuterRef("point"), 15))
            .exclude(pk=OuterRef("pk"))
            .annotate(distance=Distance("point", origin))
            .order_by("distance", "pk")
            .values("name", "point", "distance")[:1]
        )
        joined = Place.objects.alias(nearest=DerivedRelation(source, lateral=True))
        rows = list(
            joined.order_by("name").values_list(
                "name", "nearest__name", "nearest__point", "nearest__distance"
            )
        )
        self.assertEqual(
            [(name, nearest) for name, nearest, point, distance in rows],
            [("east", "mid"), ("far", None), ("mid", "west"), ("west", "mid")],
        )
        for name, nearest, point, distance in rows:
            if nearest is None:
                self.assertIsNone(point)
                self.assertIsNone(distance)
            else:
                self.assertIsInstance(point, Point)
                self.assertEqual(point.srid, 3857)
                self.assertIsInstance(distance, D)
                self.assertAlmostEqual(distance.m, 10)
        self.assertEqual(joined.filter(nearest__distance__lte=D(m=11)).count(), 3)
        rejected = Place.objects.alias(
            nearest=DerivedRelation(
                source, lateral=True, condition=Q(nearest__distance__lt=D(m=5))
            )
        )
        self.assertEqual(rejected.count(), 4)
        self.assertEqual(rejected.filter(nearest__name__isnull=False).count(), 0)

    def test_transformed_geometry_projection(self) -> None:
        source = Place.objects.annotate(projected=Transform("point", 4326)).values(
            "id", "projected"
        )
        joined = Place.objects.alias(
            rows=DerivedRelation(source, condition=Q(rows__id=F("pk")))
        )
        for place in joined.annotate(projected=F("rows__projected")):
            expected = place.point.clone()
            expected.transform(4326)
            self.assertEqual(place.projected.srid, 4326)
            self.assertTrue(place.projected.equals_exact(expected, tolerance=1e-9))

    def test_area_measure_projection_and_lookup(self) -> None:
        source = (
            Region.objects.filter(shape__covers=OuterRef("point"))
            .annotate(surface=Area("shape"))
            .order_by("name")
            .values("surface")[:1]
        )
        joined = Place.objects.alias(region=DerivedRelation(source, lateral=True))
        rows = list(joined.order_by("name").values_list("name", "region__surface"))
        self.assertEqual(
            [name for name, surface in rows], ["east", "far", "mid", "west"]
        )
        for name, surface in rows:
            if name == "far":
                self.assertIsNone(surface)
            else:
                self.assertIsInstance(surface, A)
                self.assertAlmostEqual(surface.sq_m, 24)
        self.assertEqual(joined.filter(region__surface__gt=A(sq_m=23)).count(), 3)
