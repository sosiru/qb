import hashlib
import json
import logging
from functools import wraps

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from api.models import IntegrationApiKey
from eusers.models import AccessToken

logger = logging.getLogger(__name__)


def json_error(message, status=400, extra=None):
    payload = {"error": message}
    if extra:
        payload.update(extra)
    return JsonResponse(payload, status=status)


def parse_json(request):
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid JSON body.") from exc


def authenticate_request(request):
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        logger.info("auth.request.bearer.present path=%s", request.path)
        raw_token = header.split(" ", 1)[1].strip()
        if raw_token:
            digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
            token = (
                AccessToken.objects.select_related("user")
                .filter(token_hash=digest, revoked_at__isnull=True)
                .first()
            )
            if token and token.is_active():
                token.last_used_at = timezone.now()
                token.save(update_fields=["last_used_at"])
                request.auth_mode = "access_token"
                request.integration_api_key = None
                logger.info("auth.request.bearer.success path=%s user_id=%s", request.path, token.user_id)
                return token.user
        logger.warning("auth.request.bearer.failed path=%s", request.path)

    raw_api_key = request.headers.get("X-API-Key", "").strip()
    if not raw_api_key:
        logger.info("auth.request.no_credentials path=%s", request.path)
        return None
    logger.info("auth.request.api_key.present path=%s", request.path)
    digest = hashlib.sha256(raw_api_key.encode("utf-8")).hexdigest()
    api_key = (
        IntegrationApiKey.objects.select_related("user", "organization")
        .filter(key_hash=digest, revoked_at__isnull=True, is_active=True)
        .first()
    )
    if not api_key or not api_key.is_currently_active():
        logger.warning("auth.request.api_key.failed path=%s", request.path)
        return None
    api_key.last_used_at = timezone.now()
    api_key.save(update_fields=["last_used_at"])
    request.auth_mode = "api_key"
    request.integration_api_key = api_key
    logger.info("auth.request.api_key.success path=%s user_id=%s api_key_id=%s", request.path, api_key.user_id, api_key.id)
    return api_key.user


def api_view(view_func):
    @csrf_exempt
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        return view_func(request, *args, **kwargs)

    return wrapped


def require_auth(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        user = authenticate_request(request)
        if not user:
            logger.warning("auth.required.denied path=%s", request.path)
            return json_error("Authentication required.", status=401)
        request.api_user = user
        if not hasattr(request, "integration_api_key"):
            request.integration_api_key = None
        return view_func(request, *args, **kwargs)

    return wrapped
