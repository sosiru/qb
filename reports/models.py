from django.db import models
from django.utils import timezone

from base.common import TimestampedModel
from base.utils import generate_uuid


class ReportExport(TimestampedModel):
    class ExportType(models.TextChoices):
        TRANSACTION_HISTORY = "TRANSACTION_HISTORY", "Transaction History"
        WALLET_STATEMENT = "WALLET_STATEMENT", "Wallet Statement"

    class FileFormat(models.TextChoices):
        CSV = "CSV", "CSV"
        PDF = "PDF", "PDF"
        XLSX = "XLSX", "XLSX"

    class Status(models.TextChoices):
        REQUESTED = "REQUESTED", "Requested"
        GENERATED = "GENERATED", "Generated"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=generate_uuid, editable=False)
    requested_by = models.ForeignKey("eusers.User", on_delete=models.CASCADE, related_name="report_exports")
    organization = models.ForeignKey(
        "base.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="report_exports",
    )
    export_type = models.CharField(max_length=32, choices=ExportType.choices)
    file_format = models.CharField(max_length=8, choices=FileFormat.choices, default=FileFormat.CSV)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.REQUESTED)
    file_name = models.CharField(max_length=255, blank=True)
    filters = models.JSONField(default=dict, blank=True)
    generated_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True)

    def mark_generated(self, file_name):
        self.status = self.Status.GENERATED
        self.file_name = file_name
        self.generated_at = timezone.now()
        self.last_error = ""
        self.save(update_fields=["status", "file_name", "generated_at", "last_error", "updated_at"])
