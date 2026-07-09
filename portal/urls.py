from django.urls import path

from . import views


app_name = "portal"

urlpatterns = [
    path("", views.root_redirect, name="root"),
    path("app/login/", views.login_view, name="login"),
    path("app/logout/", views.logout_view, name="logout"),
    path("app/", views.dashboard_view, name="dashboard"),
    path("app/bills/", views.bills_view, name="bills"),
    path("app/payments/", views.payments_view, name="payments"),
    path("app/wallet/", views.wallet_view, name="wallet"),
    path("app/vault/", views.vault_view, name="vault"),
    path("app/approvals/", views.approvals_view, name="approvals"),
    path("app/reports/", views.reports_view, name="reports"),
    path("app/statements/", views.statements_view, name="statements"),
]
