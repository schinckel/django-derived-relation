from django.contrib.gis.db import models

from django_derived_relation import DerivedQuerySet


class Place(models.Model):
    objects = DerivedQuerySet.as_manager()
    name = models.CharField(max_length=20)
    point = models.PointField(srid=3857)


class Region(models.Model):
    objects = DerivedQuerySet.as_manager()
    name = models.CharField(max_length=20)
    shape = models.PolygonField(srid=3857)
