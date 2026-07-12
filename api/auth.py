import hashlib
import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import wraps

from django.http import JsonResponse
from django.http.request import QueryDict
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


def _querydict_to_dict(querydict):
    if not isinstance(querydict, QueryDict):
        return dict(querydict or {})
    data = {}
    for key in querydict.keys():
        values = querydict.getlist(key)
        data[key] = values if len(values) > 1 else querydict.get(key)
    return data


def get_request_data(request):
    """
    Return request data as a plain dict across JSON, form, multipart, and query-string requests.

    Raises ValueError for invalid JSON so API views can return a 400 instead of silently
    accepting a malformed payload.
    """
    if request is None:
        return {}

    method = (getattr(request, "method", "") or "").upper()
    content_type = (getattr(request, "content_type", "") or request.META.get("CONTENT_TYPE", "") or "").split(";", 1)[0].strip().lower()

    if method == "GET":
        return _querydict_to_dict(getattr(request, "GET", {}))

    if content_type == "application/json" or content_type.endswith("+json"):
        body = getattr(request, "body", b"") or b""
        if not body:
            return {}
        try:
            parsed = json.loads(body.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {"data": parsed}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.exception("get_request_data.invalid_json path=%s", getattr(request, "path", ""))
            raise ValueError("Invalid JSON body.") from exc

    if content_type in {"multipart/form-data", "application/x-www-form-urlencoded"}:
        return _querydict_to_dict(getattr(request, "POST", {}))

    body = getattr(request, "body", b"") or b""
    if body:
        try:
            parsed = json.loads(body.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {"data": parsed}
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.info("get_request_data.unparsed_body path=%s content_type=%s", getattr(request, "path", ""), content_type)

    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        post_data = _querydict_to_dict(getattr(request, "POST", {}))
        if post_data:
            return post_data

    return {}


def get_clean_request_data(request):
    data = dict(get_request_data(request))
    for key in ("target", "source_ip", "token", "system", "client_id", "client_secret"):
        data.pop(key, None)
    return data


def json_super_serializer(obj):
    if isinstance(obj, datetime):
        try:
            return obj.strftime("%d/%m/%Y %I:%M:%S %p")
        except Exception:
            return str(obj)
    if isinstance(obj, date):
        try:
            return obj.strftime("%d/%m/%Y")
        except Exception:
            return str(obj)
    if isinstance(obj, (Decimal, float)):
        return str("{:,}".format(round(Decimal(obj), 2)))
    if isinstance(obj, timedelta):
        return obj.days
    return str(obj)


def parse_json(request):
    try:
        return get_request_data(request)
    except ValueError:
        raise


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
