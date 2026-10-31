import functools
import json
import logging

from django.contrib.auth.decorators import login_not_required
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from app.providers import services
from users.models import User

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """Error returned to the client as JSON."""

    def __init__(self, status, code, detail=""):
        """Store the HTTP status, a machine-readable code and a message."""
        super().__init__(detail or code)
        self.status = status
        self.code = code
        self.detail = detail


def error_response(status, code, detail=""):
    """Return a JSON error body."""
    return JsonResponse({"error": code, "detail": detail}, status=status)


def _token_from_request(request):
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def json_body(request):
    """Return the request body as a dict or raise a 400 ApiError."""
    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError as error:
        raise ApiError(400, "invalid_json", str(error)) from error
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_json", "Expected a JSON object.")
    return data


def api_view(methods):
    """Authenticate with the user's token and turn errors into JSON responses.

    The token is the same one the media server webhooks use. ``request.user`` is
    set so django-simple-history attributes the changes to the user.
    """

    def decorator(view):
        @login_not_required
        @csrf_exempt
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return error_response(405, "method_not_allowed")

            token = _token_from_request(request)
            user = User.objects.filter(token=token).first() if token else None
            if user is None:
                return error_response(401, "invalid_token")
            request.user = user

            try:
                return view(request, *args, **kwargs)
            except ApiError as error:
                return error_response(error.status, error.code, error.detail)
            except services.ProviderAPIError as error:
                logger.warning("Provider error in API request: %s", error)
                return error_response(502, "provider_error", str(error))

        return wrapper

    return decorator
