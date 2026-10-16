from django.db import models
from django.test import SimpleTestCase

from django_derived_relation import CompositeField


class CompositeOutputTests(SimpleTestCase):
    def test_named_output_fields_are_copied(self) -> None:
        original = models.TextField(db_column="key")
        output = CompositeField(label=original, payload=models.JSONField())
        self.assertEqual([field.name for field in output.fields], ["label", "payload"])
        self.assertIsNot(output.fields[0], original)
        self.assertIsNone(original.name)

    def test_clone_preserves_names_types_and_physical_columns(self) -> None:
        output = CompositeField(
            label=models.TextField(db_column="key"), payload=models.JSONField()
        )
        clone = output.clone()
        self.assertEqual([field.name for field in clone.fields], ["label", "payload"])
        self.assertEqual(clone.fields[0].db_column, "key")
        self.assertIsNot(clone.fields[0], output.fields[0])

    def test_not_a_concrete_model_field(self) -> None:
        field = CompositeField(a=models.IntegerField(), b=models.IntegerField())
        with self.assertRaises(TypeError):
            field.contribute_to_class(models.Model, "invalid")

    def test_invalid_output_shape(self) -> None:
        with self.assertRaises(ValueError):
            CompositeField(a=models.IntegerField())
        with self.assertRaises(ValueError):
            CompositeField(models.IntegerField(name="x"), models.TextField(name="x"))
