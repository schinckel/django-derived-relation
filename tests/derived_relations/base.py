from django.db.models import OuterRef, QuerySet
from django.test import TestCase

from .models import Child, Parent


class DerivedRelationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.empty = Parent.objects.create(name="empty")
        cls.one = Parent.objects.create(name="one")
        cls.many = Parent.objects.create(name="many")
        Child.objects.create(parent=cls.one, amount=10, sequence=1)
        Child.objects.create(parent=cls.many, amount=20, sequence=1)
        Child.objects.create(parent=cls.many, amount=30, sequence=2)

    def source(self) -> QuerySet[Child]:
        return Child.objects.filter(parent_id=OuterRef("pk")).values(
            "amount", "sequence"
        )
