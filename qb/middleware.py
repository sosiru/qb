import logging
import re
import time
import uuid

logger = logging.getLogger(__name__)
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


class RequestLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started_at = time.monotonic()
        logger.info(
            "request.start method=%s path=%s query=%s",
            request.method,
            request.path,
            request.META.get("QUERY_STRING", ""),
        )
        try:
            response = self.get_response(request)
        except Exception:
            duration_ms = round((time.monotonic() - started_at) * 1000, 2)
            logger.exception(
                "request.error method=%s path=%s duration_ms=%s",
                request.method,
                request.path,
                duration_ms,
            )
            raise

        duration_ms = round((time.monotonic() - started_at) * 1000, 2)
        user = getattr(request, "api_user", None)
        logger.info(
            "request.finish method=%s path=%s status=%s duration_ms=%s user_id=%s",
            request.method,
            request.path,
            response.status_code,
            duration_ms,
            getattr(user, "id", None),
        )
        self._audit_request(request, response, duration_ms)
        return response

    def _audit_request(self, request, response, duration_ms):
        if request.method not in MUTATING_METHODS or not request.path.startswith("/api/"):
            return
        if request.method == "OPTIONS":
            return

        try:
            from audit.models import AuditLog

            target_id = self._target_id_from_path(request.path)
            route_name = getattr(getattr(request, "resolver_match", None), "url_name", "") or ""
            user = getattr(request, "api_user", None)
            AuditLog.objects.create(
                actor=user,
                action="http.request",
                target_type=route_name or "api_request",
                target_id=target_id,
                metadata={
                    "method": request.method,
                    "path": request.path,
                    "query_string": request.META.get("QUERY_STRING", ""),
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                    "route_name": route_name,
                    "auth_mode": getattr(request, "auth_mode", None),
                    "integration_api_key_id": str(getattr(getattr(request, "integration_api_key", None), "id", "")) or None,
                    "ip_address": self._client_ip(request),
                    "user_agent": request.META.get("HTTP_USER_AGENT", ""),
                },
            )
        except Exception:
            logger.exception("audit.request.failed method=%s path=%s", request.method, request.path)

    def _target_id_from_path(self, path):
        match = UUID_PATTERN.search(path)
        if match:
            return uuid.UUID(match.group(0))
        return uuid.uuid4()

    def _client_ip(self, request):
        forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if forwarded_for:
            return forwarded_for.split(",", 1)[0].strip()
        return request.META.get("REMOTE_ADDR", "")
