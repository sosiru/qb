from datetime import datetime
from io import BytesIO

from django.utils import timezone

from reports.models import ReportExport

from base.services import build_transaction_summary, export_transactions_csv_rows


def record_transaction_export(user, organization=None, file_name="transactions.csv", filters=None, file_format=None):
    export = ReportExport.objects.create(
        requested_by=user,
        organization=organization,
        export_type=ReportExport.ExportType.TRANSACTION_HISTORY,
        file_format=file_format or ReportExport.FileFormat.CSV,
        filters=filters or {},
    )
    export.mark_generated(file_name)
    return export


def generate_transaction_statement_pdf(user, organization=None, filters=None):
    filters = {key: value for key, value in (filters or {}).items() if value}
    organization_id = str(organization.id) if organization else filters.get("organization_id")
    summary = build_transaction_summary(user, organization_id, filters)
    statement = _StatementPdf()
    file_name = f"quickbundl-statement-{summary['date_from']}-to-{summary['date_to']}.pdf"
    title = f"{organization.name if organization else user.full_name} Transaction Statement"
    statement.render(
        title=title,
        customer_name=organization.name if organization else user.full_name,
        mobile_number=user.phone_number,
        email=user.email or "-",
        period=f"{_pretty_date(summary['date_from'])} - {_pretty_date(summary['date_to'])}",
        requested_at=timezone.localtime(timezone.now()).strftime("%d-%b-%Y"),
        summary=summary,
        transactions=summary.get("transactions", []),
    )
    return statement.to_bytes(), file_name


def _money(minor):
    return f"Ksh {int(minor or 0) / 100:,.2f}"


def _pretty_date(value):
    try:
        return datetime.fromisoformat(str(value)).strftime("%d-%b-%y")
    except ValueError:
        return str(value)


def _pdf_escape(value):
    return str(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class _StatementPdf:
    width = 595
    height = 842
    margin = 42
    red = "1 0 0"
    black = "0 0 0"

    def __init__(self):
        self.pages = []
        self.ops = []

    def render(self, title, customer_name, mobile_number, email, period, requested_at, summary, transactions):
        rows = self._rows(summary, transactions)
        chunks = [rows[:25]] + [rows[index:index + 33] for index in range(25, len(rows), 33)]
        if not chunks:
            chunks = [[]]
        for page_number, chunk in enumerate(chunks, start=1):
            self._new_page()
            self._header(title, customer_name, mobile_number, email, period, requested_at, page_number)
            start_y = 605 if page_number == 1 else 705
            if page_number == 1:
                self._summary(summary)
            self._transaction_table(chunk, start_y)
            self._footer(page_number, len(chunks))
            self._finish_page()

    def to_bytes(self):
        buffer = BytesIO()
        objects = []

        def add_object(payload):
            objects.append(payload)
            return len(objects)

        font_id = add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
        bold_font_id = add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
        page_ids = []
        for content in self.pages:
            content_bytes = content.encode("latin-1", errors="replace")
            content_id = add_object(
                f"<< /Length {len(content_bytes)} >>\nstream\n".encode("latin-1") + content_bytes + b"\nendstream"
            )
            page_id = add_object(
                f"<< /Type /Page /Parent 0 0 R /MediaBox [0 0 {self.width} {self.height}] "
                f"/Resources << /Font << /F1 {font_id} 0 R /F2 {bold_font_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>".encode("latin-1")
            )
            page_ids.append(page_id)
        pages_id = add_object(
            f"<< /Type /Pages /Count {len(page_ids)} /Kids [{' '.join(f'{pid} 0 R' for pid in page_ids)}] >>".encode("latin-1")
        )
        for page_id in page_ids:
            objects[page_id - 1] = objects[page_id - 1].replace(b"/Parent 0 0 R", f"/Parent {pages_id} 0 R".encode("latin-1"))
        catalog_id = add_object(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("latin-1"))

        buffer.write(b"%PDF-1.4\n")
        offsets = [0]
        for index, payload in enumerate(objects, start=1):
            offsets.append(buffer.tell())
            buffer.write(f"{index} 0 obj\n".encode("latin-1"))
            buffer.write(payload)
            buffer.write(b"\nendobj\n")
        xref = buffer.tell()
        buffer.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("latin-1"))
        for offset in offsets[1:]:
            buffer.write(f"{offset:010d} 00000 n \n".encode("latin-1"))
        buffer.write(f"trailer << /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\nstartxref\n{xref}\n%%EOF".encode("latin-1"))
        return buffer.getvalue()

    def _new_page(self):
        self.ops = []
        self._text("QUICK", 72, 782, 10, bold=True, color=self.red)
        self._text("bundl", 72, 770, 9, color=self.red)

    def _finish_page(self):
        self.pages.append("\n".join(self.ops))
        self.ops = []

    def _header(self, title, customer_name, mobile_number, email, period, requested_at, page_number):
        self._text("QUICKBUNDL MONEY STATEMENT", 210, 778, 14, bold=True, color=self.red)
        self._text("Customer Name:", 92, 716, 8, bold=True, color=self.red)
        self._text(customer_name, 205, 716, 8, bold=True)
        self._text("Mobile Number:", 92, 698, 8, bold=True, color=self.red)
        self._text(mobile_number, 205, 698, 8, bold=True)
        self._text("Email Address:", 92, 680, 8, bold=True, color=self.red)
        self._text(email, 205, 680, 8, bold=True)
        self._text("Statement Period:", 92, 662, 8, bold=True, color=self.red)
        self._text(period, 205, 662, 8, bold=True)
        self._text("Request Date:", 92, 644, 8, bold=True, color=self.red)
        self._text(requested_at, 205, 644, 8, bold=True)
        self._stamp(requested_at, title)
        if page_number > 1:
            self._text("DETAILED STATEMENT CONTINUED", 210, 725, 11, bold=True, color=self.red)

    def _stamp(self, requested_at, title):
        self._rect(432, 652, 112, 62, stroke=self.red, width=1.4)
        self._text("Approved", 462, 696, 6.5, bold=True)
        self._text(requested_at, 494, 696, 6.5, bold=True)
        self._text(title[:28], 445, 678, 6.5, bold=True)
        self._text("STATEMENT VERIFIED", 455, 664, 6.5, color=self.red, bold=True)

    def _summary(self, summary):
        self._text("SUMMARY", 275, 598, 11, bold=True, color=self.red)
        rows = [
            ("Opening Balance", _money(summary.get("opening_balance_minor"))),
            ("Closing Balance", _money(summary.get("opening_balance_minor", 0) + summary.get("total_credits_minor", 0) - summary.get("total_debits_minor", 0))),
            ("Total Credit", _money(summary.get("total_credits_minor"))),
            ("Total Debit", _money(summary.get("total_debits_minor"))),
        ]
        y = 574
        self._rect(80, y - 58, 435, 58, stroke=self.red, width=0.8)
        for label, value in rows:
            self._line(80, y - 14, 515, y - 14, self.red, 0.6)
            self._line(300, y, 300, y - 14, self.red, 0.6)
            self._text(label, 84, y - 10, 6.5)
            self._text(value, 306, y - 10, 6.5)
            y -= 14
        self._text("DETAILED STATEMENT", 252, 490, 11, bold=True, color=self.red)

    def _transaction_table(self, rows, top_y):
        headers = ["Transaction ID", "Transaction Date", "Description", "Status", "Amount", "Type", "Balance"]
        widths = [70, 66, 132, 62, 58, 54, 58]
        x = 48
        y = top_y
        self._fill_rect(x, y - 18, sum(widths), 18, self.red)
        cursor = x
        for header, width in zip(headers, widths):
            self._text(header, cursor + 3, y - 12, 5.4, bold=True, color="1 1 1")
            cursor += width
        y -= 18
        row_h = 22
        for row in rows:
            cursor = x
            self._rect(x, y - row_h, sum(widths), row_h, stroke=self.red, width=0.5)
            values = [row["id"], row["date"], row["description"], row["status"], row["amount"], row["direction"], row["balance"]]
            for value, width in zip(values, widths):
                self._line(cursor, y, cursor, y - row_h, self.red, 0.4)
                self._text(str(value)[:34], cursor + 3, y - 9, 5.2)
                if width > 70 and len(str(value)) > 34:
                    self._text(str(value)[34:68], cursor + 3, y - 17, 5.2)
                cursor += width
            self._line(cursor, y, cursor, y - row_h, self.red, 0.4)
            y -= row_h

    def _rows(self, summary, transactions):
        balance = summary.get("opening_balance_minor", 0)
        rows = []
        for item in transactions:
            amount = int(item.get("gross_amount_minor") or item.get("amount_minor") or 0)
            balance -= amount
            created = item.get("created_at", "")
            rows.append({
                "id": str(item.get("id") or item.get("instruction_id") or "-")[:10].upper(),
                "date": _pretty_date(created[:10]) + (f" {created[11:16]}" if len(created) >= 16 else ""),
                "description": item.get("description") or f"Sent to {item.get('recipient_name') or 'recipient'}",
                "status": str(item.get("status") or "SUCCEEDED").replace("_", " ").title(),
                "amount": _money(amount),
                "direction": "Debit",
                "balance": _money(balance),
            })
        return rows

    def _footer(self, page_number, page_count):
        self._text("Generated by QuickBundl. For support, quote the transaction ID shown on this statement.", 48, 32, 6, color=self.red)
        self._text(f"Page {page_number} of {page_count}", 505, 32, 6)

    def _text(self, text, x, y, size=8, bold=False, color=None):
        if color:
            self.ops.append(f"{color} rg")
        font = "F2" if bold else "F1"
        self.ops.append(f"BT /{font} {size} Tf {x} {y} Td ({_pdf_escape(text)}) Tj ET")
        if color:
            self.ops.append(f"{self.black} rg")

    def _line(self, x1, y1, x2, y2, color=None, width=1):
        if color:
            self.ops.append(f"{color} RG")
        self.ops.append(f"{width} w {x1} {y1} m {x2} {y2} l S")
        if color:
            self.ops.append(f"{self.black} RG")

    def _rect(self, x, y, w, h, stroke=None, width=1):
        if stroke:
            self.ops.append(f"{stroke} RG")
        self.ops.append(f"{width} w {x} {y} {w} {h} re S")
        if stroke:
            self.ops.append(f"{self.black} RG")

    def _fill_rect(self, x, y, w, h, color):
        self.ops.append(f"{color} rg {x} {y} {w} {h} re f {self.black} rg")
