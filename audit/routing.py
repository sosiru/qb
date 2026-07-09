from django.urls import path

from .realtime import AuditFeedConsumer


websocket_urlpatterns = [
    path("ws/audit-feed/", AuditFeedConsumer.as_asgi()),
]
