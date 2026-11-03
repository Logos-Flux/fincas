from django.test import TestCase

from core.models import Company, DocumentSequence


class DocumentSequenceTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Co")

    def test_get_next_returns_1_for_new_company(self):
        num = DocumentSequence.get_next(self.company)
        self.assertEqual(num, 1)

    def test_get_next_increments(self):
        n1 = DocumentSequence.get_next(self.company)
        n2 = DocumentSequence.get_next(self.company)
        n3 = DocumentSequence.get_next(self.company)
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 2)
        self.assertEqual(n3, 3)

    def test_separate_companies_have_separate_sequences(self):
        company2 = Company.objects.create(name="Other Co")
        n1 = DocumentSequence.get_next(self.company)
        n2 = DocumentSequence.get_next(company2)
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 1)

    def test_second_tenant_first_order_skips_taken_number(self):
        """Per-company sequences restart at 1 while order numbers are
        globally unique — without skip-past-taken, the second tenant's
        first order 500s on SO-00001 (caught by the J2 staging journey)."""
        from orders.models import Order

        first = Order.objects.create(company=self.company)
        self.assertEqual(first.order_number, "SO-00001")
        company2 = Company.objects.create(name="Other Co")
        second = Order.objects.create(company=company2)
        self.assertEqual(second.order_number, "SO-00002")
