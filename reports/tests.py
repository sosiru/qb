from django.test import SimpleTestCase

from .services import _StatementPdf


class StatementPdfTests(SimpleTestCase):
    def test_statement_embeds_quickbills_logo_and_brand_colors(self):
        statement = _StatementPdf()
        statement.render(
            title="Example Customer Transaction Statement",
            customer_name="Example Customer",
            mobile_number="254700000000",
            email="customer@example.com",
            period="01-Aug-26 - 31-Aug-26",
            requested_at="31-Aug-2026",
            summary={
                "opening_balance_minor": 200000,
                "total_credits_minor": 100000,
                "total_debits_minor": 50000,
            },
            transactions=[],
        )

        pdf = statement.to_bytes()

        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        self.assertIn(b"/Subtype /Image", pdf)
        self.assertIn(b"/Logo", pdf)
        self.assertIn(b"QUICKBILLS TRANSACTION STATEMENT", pdf)
        self.assertIn(statement.navy.encode("ascii"), pdf)
        self.assertIn(statement.blue.encode("ascii"), pdf)
        self.assertIn(statement.teal.encode("ascii"), pdf)
