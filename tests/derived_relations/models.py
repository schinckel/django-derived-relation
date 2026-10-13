from django.db import models

from django_derived_relation import DerivedQuerySet


class Parent(models.Model):
    objects = DerivedQuerySet.as_manager()
    name = models.CharField(max_length=20)
    minimum = models.IntegerField(default=0)


class Child(models.Model):
    objects = DerivedQuerySet.as_manager()
    parent = models.ForeignKey(Parent, models.CASCADE, related_name="children")
    amount = models.IntegerField()
    sequence = models.IntegerField()
