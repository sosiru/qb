from reports.models import ReportExport

from base.services import export_transactions_csv_rows


def record_transaction_export(user, organization=None, file_name="transactions.csv", filters=None):
    export = ReportExport.objects.create(
        requested_by=user,
        organization=organization,
        export_type=ReportExport.ExportType.TRANSACTION_HISTORY,
        file_format=ReportExport.FileFormat.CSV,
        filters=filters or {},
    )
    export.mark_generated(file_name)
    return export
