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


class InheritedChild(Child):
    label = models.CharField(max_length=20)


class CompositeChild(models.Model):
    objects = DerivedQuerySet.as_manager()
    if hasattr(models, "CompositePrimaryKey"):
        pk = models.CompositePrimaryKey("parent_id", "sequence")
    parent = models.ForeignKey(Parent, models.CASCADE)
    amount = models.IntegerField()
    sequence = models.IntegerField()
