"""The `recurse` command-line interface and its Recurse service client.

Private companion module of the Recurse SDK. It orchestrates the public
workflows - browser login, application deployment, and billing - against the
branded Recurse service, stores the device credential in the operating
system keychain, bridges local MCP clients to deployed applications, and
serves the tiny loopback pages the browser sees during login.
"""

import argparse
import base64
import getpass
import hashlib
import http.client
import json
import math
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, redirect_stdout
from decimal import Decimal, DecimalException
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, BinaryIO, NoReturn, cast
from uuid import UUID, uuid4

import keyring
import keyring.errors
import yaml
from filelock import FileLock, Timeout

from recurse import RecurseError, _build_bundle

_DEFAULT_API_URL = "https://api.recurse.run"
_REQUEST_TIMEOUT_SECONDS = 60
_AUTH_LOCK_DIRECTORY = Path("~") / ".recurse" / "locks"
_TOKEN_REFRESH_MARGIN_SECONDS = 30
_POLL_SECONDS = 2.0
_REMOTE_MCP_PROTOCOL = "2026-07-28"
_TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
_RUN_RESOURCES_META_KEY = "recurse.run/resources"
_JSONRPC_SERVER_ERROR = -32000
_JSONRPC_RESOURCE_NOT_FOUND = -32002
_JSONRPC_REQUEST_CANCELLED = -32800
_JSONRPC_INVALID_PARAMS = -32602
_JSONRPC_INTERNAL_ERROR = -32603
_ARTIFACT_URI_PARTS = 2
_MAX_ARTIFACT_SIZE_BYTES = 64_000_000
_SHA256_HEX_LENGTH = 64
_RUNTIME_SECRET_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_RUNTIME_SECRET_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_RUNTIME_SECRET_ENVIRONMENT_NAMES = frozenset({"RECURSE_INPUTS"})
_MAX_RUNTIME_SECRET_BYTES = 32 * 1024
_CREDIT_CODE = re.compile(r"^rc_[A-Za-z0-9_-]{24,128}$")
_MIN_TOP_UP_CENTS = 500
_MAX_TOP_UP_CENTS = 50_000


# Commands outside run/status retain their existing CLI-error exit code.
# Run/status use command-level success (0), failure (1), and interruption (130).
_CLI_ERROR_STATUS = 2


class _CliArgumentParser(argparse.ArgumentParser):
    """Let the command boundary select the appropriate argument-error output."""

    structured_errors = False

    def error(self, message: str) -> NoReturn:
        """Return run/status argument errors for YAML; retain other commands' usage errors."""
        if self.structured_errors:
            raise argparse.ArgumentError(None, message)
        self.print_usage(sys.stderr)
        self.exit(_CLI_ERROR_STATUS, f"{self.prog}: error: {message}\n")


class ServiceError(Exception):
    """A Recurse service request failed with a public error message."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """Retain an HTTP status only when callers can safely retry it."""
        super().__init__(message)
        self.status_code = status_code


def api_base_url() -> str:
    """Return the Recurse API base URL.

    Returns:
        The value of ``RECURSE_API_URL`` when set, otherwise the public
        Recurse API endpoint.

    Raises:
        ServiceError: If the configured URL is not an HTTP(S) URL.
    """
    url = os.environ.get("RECURSE_API_URL", _DEFAULT_API_URL).rstrip("/")
    if not url.startswith(("https://", "http://")):
        raise ServiceError("RECURSE_API_URL must be an http(s) URL")
    return url


def required_field(payload: dict[str, Any], name: str) -> str:
    """Return a required non-empty string field of a service response.

    Args:
        payload: A decoded successful service response body.
        name: The field the caller consumes.

    Returns:
        The field value.

    Raises:
        ServiceError: If the field is missing, empty, or not a string.
    """
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ServiceError(f"the Recurse service returned an invalid response: missing {name}")
    return value


def request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    json_body: dict[str, Any] | None = None,
    json_response: bool = True,
) -> dict[str, Any]:
    """Send one request to the Recurse service and return its JSON body.

    Args:
        method: HTTP method.
        path: Absolute request path, for example ``/v1/account``.
        token: Bearer access token, when the route requires authentication.
        json_body: JSON request body.
        json_response: Whether the successful response must contain a JSON object.

    Returns:
        The decoded JSON response body.

    Raises:
        ServiceError: If the service rejects the request or is unreachable.
            The message carries the service's public error detail.
    """
    body: bytes | None = None
    headers = {}
    if json_body is not None:
        body = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    prepared = urllib.request.Request(  # noqa: S310 - scheme validated above
        api_base_url() + path, data=body, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - scheme validated above
            prepared, timeout=_REQUEST_TIMEOUT_SECONDS
        ) as response:
            raw_payload = response.read()
    except urllib.error.HTTPError as error:
        raw = error.read()
        raise ServiceError(_error_detail(raw, error.reason), error.code) from error
    except (http.client.HTTPException, OSError) as error:
        raise ServiceError(
            "the Recurse service could not be reached. Check your connection and RECURSE_API_URL."
        ) from error
    if not json_response:
        return {}
    try:
        payload: dict[str, Any] = json.loads(raw_payload)
    except ValueError as error:
        raise ServiceError("the Recurse service returned an invalid response") from error
    if not isinstance(payload, dict):
        raise ServiceError("the Recurse service returned an invalid response")
    return payload


def _error_detail(raw: bytes, fallback: object) -> str:
    """Return a public service error detail from an HTTP response.

    Args:
        raw: Raw service response body.
        fallback: Value used when the body has no JSON detail.

    Returns:
        The service detail or fallback text.
    """
    try:
        detail = json.loads(raw)["detail"]
    except KeyError, TypeError, ValueError:
        detail = fallback
    return str(detail)


def upload_artifact(target: dict[str, Any], artifact_bytes: bytes) -> None:
    """Follow one opaque direct-transfer descriptor at most twice.

    A transfer error can arrive after the service stored the bytes. After both
    attempts, the caller must submit the stable receipt for authoritative
    completion instead of treating the last transfer response as final.

    Args:
        target: One artifact's opaque direct-transfer descriptor.
        artifact_bytes: Exact artifact bytes to transfer.

    Raises:
        ServiceError: If the descriptor is malformed.
    """
    upload_url = required_field(target, "upload_url")
    if not upload_url.startswith(("https://", "http://")):
        raise ServiceError("the Recurse service returned an invalid response")
    try:
        prefix = base64.b64decode(required_field(target, "upload_prefix"), validate=True)
        suffix = base64.b64decode(required_field(target, "upload_suffix"), validate=True)
    except ValueError as error:
        raise ServiceError("the Recurse service returned an invalid response") from error
    prepared = urllib.request.Request(  # noqa: S310 - scheme validated above
        upload_url,
        data=prefix + artifact_bytes + suffix,
        headers={
            "Authorization": required_field(target, "upload_authorization"),
            "Content-Type": required_field(target, "upload_content_type"),
            "User-Agent": "recurse-sdk",
        },
        method="POST",
    )
    for _attempt in range(2):
        try:
            with urllib.request.urlopen(  # noqa: S310 - scheme validated above
                prepared, timeout=_REQUEST_TIMEOUT_SECONDS
            ):
                return
        except OSError, ValueError:
            pass


_RECURSE_LOGO_PATH = (
    "M300 300H0V0H300V300ZM71.0273 63.4531C65.6497 63.4531 61.0404 65.2777 57.1992 "
    "68.9268C53.3581 72.3838 51.3415 76.8011 51.1494 82.1787V218.731C51.1494 224.10"
    "9 53.07 228.622 56.9111 232.271C60.9443 235.729 65.6497 237.457 71.0273 237.45"
    "7C82.5508 236.497 88.8887 230.255 90.041 218.731V109.547C90.041 104.745 92.537"
    "8 102.249 97.5312 102.057H198.649C202.299 102.249 205.275 103.113 207.58 104.6"
    "49C210.077 105.994 211.901 107.626 213.054 109.547C214.206 111.275 214.782 113"
    ".1 214.782 115.021C214.782 116.941 214.206 118.862 213.054 120.782C211.901 122"
    ".511 210.077 124.143 207.58 125.68C205.275 127.024 202.299 127.792 198.649 127"
    ".984H122.595C117.217 127.984 112.608 129.905 108.767 133.746C105.118 137.395 1"
    "03.293 141.909 103.293 147.286C103.293 149.399 105.021 159.098 108.479 176.383"
    "C110.975 191.939 117.985 205.383 129.509 216.715C141.032 228.046 153.9 233.616"
    " 168.112 233.424H235.524C240.71 233.232 245.031 231.407 248.488 227.95C252.137"
    " 224.301 254.058 219.884 254.25 214.698C254.25 209.321 252.425 204.711 248.776"
    " 200.87C245.319 197.029 240.902 195.012 235.524 194.82H173.298C166.192 194.82 "
    "160.334 192.516 155.725 187.906C150.923 183.297 147.754 176.191 146.218 166.58"
    "8H196.921C210.365 166.204 221.408 163.131 230.051 157.369C238.885 151.607 245."
    "127 145.077 248.776 137.779C252.618 130.289 254.538 122.703 254.538 115.021C25"
    "4.538 107.53 252.618 100.136 248.776 92.8379C244.935 85.3477 238.693 78.7217 2"
    "30.051 72.96C221.408 67.1982 210.365 64.1253 196.921 63.7412H195.192C194.04 63"
    ".5492 192.888 63.4531 191.735 63.4531H71.0273Z"
)


def _login_result_page(accepted: bool) -> str:
    """Return the styled browser result for one login callback.

    Args:
        accepted: Whether the callback carried the expected authorization state.

    Returns:
        A complete, responsive HTML document for the callback result.
    """
    if accepted:
        status = "success"
        symbol = "✓"
        title = "Authorization received"
        message = "Return to your terminal while Recurse finishes signing you in."
        help_text = (
            "Your terminal will confirm whether login completed and which account is active."
        )
    else:
        status = "failure"
        symbol = "&times;"
        title = "Login not completed"
        message = (
            "The sign-in attempt was not accepted. Close this window and run "
            "<code>recurse login</code> again."
        )
        help_text = "No login credential was stored."
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} · Recurse</title>
  <style>
    :root {{ --bg: #fcfcfd; --fg: #17151f; --muted: #615c70; --line: #e8e6ef; }}
    * {{ box-sizing: border-box; }}
    body {{
      background: var(--bg); color: var(--fg); display: grid;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 0; min-height: 100vh; padding: 24px; place-items: center;
    }}
    .result-card {{
      background: #fff; border: 1px solid var(--line); border-radius: 18px;
      max-width: 420px; padding: 36px; width: 100%;
    }}
    .brand {{ align-items: center; display: flex; font-size: 17px; font-weight: 700; gap: 9px; }}
    .brand-mark {{ display: block; height: 26px; width: 26px; }}
    .status-icon {{
      align-items: center; border-radius: 50%; display: flex; font-size: 24px;
      font-weight: 700; height: 52px; justify-content: center; margin-top: 32px; width: 52px;
    }}
    .result-card--success .status-icon {{ background: #e8f7f1; color: #12805c; }}
    .result-card--failure .status-icon {{ background: #feeeee; color: #b42318; }}
    h1 {{ font-size: 30px; letter-spacing: -.03em; margin: 18px 0 10px; }}
    .message {{ color: var(--muted); line-height: 1.6; margin: 0; }}
    code {{ background: #f1eff6; border-radius: 5px; color: var(--fg); padding: 2px 5px; }}
    .help {{ border-top: 1px solid var(--line); color: var(--muted); font-size: 13px;
      margin: 28px 0 0; padding-top: 20px; }}
    @media (max-width: 480px) {{ .result-card {{ padding: 28px 24px; }} }}
  </style>
</head>
<body>
  <main class="result-card result-card--{status}" aria-labelledby="result-title">
    <div class="brand">
      <svg class="brand-mark" aria-hidden="true" viewBox="0 0 300 300"
           xmlns="http://www.w3.org/2000/svg">
        <g clip-path="url(#recurse-logo-clip)">
          <path d="{_RECURSE_LOGO_PATH}" fill="#5C3DF5"/>
        </g>
        <defs>
          <clipPath id="recurse-logo-clip">
            <rect width="300" height="300" rx="50"/>
          </clipPath>
        </defs>
      </svg>
      Recurse
    </div>
    <div class="status-icon" aria-hidden="true">{symbol}</div>
    <h1 id="result-title">{title}</h1>
    <p class="message">{message}</p>
    <p class="help">{help_text}</p>
  </main>
</body>
</html>
"""


_KEYCHAIN_SERVICE = "recurse-cli"
_KEYCHAIN_DEVICE_CREDENTIAL = "device-credential"
_CALLBACK_PORT = 8765
_LOGIN_WAIT_SECONDS = 300
_POLL_ATTEMPTS = 300
_MIN_CPU_LIMIT = 0.125
_MAX_CPU_LIMIT = 16.0
_MIN_MEMORY_LIMIT_MIB = 768
_MAX_MEMORY_LIMIT_MIB = 16_384


class _CliError(RecurseError):
    """A CLI workflow failed with a user-facing message."""


class _LoginCallback(BaseHTTPRequestHandler):
    """Loopback handler that receives the browser login callback."""

    def do_GET(self) -> None:
        """Accept one login callback and show the browser a result page."""
        server = cast("_LoginServer", self.server)
        requested = urllib.parse.urlparse(self.path)
        query = dict(urllib.parse.parse_qsl(requested.query))
        accepted = (
            requested.path == "/callback"
            and "code" in query
            and secrets.compare_digest(query.get("state", ""), server.expected_state)
        )
        page = _login_result_page(accepted).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)
        if accepted:
            server.code = query["code"]
        if requested.path == "/callback" and query:
            server.done.set()

    def log_message(self, *args: object) -> None:
        """Silence request logging on the user's terminal."""
        del args


class _LoginServer(HTTPServer):
    """Loopback server that waits for exactly one login callback."""

    def __init__(self, expected_state: str) -> None:
        """Bind the Recurse login callback address.

        Args:
            expected_state: The state value a valid callback must echo.
        """
        super().__init__(("127.0.0.1", _CALLBACK_PORT), _LoginCallback)
        self.expected_state = expected_state
        self.code: str | None = None
        self.done = threading.Event()


def _keychain_credential_name() -> str:
    """Scope the saved login to the Recurse API that issued it."""
    return f"{_KEYCHAIN_DEVICE_CREDENTIAL}:{api_base_url()}"


def _device_credential() -> str:
    """Read the durable device credential from the operating system keychain.

    Returns:
        The stored opaque device credential.

    Raises:
        _CliError: If no credential is stored or the keychain is unavailable.
    """
    try:
        credential = keyring.get_password(_KEYCHAIN_SERVICE, _keychain_credential_name())
    except keyring.errors.KeyringError as error:
        raise _CliError(f"the system keychain is unavailable: {error}") from error
    if credential is None:
        raise _CliError("you are not logged in; run: recurse login")
    return credential


def _store_device_credential(tokens: dict[str, object]) -> None:
    """Persist the opaque device credential from an authorization grant.

    Args:
        tokens: An authorization-code token grant response.

    Raises:
        _CliError: If the operating system keychain rejects the write.
    """
    credential = required_field(tokens, "device_credential")
    with _auth_lock():
        _clear_cached_access()
        keyring.set_password(_KEYCHAIN_SERVICE, _keychain_credential_name(), credential)


@contextmanager
def _auth_lock() -> Iterator[None]:
    """Serialize keychain login/cache changes across threads and local processes.

    Only a hash of the API URL is written in the owner-only lock filename;
    credentials and tokens remain in the OS keychain. The OS releases the lock
    if its owning process exits.

    Yields:
        Control while the endpoint-scoped lock is held.

    Raises:
        _CliError: If locking times out, storage fails, or the keychain is unavailable.
    """
    try:
        directory = _AUTH_LOCK_DIRECTORY.expanduser()
    except RuntimeError as error:
        raise _CliError(
            "could not resolve the home directory for the Recurse login lock"
        ) from error
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        identity = hashlib.sha256(api_base_url().encode()).hexdigest()
        with FileLock(directory / f"{identity}.lock", timeout=_REQUEST_TIMEOUT_SECONDS, mode=0o600):
            yield
    except Timeout as error:
        raise _CliError("another Recurse process is refreshing your login; try again") from error
    except OSError as error:
        raise _CliError("could not access the local Recurse login lock") from error
    except keyring.errors.KeyringError as error:
        raise _CliError(f"the system keychain is unavailable: {error}") from error


def _clear_cached_access() -> None:
    """Remove the endpoint's cached bearer while the caller holds its auth lock."""
    name = f"access-token:{api_base_url()}"
    if keyring.get_password(_KEYCHAIN_SERVICE, name) is not None:
        keyring.delete_password(_KEYCHAIN_SERVICE, name)


def _cached_access_token(
    raw: str | None, credential: str, rejected_token: str | None
) -> str | None:
    """Return a valid cached bearer only for the current saved login.

    Args:
        raw: Serialized keychain cache entry, if present.
        credential: Currently saved device credential.
        rejected_token: Bearer rejected by the server, which must not be reused.

    Returns:
        A usable token, or ``None`` for missing, malformed, stale or mismatched data.
    """
    try:
        cached = json.loads(raw or "null")
        if not isinstance(cached, dict):
            return None
        token = cached.get("access_token")
        expires_at = cached.get("expires_at")
        if (
            cached.get("credential_sha256") != hashlib.sha256(credential.encode()).hexdigest()
            or not isinstance(token, str)
            or not token
            or token == rejected_token
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(expires_at)
            or expires_at <= time.time() + _TOKEN_REFRESH_MARGIN_SECONDS
        ):
            return None
        return token
    except ValueError:
        return None


def _exchange_device_credential(credential: str) -> dict[str, Any]:
    """Exchange one durable credential for a short-lived access token.

    Args:
        credential: Opaque device credential read from the keychain.

    Returns:
        The token response, including its lifetime.

    Raises:
        ServiceError: If the service rejects the exchange.
    """
    return request(
        "POST",
        "/v1/auth/token",
        json_body={"grant_type": "device_credential", "device_credential": credential},
    )


def _access_token(rejected_token: str | None = None) -> str:
    """Reuse the saved login's bearer, coordinating a refresh when necessary.

    Args:
        rejected_token: Bearer rejected by the service; a newer cached bearer is reusable.

    Returns:
        A cached or freshly issued access token. If saving the cache fails, the
        fresh token remains usable and a warning is printed to stderr.

    Raises:
        _CliError: If no login is stored or local coordination/storage fails.
        ServiceError: If the exchange fails or its token/lifetime is invalid.
    """
    with _auth_lock():
        credential = _device_credential()
        cache_name = f"access-token:{api_base_url()}"
        raw = keyring.get_password(_KEYCHAIN_SERVICE, cache_name)
        cached = _cached_access_token(raw, credential, rejected_token)
        if cached is not None:
            return cached
        if raw is not None:
            keyring.delete_password(_KEYCHAIN_SERVICE, cache_name)
        started_at = time.time()
        tokens = _exchange_device_credential(credential)
        token = required_field(tokens, "access_token")
        expires_in = tokens.get("expires_in")
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float))
            or not math.isfinite(expires_in)
            or expires_in <= 0
        ):
            raise ServiceError("the Recurse service returned an invalid response: expires_in")
        try:
            keyring.set_password(
                _KEYCHAIN_SERVICE,
                cache_name,
                json.dumps(
                    {
                        "credential_sha256": hashlib.sha256(credential.encode()).hexdigest(),
                        "access_token": token,
                        "expires_at": started_at + expires_in,
                    }
                ),
            )
        except keyring.errors.KeyringError:
            print(
                "warning: cannot save the login token for reuse; allow keychain writes "
                "to avoid repeated sign-ins and rate limits. Continuing with this token.",
                file=sys.stderr,
            )
        return token


def _login() -> None:
    """Run the browser login flow and store the device credential.

    The flow waits on the fixed loopback callback ``127.0.0.1:8765`` that the
    Recurse login page is allowed to redirect to.

    Raises:
        _CliError: If the browser flow does not complete.
        ServiceError: If the token exchange fails.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    state = secrets.token_urlsafe(32)
    try:
        server = _LoginServer(state)
    except OSError as error:
        raise _CliError(
            f"the login callback address 127.0.0.1:{_CALLBACK_PORT} is unavailable: {error}"
        ) from error
    try:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            login_url = (
                api_base_url()
                + "/login?"
                + urllib.parse.urlencode(
                    {
                        "redirect_to": f"http://127.0.0.1:{server.server_port}/callback",
                        "code_challenge": challenge,
                        "state": state,
                    }
                )
            )
            webbrowser.open(login_url)
            print("Complete the login in your browser. Waiting up to 5 minutes...")
            completed = server.done.wait(timeout=_LOGIN_WAIT_SECONDS)
        finally:
            server.shutdown()
            thread.join()
        code = server.code
    finally:
        server.server_close()
    if code is None:
        if not completed:
            raise _CliError("login timed out; run: recurse login")
        raise _CliError("login was not completed in the browser")
    tokens = request(
        "POST",
        "/v1/auth/token",
        json_body={"grant_type": "authorization_code", "code": code, "code_verifier": verifier},
    )
    access_token = required_field(tokens, "access_token")
    account = request("GET", "/v1/account", token=access_token)
    account_id = required_field(account, "account_id")
    display_name = required_field(account, "display_name")
    _store_device_credential(tokens)
    print(f"Logged in to Recurse at {api_base_url()} as {display_name}.")
    print(f"Account: {account_id}")


def _logout() -> None:
    """Revoke the durable credential, then remove it from the keychain.

    Raises:
        _CliError: If the keychain cannot be read or changed.
        ServiceError: If the credential cannot be exchanged or revoked.
    """
    with _auth_lock():
        credential = _device_credential()
        access_token = required_field(_exchange_device_credential(credential), "access_token")
        request(
            "POST",
            "/v1/auth/logout",
            token=access_token,
            json_body={"device_credential": credential},
            json_response=False,
        )
        _clear_cached_access()
        keyring.delete_password(_KEYCHAIN_SERVICE, _keychain_credential_name())
    print(f"Logged out of Recurse at {api_base_url()}.")
    print("Your saved CLI login was removed from your keychain.")


def _mcp_request(
    deployment_id: str,
    body: bytes,
    method: str,
    access_token: str,
    *,
    name: str | None = None,
) -> tuple[int, bytes]:
    """Send one adapted JSON-RPC request to a deployed MCP endpoint.

    Args:
        deployment_id: Public deployment identifier.
        body: Encoded JSON-RPC request body.
        method: Remote MCP method.
        access_token: Short-lived Recurse access token.
        name: Tool name for a tool call.

    Returns:
        HTTP status and exact response body.

    Raises:
        ServiceError: If the service cannot be reached.
    """
    path_id = urllib.parse.quote(deployment_id, safe="")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": _REMOTE_MCP_PROTOCOL,
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    prepared = urllib.request.Request(  # noqa: S310 - scheme validated by api_base_url
        f"{api_base_url()}/mcp/{path_id}",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - scheme validated by api_base_url
            prepared, timeout=_REQUEST_TIMEOUT_SECONDS
        ) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except urllib.error.URLError as error:
        raise ServiceError(f"the Recurse service could not be reached: {error.reason}") from error


def _remote_mcp_meta() -> dict[str, Any]:
    """Return the fixed capability envelope required by the remote endpoint."""
    return {
        "io.modelcontextprotocol/protocolVersion": _REMOTE_MCP_PROTOCOL,
        "io.modelcontextprotocol/clientInfo": {
            "name": "recurse-sdk",
            "version": "0.2.1",
        },
        "io.modelcontextprotocol/clientCapabilities": {"extensions": {_TASKS_EXTENSION: {}}},
    }


def _remote_mcp_body(request_id: object, method: str, params: dict[str, Any]) -> bytes:
    """Encode one remote request with the required capability envelope.

    Args:
        request_id: Host JSON-RPC request identifier.
        method: Remote MCP method.
        params: Host parameters to retain.

    Returns:
        Compact encoded JSON-RPC request bytes.
    """
    remote_params = dict(params)
    remote_meta = _remote_mcp_meta()
    host_meta = params.get("_meta")
    if (
        method == "tools/call"
        and isinstance(host_meta, dict)
        and _RUN_RESOURCES_META_KEY in host_meta
    ):
        remote_meta[_RUN_RESOURCES_META_KEY] = host_meta[_RUN_RESOURCES_META_KEY]
    remote_params["_meta"] = remote_meta
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": remote_params,
        },
        separators=(",", ":"),
    ).encode()


def _decode_mcp_response(status: int, body: bytes) -> dict[str, Any]:
    """Decode a remote response or raise its public service error.

    Args:
        status: HTTP response status.
        body: Raw response bytes.

    Returns:
        Decoded JSON-RPC response object.

    Raises:
        ServiceError: If the response is unsuccessful or malformed.
    """
    try:
        response = json.loads(body)
    except UnicodeDecodeError, ValueError:
        response = None
    if not HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES:
        if isinstance(response, dict) and isinstance(response.get("error"), dict):
            return response
        raise ServiceError(_error_detail(body, f"HTTP {status}"))
    if not isinstance(response, dict) or response.get("jsonrpc") != "2.0":
        raise ServiceError("the Recurse service returned an invalid MCP response")
    return response


def _remote_mcp_request(
    deployment_id: str,
    request_id: object,
    method: str,
    params: dict[str, Any],
    access_token: str,
) -> tuple[dict[str, Any] | None, str]:
    """Send one remote request with one authentication retry.

    Args:
        deployment_id: Public deployment identifier.
        request_id: Host JSON-RPC request identifier.
        method: Remote MCP method.
        params: Request parameters before the remote envelope is added.
        access_token: Current short-lived access token.

    Returns:
        The decoded response, or ``None`` for tolerated unavailability, and the current token.

    Raises:
        ServiceError: If authentication or the remote request fails.
    """
    body = _remote_mcp_body(request_id, method, params)
    name = params.get("name") if method == "tools/call" else None
    status, response_body = _mcp_request(deployment_id, body, method, access_token, name=name)
    if status == HTTPStatus.UNAUTHORIZED:
        access_token = _access_token(rejected_token=access_token)
        status, response_body = _mcp_request(deployment_id, body, method, access_token, name=name)
    if method == "tasks/get" and status in {
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.SERVICE_UNAVAILABLE,
    }:
        return None, access_token
    return _decode_mcp_response(status, response_body), access_token


def _host_result(request_id: object, result: dict[str, Any]) -> dict[str, Any]:
    """Build a standard JSON-RPC result with the host request identifier."""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _host_error(request_id: object, error: dict[str, Any]) -> dict[str, Any]:
    """Build a standard JSON-RPC error with the host request identifier."""
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _response_parts(
    response: dict[str, Any], request_id: object
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return a remote result or a host-addressed error.

    Args:
        response: Decoded remote JSON-RPC response.
        request_id: Host request identifier to preserve.

    Returns:
        A result and no error, or no result and a translated error.

    Raises:
        ServiceError: If the remote response contains neither a result nor an error.
    """
    error = response.get("error")
    if isinstance(error, dict):
        return None, _host_error(request_id, error)
    result = response.get("result")
    if not isinstance(result, dict):
        raise ServiceError("the Recurse service returned an invalid MCP response")
    return result, None


def _initialize_mcp(
    deployment_id: str,
    message: dict[str, Any],
    access_token: str,
) -> tuple[dict[str, Any], str]:
    """Translate a standard host initialization into remote discovery.

    Args:
        deployment_id: Public deployment identifier.
        message: Standard host initialize request.
        access_token: Current short-lived access token.

    Returns:
        Standard host response and the current access token.

    Raises:
        _CliError: If the host request is malformed.
        ServiceError: If discovery returns an invalid response.
    """
    request_id = message.get("id")
    params = message.get("params")
    version = params.get("protocolVersion") if isinstance(params, dict) else None
    if "id" not in message or not isinstance(version, str) or not version:
        raise _CliError("malformed JSON-RPC frame on standard input")
    response, access_token = _remote_mcp_request(
        deployment_id,
        request_id,
        "server/discover",
        {},
        access_token,
    )
    result, error = _response_parts(cast(dict[str, Any], response), request_id)
    if error is not None:
        return error, access_token
    result = cast(dict[str, Any], result)
    capabilities = result.get("capabilities")
    remote_meta = result.get("_meta")
    tools = capabilities.get("tools") if isinstance(capabilities, dict) else None
    server_info = (
        remote_meta.get("io.modelcontextprotocol/serverInfo")
        if isinstance(remote_meta, dict)
        else None
    )
    if not isinstance(tools, dict) or not isinstance(server_info, dict):
        raise ServiceError("the Recurse service returned an invalid MCP discovery response")
    return (
        _host_result(
            request_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": tools, "resources": {}},
                "serverInfo": server_info,
            },
        ),
        access_token,
    )


def _list_mcp_tools(
    deployment_id: str,
    message: dict[str, Any],
    access_token: str,
) -> tuple[dict[str, Any], str]:
    """Forward a standard tool-list request with remote capabilities.

    Args:
        deployment_id: Public deployment identifier.
        message: Standard host tool-list request.
        access_token: Current short-lived access token.

    Returns:
        Standard host response and the current access token.

    Raises:
        _CliError: If the host request is malformed.
        ServiceError: If the remote response is malformed.
    """
    if "id" not in message:
        raise _CliError("malformed JSON-RPC frame on standard input")
    request_id = message["id"]
    params = message.get("params", {})
    if not isinstance(params, dict):
        raise _CliError("malformed JSON-RPC frame on standard input")
    response, access_token = _remote_mcp_request(
        deployment_id, request_id, "tools/list", params, access_token
    )
    result, error = _response_parts(cast(dict[str, Any], response), request_id)
    if error is not None:
        return error, access_token
    return _host_result(request_id, cast(dict[str, Any], result)), access_token


def _artifact_ids(uri: object) -> tuple[str, str, str]:
    """Validate one Recurse artifact URI and return its exact identities.

    Args:
        uri: Host-supplied resource URI.

    Returns:
        The canonical URI, run UUID, and output UUID.

    Raises:
        _CliError: If the URI is not one exact Recurse artifact identity.
    """
    if not isinstance(uri, str):
        raise _CliError("invalid artifact resource URI")
    parsed = urllib.parse.urlparse(uri)
    parts = parsed.path.removeprefix("/").split("/")
    if (
        parsed.scheme != "recurse"
        or parsed.netloc != "artifact"
        or parsed.params
        or parsed.query
        or parsed.fragment
        or len(parts) != _ARTIFACT_URI_PARTS
    ):
        raise _CliError("invalid artifact resource URI")
    try:
        run_id = str(UUID(parts[0]))
        output_id = str(UUID(parts[1]))
    except ValueError as error:
        raise _CliError("invalid artifact resource URI") from error
    canonical = f"recurse://artifact/{run_id}/{output_id}"
    if uri != canonical:
        raise _CliError("invalid artifact resource URI")
    return canonical, run_id, output_id


def _download_artifact(grant: dict[str, Any]) -> bytes:
    """Download and verify bytes authorized by one exact artifact grant."""
    url = required_field(grant, "download_url")
    token = required_field(grant, "download_token")
    sha256 = required_field(grant, "sha256")
    size = grant.get("size_bytes")
    if (
        not url.startswith(("https://", "http://"))
        or not isinstance(size, int)
        or isinstance(size, bool)
        or not 0 <= size <= _MAX_ARTIFACT_SIZE_BYTES
        or len(sha256) != _SHA256_HEX_LENGTH
        or any(character not in "0123456789abcdef" for character in sha256)
    ):
        raise ServiceError("the Recurse service returned an invalid artifact grant")
    prepared = urllib.request.Request(  # noqa: S310 - scheme validated above
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "recurse-sdk/0.2.1",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - scheme validated above
            prepared, timeout=_REQUEST_TIMEOUT_SECONDS
        ) as response:
            body = cast(bytes, response.read(size + 1))
    except urllib.error.HTTPError as error:
        error.read()
        raise ServiceError("artifact download failed") from error
    except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
        raise ServiceError("artifact download failed") from error
    if len(body) != size or hashlib.sha256(body).hexdigest() != sha256:
        raise ServiceError("artifact verification failed")
    return body


def _read_mcp_resource(message: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Read one owner-authorized artifact through a standard MCP resource URI."""
    if "id" not in message or not isinstance(message.get("params"), dict):
        raise _CliError("malformed JSON-RPC frame on standard input")
    request_id = message["id"]
    uri, run_id, output_id = _artifact_ids(message["params"].get("uri"))
    access_token = _access_token()
    path = f"/v1/runs/{run_id}/artifacts/{output_id}"
    try:
        grant = request("GET", path, token=access_token)
    except ServiceError as error:
        if error.status_code != HTTPStatus.UNAUTHORIZED:
            raise
        access_token = _access_token(rejected_token=access_token)
        grant = request("GET", path, token=access_token)
    body = _download_artifact(grant)
    return (
        _host_result(
            request_id,
            {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "application/octet-stream",
                        "blob": base64.b64encode(body).decode(),
                    }
                ]
            },
        ),
        access_token,
    )


def _list_mcp_resources(message: dict[str, Any]) -> dict[str, Any]:
    """Return the empty global list used by call-scoped artifact resources."""
    if "id" not in message or not isinstance(message.get("params", {}), dict):
        raise _CliError("malformed JSON-RPC frame on standard input")
    return _host_result(message["id"], {"resources": []})


def _list_mcp_resource_templates(message: dict[str, Any]) -> dict[str, Any]:
    """Return the empty template list used by exact artifact resource URIs."""
    if "id" not in message or not isinstance(message.get("params", {}), dict):
        raise _CliError("malformed JSON-RPC frame on standard input")
    return _host_result(message["id"], {"resourceTemplates": []})


def _poll_interval_seconds(result: dict[str, Any]) -> float:
    """Return a validated task polling interval in seconds.

    Args:
        result: Remote task status result.

    Returns:
        Non-negative interval in seconds.

    Raises:
        ServiceError: If the interval is absent or invalid.
    """
    interval = result.get("pollIntervalMs")
    if isinstance(interval, bool) or not isinstance(interval, int) or interval < 0:
        raise ServiceError("the Recurse service returned an invalid MCP task response")
    return interval / 1000


def _terminal_task_response(
    result: dict[str, Any], request_id: object, task_id: str
) -> dict[str, Any] | None:
    """Translate a terminal task status, or identify a working task.

    Args:
        result: Remote task status result.
        request_id: Host request identifier.
        task_id: Public task identifier retained from admission.

    Returns:
        Host response for a terminal status, or ``None`` while work continues.

    Raises:
        ServiceError: If the task response is invalid.
    """
    status = result.get("status")
    if status == "working":
        return None
    if status == "completed":
        call_result = result.get("result")
        if not isinstance(call_result, dict) or not isinstance(call_result.get("content"), list):
            raise ServiceError("the Recurse service returned an invalid MCP task response")
        return _host_result(
            request_id,
            {
                **call_result,
                "content": [
                    *call_result["content"],
                    {"type": "text", "text": f"Task: {task_id}"},
                ],
            },
        )
    if status == "cancelled":
        return _host_error(
            request_id,
            {
                "code": _JSONRPC_REQUEST_CANCELLED,
                "message": f"tool call was cancelled Task: {task_id}",
            },
        )
    if status == "failed":
        error = result.get("error")
        if not isinstance(error, dict) or not isinstance(error.get("message"), str):
            raise ServiceError("the Recurse service returned an invalid MCP task response")
        return _host_error(request_id, {**error, "message": f"{error['message']} Task: {task_id}"})
    raise ServiceError("the Recurse service returned an invalid MCP task response")


def _call_mcp_tool(
    deployment_id: str,
    message: dict[str, Any],
    access_token: str,
    cancelled: threading.Event,
) -> tuple[dict[str, Any] | None, str]:
    """Submit one tool call and poll its admitted task to completion.

    Args:
        deployment_id: Public deployment identifier.
        message: Standard host tool-call request.
        access_token: Current short-lived access token.
        cancelled: Host cancellation signal for this exact request.

    Returns:
        Standard host response and the current access token.

    Raises:
        _CliError: If the host request is malformed.
        ServiceError: If the remote response is malformed.
    """
    if "id" not in message:
        raise _CliError("malformed JSON-RPC frame on standard input")
    request_id = message["id"]
    params = message.get("params")
    if not isinstance(params, dict):
        raise _CliError("malformed JSON-RPC frame on standard input")
    name = params.get("name")
    if not isinstance(name, str) or not name or not name.isascii() or not name.isprintable():
        raise _CliError("malformed JSON-RPC frame on standard input")
    response, access_token = _remote_mcp_request(
        deployment_id,
        request_id,
        "tools/call",
        params,
        access_token,
    )
    result, error = _response_parts(cast(dict[str, Any], response), request_id)
    if error is not None:
        return error, access_token
    result = cast(dict[str, Any], result)
    task_id = result.get("taskId")
    if not isinstance(task_id, str) or not task_id:
        raise ServiceError("the Recurse service returned an invalid MCP task response")
    while True:
        if cancelled.is_set():
            response, access_token = _remote_mcp_request(
                deployment_id,
                request_id,
                "tasks/cancel",
                {"taskId": task_id},
                access_token,
            )
            _response_parts(cast(dict[str, Any], response), request_id)
            return None, access_token
        terminal = _terminal_task_response(result, request_id, task_id)
        if terminal is not None:
            return terminal, access_token
        interval = _poll_interval_seconds(result)
        if cancelled.wait(interval):
            continue
        response, access_token = _remote_mcp_request(
            deployment_id,
            request_id,
            "tasks/get",
            {"taskId": task_id},
            access_token,
        )
        if response is None:
            continue
        result, error = _response_parts(response, request_id)
        if error is not None:
            return (
                _terminal_task_response(
                    {"status": "failed", "error": error["error"]}, request_id, task_id
                ),
                access_token,
            )
        result = cast(dict[str, Any], result)


def _mcp_message(message: object) -> tuple[str, dict[str, Any]]:
    """Validate one host frame and return its method and object.

    Args:
        message: Decoded newline-delimited JSON value.

    Returns:
        Method name and validated JSON-RPC object.

    Raises:
        _CliError: If the frame is malformed.
    """
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        raise _CliError("malformed JSON-RPC frame on standard input")
    method = message.get("method")
    if not isinstance(method, str) or not method:
        raise _CliError("malformed JSON-RPC frame on standard input")
    return method, message


def _write_mcp_response(output_stream: BinaryIO, response: dict[str, Any]) -> None:
    """Write one compact newline-delimited host response."""
    output_stream.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
    output_stream.flush()


def _serve_mcp(  # noqa: PLR0915 - coordinates one explicit protocol dispatcher
    deployment_id: str, input_stream: Iterable[bytes], output_stream: BinaryIO
) -> None:
    """Bridge newline-delimited stdio JSON-RPC to one Recurse MCP deployment.

    Args:
        deployment_id: Public deployment identifier.
        input_stream: Binary newline-delimited JSON-RPC input.
        output_stream: Binary newline-delimited JSON-RPC output.

    Raises:
        _CliError: If standard input contains a malformed frame.
        ServiceError: If authentication or the deployed service fails.
    """
    try:
        access_token = _access_token()
    except _CliError as error:
        access_token = ""
        authentication_error: str | None = str(error)
    except ServiceError as error:
        access_token = ""
        authentication_error = (
            f"{error}. If your saved login was revoked or expired, run: recurse login"
        )
    else:
        authentication_error = None
    initialized = False
    active_requests: dict[str, tuple[threading.Event, threading.Thread]] = {}
    active_lock = threading.Lock()
    output_lock = threading.Lock()

    def call_key(request_id: object) -> str:
        """Return one stable key for a JSON-RPC request identifier."""
        return json.dumps(request_id, sort_keys=True, separators=(",", ":"))

    def run_request(
        key: str,
        message: dict[str, Any],
        cancelled: threading.Event,
        method: str,
    ) -> None:
        """Complete one blocking host request without blocking protocol dispatch."""
        try:
            if method == "tools/call":
                response, _token = _call_mcp_tool(
                    deployment_id, message, _access_token(), cancelled
                )
            else:
                response, _token = _read_mcp_resource(message)
        except _CliError as error:
            response = _host_error(
                message["id"],
                {"code": _JSONRPC_INVALID_PARAMS, "message": str(error)},
            )
        except ServiceError as error:
            code = (
                _JSONRPC_RESOURCE_NOT_FOUND if method == "resources/read" else _JSONRPC_SERVER_ERROR
            )
            response = _host_error(message["id"], {"code": code, "message": str(error)})
        except Exception:
            response = _host_error(
                message["id"],
                {"code": _JSONRPC_INTERNAL_ERROR, "message": "internal error"},
            )
        try:
            if response is not None and not cancelled.is_set():
                with output_lock:
                    _write_mcp_response(output_stream, response)
        finally:
            with active_lock:
                active_requests.pop(key, None)

    def dispatch() -> None:  # noqa: PLR0912,PLR0915 - explicit protocol dispatch
        """Read and dispatch host frames until the input stream closes."""
        nonlocal access_token, initialized
        for line in input_stream:
            body = line.removesuffix(b"\n").removesuffix(b"\r")
            try:
                message = json.loads(body)
            except (UnicodeDecodeError, ValueError) as error:
                raise _CliError("malformed JSON-RPC frame on standard input") from error
            method, message = _mcp_message(message)
            if method == "initialize":
                if authentication_error is not None:
                    response = _host_error(
                        message.get("id"),
                        {"code": _JSONRPC_SERVER_ERROR, "message": authentication_error},
                    )
                    with output_lock:
                        _write_mcp_response(output_stream, response)
                    return
                response, access_token = _initialize_mcp(deployment_id, message, access_token)
                initialized = "result" in response
            elif not initialized:
                raise _CliError("the first MCP frame must be initialize")
            elif method == "notifications/initialized":
                continue
            elif method == "tools/list":
                response, access_token = _list_mcp_tools(deployment_id, message, access_token)
            elif method == "resources/list":
                response = _list_mcp_resources(message)
            elif method == "resources/templates/list":
                response = _list_mcp_resource_templates(message)
            elif method in {"tools/call", "resources/read"}:
                if "id" not in message:
                    raise _CliError("malformed JSON-RPC frame on standard input")
                key = call_key(message["id"])
                cancelled = threading.Event()
                thread = threading.Thread(
                    target=run_request,
                    args=(key, message, cancelled, method),
                )
                with active_lock:
                    if key in active_requests:
                        raise _CliError("duplicate in-flight JSON-RPC request id")
                    active_requests[key] = (cancelled, thread)
                thread.start()
                continue
            elif method == "notifications/cancelled":
                params = message.get("params")
                if isinstance(params, dict) and "requestId" in params:
                    with active_lock:
                        active = active_requests.get(call_key(params["requestId"]))
                    if active is not None:
                        active[0].set()
                continue
            elif "id" not in message:
                continue
            else:
                response = _host_error(
                    message["id"], {"code": -32601, "message": "method not found"}
                )
            with output_lock:
                _write_mcp_response(output_stream, response)

    def finish_active_requests(cancel: bool) -> None:
        """Optionally cancel, then join every request started before dispatch stopped."""
        with active_lock:
            active = list(active_requests.values())
        if cancel:
            for cancelled, _thread in active:
                cancelled.set()
        for _cancelled, thread in active:
            thread.join()

    try:
        dispatch()
    except BaseException:
        finish_active_requests(cancel=True)
        raise
    else:
        finish_active_requests(cancel=False)


def _prepare(
    app_directory: str,
    *,
    token: str | None = None,
    billing_retry_target: str = "deployment",
) -> tuple[str, str, str | None]:
    """Build, upload, and prepare one immutable application version.

    Args:
        app_directory: The application directory to prepare.
        token: An access token already obtained while resolving secret bindings.
        billing_retry_target: The caller action named after a billing denial.

    Returns:
        The short-lived access token, prepared version identifier, and resolved model if supplied.

    Raises:
        RecurseError: If the application fails authoring validation.
        _CliError: If preparation fails or does not finish.
        ServiceError: If the service rejects a request.
    """
    print("Packaging application...", flush=True)
    artifacts, record, manifest = _build_bundle(app_directory)
    metadata = manifest["metadata"]
    if token is None:
        token = _access_token()
    target, token = _authenticated_request(
        "POST",
        "/v1/agent-versions",
        token=token,
        json_body={
            "agent_name": metadata["name"],
            "summary": metadata["summary"],
            "application_record": record,
        },
    )
    version_id = required_field(target, "version_id")
    source_target = target.get("source_upload")
    if not isinstance(source_target, dict):
        raise ServiceError("the Recurse service returned an invalid response")
    print("Uploading source distribution...", flush=True)
    upload_artifact(source_target, artifacts["source"])
    print("Preparing runtime...", flush=True)
    _, token = _authenticated_request(
        "POST",
        f"/v1/agent-versions/{version_id}/complete",
        token=token,
        json_body={
            "source_upload_id": required_field(source_target, "upload_id"),
        },
    )
    for _attempt in range(_POLL_ATTEMPTS):
        version, token = _authenticated_request(
            "GET", f"/v1/agent-versions/{version_id}", token=token
        )
        build_status = required_field(version, "status")
        if build_status == "ready":
            if isinstance(model := version.get("model"), str):
                print(f"model: {model}", flush=True)
            break
        if build_status == "failed":
            build_error = required_field(version, "error")
            if build_error == "unsupported_model":
                raise _CliError(
                    "The selected model is unsupported or unavailable. Check agent.model in "
                    "agent.yaml; remove it to use the default, or select an enabled model."
                )
            if build_error == "insufficient_balance":
                raise _CliError(
                    "Wallet balance is too low. Run recurse billing top-up 5 or "
                    "recurse billing redeem CODE, then retry "
                    f"{billing_retry_target}."
                )
            raise _CliError(f"deployment build failed: {build_error}")
        time.sleep(_POLL_SECONDS)
    else:
        raise _CliError("the deployment build did not finish; try again later")
    return token, version_id, model if isinstance(model, str) else None


def _deploy(
    app_directory: str,
    cpu_limit: float,
    memory_limit_mib: int,
    secret_bindings: list[str] | None = None,
) -> None:
    """Prepare and create one permanent MCP deployment."""
    resolved_token, resolved_bindings = _resolve_runtime_secret_bindings(secret_bindings)
    token, version_id, _model = _prepare(app_directory, token=resolved_token)
    print("Creating MCP deployment...", flush=True)
    request_body: dict[str, Any] = {
        "version_id": version_id,
        "cpu_limit": cpu_limit,
        "memory_limit_mib": memory_limit_mib,
    }
    if resolved_bindings:
        request_body["secret_bindings"] = resolved_bindings
    deployment = request(
        "POST",
        "/v1/deployments",
        token=token,
        json_body=request_body,
    )
    deployment_id = required_field(deployment, "deployment_id")
    endpoint = required_field(deployment, "endpoint")
    deployed_cpu = deployment.get("cpu_limit")
    deployed_memory = deployment.get("memory_limit_mib")
    if (
        isinstance(deployed_cpu, bool)
        or not isinstance(deployed_cpu, int | float)
        or isinstance(deployed_memory, bool)
        or not isinstance(deployed_memory, int)
    ):
        raise ServiceError(
            "the Recurse service returned an invalid response: missing deployment resources"
        )
    print(f"deployment: {deployment_id}")
    print(f"endpoint: {endpoint}")
    print(f"resources: {deployed_cpu:g} CPU, {deployed_memory} MiB")


def _read_run_inputs(source: str | None) -> dict[str, Any]:
    """Read one JSON object from a file, standard input, or the empty default."""
    if source is None:
        return {}
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
        inputs = json.loads(text)
    except (OSError, UnicodeError, ValueError) as error:
        raise _CliError("run inputs must be a readable JSON object") from error
    if not isinstance(inputs, dict):
        raise _CliError("run inputs must be a readable JSON object")
    return inputs


def _validate_runtime_secret_name(name: str) -> None:
    """Require the stable lowercase logical-name syntax."""
    if _RUNTIME_SECRET_NAME.fullmatch(name) is None:
        raise _CliError(
            "secret names must start with a lowercase letter and contain only "
            "lowercase letters, digits, or hyphens"
        )


def _decode_runtime_secret(raw_value: bytes) -> str:
    """Decode one exact, non-empty UTF-8 value bounded to 32 KiB."""
    if not raw_value:
        raise _CliError("secret value must not be empty")
    if len(raw_value) > _MAX_RUNTIME_SECRET_BYTES:
        raise _CliError("secret value must not exceed 32 KiB")
    try:
        return raw_value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _CliError("secret value must be valid UTF-8") from error


def _read_runtime_secret(*, from_stdin: bool) -> str:
    """Read exact stdin bytes or two hidden, matching interactive values."""
    if from_stdin:
        return _decode_runtime_secret(sys.stdin.buffer.read())
    try:
        value = getpass.getpass("Secret value: ")
        confirmation = getpass.getpass("Confirm secret value: ")
    except EOFError as error:
        raise _CliError("secret entry was cancelled") from error
    try:
        raw_value = value.encode("utf-8")
        raw_confirmation = confirmation.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _CliError("secret value must be valid UTF-8") from error
    if not secrets.compare_digest(raw_value, raw_confirmation):
        raise _CliError("secret values do not match")
    return _decode_runtime_secret(raw_value)


def _runtime_secret_metadata(payload: object) -> dict[str, Any]:
    """Validate one metadata-only runtime-secret service record."""
    if not isinstance(payload, dict) or "value" in payload:
        raise ServiceError("the Recurse service returned invalid secret metadata")
    try:
        secret_id = str(UUID(payload["secret_id"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ServiceError("the Recurse service returned invalid secret metadata") from error
    name = payload.get("name")
    version = payload.get("active_version")
    created_at = payload.get("created_at")
    updated_at = payload.get("updated_at")
    binding_count = payload.get("deployment_binding_count")
    if (
        not isinstance(name, str)
        or _RUNTIME_SECRET_NAME.fullmatch(name) is None
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version < 1
        or not isinstance(created_at, str)
        or not created_at
        or not isinstance(updated_at, str)
        or not updated_at
        or isinstance(binding_count, bool)
        or not isinstance(binding_count, int)
        or binding_count < 0
    ):
        raise ServiceError("the Recurse service returned invalid secret metadata")
    return {
        "secret_id": secret_id,
        "name": name,
        "active_version": version,
        "created_at": created_at,
        "updated_at": updated_at,
        "deployment_binding_count": binding_count,
    }


def _runtime_secret_version_metadata(payload: object) -> dict[str, Any]:
    """Validate the metadata-only result for one created secret version."""
    if not isinstance(payload, dict) or "value" in payload:
        raise ServiceError("the Recurse service returned invalid secret metadata")
    try:
        secret_id = str(UUID(payload["secret_id"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ServiceError("the Recurse service returned invalid secret metadata") from error
    name = payload.get("name")
    version = payload.get("version")
    created_at = payload.get("created_at")
    updated_at = payload.get("updated_at")
    if (
        not isinstance(name, str)
        or _RUNTIME_SECRET_NAME.fullmatch(name) is None
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version < 1
        or not isinstance(created_at, str)
        or not created_at
        or not isinstance(updated_at, str)
        or not updated_at
    ):
        raise ServiceError("the Recurse service returned invalid secret metadata")
    return {
        "secret_id": secret_id,
        "name": name,
        "version": version,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _runtime_secrets(token: str) -> list[dict[str, Any]]:
    """Load and validate metadata for the signed-in account."""
    response = request("GET", "/v1/runtime-secrets", token=token)
    records = response.get("secrets")
    if not isinstance(records, list):
        raise ServiceError("the Recurse service returned invalid secret metadata")
    return [_runtime_secret_metadata(record) for record in records]


def _resolve_runtime_secret_bindings(
    bindings: list[str] | None,
) -> tuple[str | None, dict[str, str]]:
    """Resolve explicit environment-to-logical-name mappings before preparation."""
    if not bindings:
        return None, {}

    requested: dict[str, str] = {}
    for binding in bindings:
        environment_name, separator, logical_name = binding.partition("=")
        if (
            not separator
            or _RUNTIME_SECRET_ENVIRONMENT_NAME.fullmatch(environment_name) is None
            or environment_name in _RESERVED_RUNTIME_SECRET_ENVIRONMENT_NAMES
        ):
            raise _CliError("secret bindings must use an available ENVIRONMENT_NAME=secret-name")
        _validate_runtime_secret_name(logical_name)
        if environment_name in requested:
            raise _CliError(f"duplicate secret environment name: {environment_name}")
        requested[environment_name] = logical_name

    token = _access_token()
    secrets_by_name = {record["name"]: record for record in _runtime_secrets(token)}
    resolved: dict[str, str] = {}
    for environment_name, logical_name in requested.items():
        secret = secrets_by_name.get(logical_name)
        if secret is None:
            raise _CliError(f"runtime secret was not found: {logical_name}")
        print(f"secret binding: {environment_name}={logical_name}")
        resolved[environment_name] = secret["secret_id"]
    return token, resolved


def _secret_set(name: str, *, from_stdin: bool) -> None:
    """Create or rotate one encrypted account-owned runtime secret."""
    _validate_runtime_secret_name(name)
    value = _read_runtime_secret(from_stdin=from_stdin)
    metadata = _runtime_secret_version_metadata(
        _retry_request(
            "POST",
            "/v1/runtime-secrets",
            token=_access_token(),
            json_body={
                "name": name,
                "value": value,
                "idempotency_key": f"secret_{uuid4().hex}",
            },
        )
    )
    if metadata["name"] != name:
        raise ServiceError("the Recurse service returned invalid secret metadata")
    print(f"secret: {metadata['name']}")
    print(f"version: {metadata['version']}")
    print(f"updated: {metadata['updated_at']}")


def _secret_list() -> None:
    """Print account-secret metadata without any stored value."""
    records = _runtime_secrets(_access_token())
    print("NAME\tVERSION\tMCP BINDINGS\tUPDATED")
    for record in records:
        print(
            f"{record['name']}\t{record['active_version']}\t"
            f"{record['deployment_binding_count']}\t{record['updated_at']}"
        )


def _secret_delete(name: str, *, confirmed: bool) -> None:
    """Confirm and destroy one account-owned runtime secret."""
    _validate_runtime_secret_name(name)
    token = _access_token()
    match = next(
        (record for record in _runtime_secrets(token) if record["name"] == name),
        None,
    )
    if match is None:
        raise _CliError("runtime secret was not found")
    if not confirmed:
        count = match["deployment_binding_count"]
        try:
            answer = input(f"Delete {name} and remove it from {count} MCP deployments? [y/N] ")
        except EOFError as error:
            raise _CliError("secret deletion was cancelled") from error
        if answer.strip().lower() not in {"y", "yes"}:
            print(f"not deleted: {name}")
            return
    secret_id = urllib.parse.quote(match["secret_id"], safe="")
    request(
        "DELETE",
        f"/v1/runtime-secrets/{secret_id}",
        token=token,
        json_response=False,
    )
    print(f"deleted: {name}")


def _manage_secret(arguments: argparse.Namespace) -> None:
    """Dispatch one parsed runtime-secret management subcommand."""
    if arguments.secret_command == "set":  # noqa: S105 - command name, not a credential
        _secret_set(arguments.name, from_stdin=arguments.from_stdin)
    elif arguments.secret_command == "list":  # noqa: S105 - command name, not a credential
        _secret_list()
    else:
        _secret_delete(arguments.name, confirmed=arguments.yes)


def _retry_request(
    method: str,
    path: str,
    *,
    token: str,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Retry one safe or idempotent public request after transient gateway failure."""
    try:
        return request(method, path, token=token, json_body=json_body)
    except ServiceError as error:
        if error.status_code not in {
            HTTPStatus.BAD_GATEWAY,
            HTTPStatus.SERVICE_UNAVAILABLE,
        }:
            raise
    time.sleep(_POLL_SECONDS)
    return request(method, path, token=token, json_body=json_body)


def _run_request(
    method: str,
    path: str,
    *,
    token: str,
    json_body: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """Send a run request and refresh one expired access token.

    Args:
        method: HTTP method for the public run route.
        path: Public API path.
        token: Current short-lived access token.
        json_body: Optional request object retained exactly across retry.

    Returns:
        The response object and the access token that succeeded.

    Raises:
        ServiceError: If the request fails after the permitted authentication retry.
    """
    try:
        return _retry_request(method, path, token=token, json_body=json_body), token
    except ServiceError as error:
        if error.status_code != HTTPStatus.UNAUTHORIZED:
            raise
    token = _access_token(rejected_token=token)
    return _retry_request(method, path, token=token, json_body=json_body), token


def _authenticated_request(
    method: str,
    path: str,
    *,
    token: str,
    json_body: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """Refresh one rejected bearer without adding transport retries."""
    try:
        return request(method, path, token=token, json_body=json_body), token
    except ServiceError as error:
        if error.status_code != HTTPStatus.UNAUTHORIZED:
            raise
    token = _access_token(rejected_token=token)
    return request(method, path, token=token, json_body=json_body), token


_TERMINAL_RUN_STATUSES = {
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    "preempted",
    # Retained only for the legacy cancellation response contract.
    "infrastructure_failed",
}
_RUN_STATUSES = {"queued", "running", *_TERMINAL_RUN_STATUSES}
_PUBLIC_RUN_STATUSES = _RUN_STATUSES - {"infrastructure_failed"}
_PUBLIC_RUN_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_OUTPUT_AVAILABILITIES = {"pending", "available", "expired"}
_BIDI_CONTROL_CHARACTERS = (
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)
_EXPLICIT_YAML_CONTROL_CHARACTERS = _BIDI_CONTROL_CHARACTERS + "\u2028\u2029\ufeff"
_YAML_PRINTABLE_CODEPOINT_RANGES = (
    (0x20, 0x7E),
    (0xA0, 0xD7FF),
    (0xE000, 0xFFFD),
    (0x10000, 0x10FFFE),
)


def _run_yaml_requires_escape(character: str) -> bool:
    """Identify terminal, bidi, and non-printable characters in one scalar."""
    codepoint = ord(character)
    return character in _EXPLICIT_YAML_CONTROL_CHARACTERS or not any(
        lower <= codepoint <= upper for lower, upper in _YAML_PRINTABLE_CODEPOINT_RANGES
    )


def _double_quoted_yaml_character(character: str) -> str:
    """Render one character without losing readable Unicode or YAML round-tripping."""
    if character in {'"', "\\"}:
        return f"\\{character}"
    if _run_yaml_requires_escape(character):
        return ascii(character)[1:-1]
    return character


class _RunViewDumper(yaml.SafeDumper):
    """Render run snapshots with readable Unicode and inert bidi controls."""

    def write_double_quoted(self, text: str, split: bool = True) -> None:
        """Write quoted text without PyYAML's broad non-ASCII escaping."""
        del split
        self.write_indicator('"', True)
        rendered = "".join(_double_quoted_yaml_character(character) for character in text)
        self.column += len(rendered)
        self.stream.write(rendered)
        self.write_indicator('"', False)


def _represent_run_view_string(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    """Use double quotes where a later YAML escape must retain its meaning."""
    style = '"' if any(_run_yaml_requires_escape(character) for character in value) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_RunViewDumper.add_representer(str, _represent_run_view_string)


def _same_run_id(returned: str, requested: str) -> bool:
    """Compare equivalent UUID spellings while retaining strict fallback behavior.

    Args:
        returned: Run identifier returned by the service.
        requested: Run identifier supplied by the user.

    Returns:
        Whether both values identify the same run.
    """
    try:
        return UUID(returned) == UUID(requested)
    except ValueError:
        return returned == requested


def _invalid_run_response() -> NoReturn:
    """Reject a malformed canonical run snapshot without echoing its contents."""
    raise ServiceError("the Recurse service returned an invalid run response")


def _validated_run_resources(value: object) -> dict[str, int | float]:
    """Validate and order the admitted resource limits in a run snapshot."""
    if not isinstance(value, dict) or not {"cpu_limit", "memory_limit_mib"} <= value.keys():
        _invalid_run_response()
    cpu_limit = value.get("cpu_limit")
    memory_limit_mib = value.get("memory_limit_mib")
    if (
        isinstance(cpu_limit, bool)
        or not isinstance(cpu_limit, int | float)
        or not math.isfinite(cpu_limit)
        or cpu_limit <= 0
        or isinstance(memory_limit_mib, bool)
        or not isinstance(memory_limit_mib, int)
        or memory_limit_mib <= 0
    ):
        _invalid_run_response()
    return {"cpu_limit": cpu_limit, "memory_limit_mib": memory_limit_mib}


def _validated_run_cost(value: object) -> dict[str, object]:
    """Validate and order one run's settled-or-pending cost."""
    if (
        not isinstance(value, dict)
        or not {"currency", "total_microusd"} <= value.keys()
        or value.get("currency") != "USD"
    ):
        _invalid_run_response()
    total_microusd = value.get("total_microusd")
    if total_microusd is not None and (
        isinstance(total_microusd, bool)
        or not isinstance(total_microusd, int)
        or total_microusd < 0
    ):
        _invalid_run_response()
    return {"currency": "USD", "total_microusd": total_microusd}


def _validated_run_error(value: object, status: str) -> dict[str, str] | None:
    """Validate and order the safe structured error for one lifecycle state."""
    if value is None:
        projected = None
    elif isinstance(value, dict):
        if not {"code", "message"} <= value.keys():
            _invalid_run_response()
        code = value.get("code")
        message = value.get("message")
        if (
            not isinstance(code, str)
            or _PUBLIC_RUN_ERROR_CODE.fullmatch(code) is None
            or not isinstance(message, str)
            or not message
        ):
            _invalid_run_response()
        projected = {"code": code, "message": message}
    else:
        _invalid_run_response()
    if (status in {"queued", "running", "succeeded"}) != (projected is None):
        _invalid_run_response()
    return projected


def _validated_run_artifacts(value: object) -> list[dict[str, object]] | None:
    """Validate and order retained public artifact metadata."""
    if value is None:
        return None
    if not isinstance(value, list):
        _invalid_run_response()
    projected = []
    for artifact in value:
        if (
            not isinstance(artifact, dict)
            or not {
                "id",
                "path",
                "size_bytes",
                "sha256",
            }
            <= artifact.keys()
        ):
            _invalid_run_response()
        artifact_id = artifact.get("id")
        path = artifact.get("path")
        size_bytes = artifact.get("size_bytes")
        sha256 = artifact.get("sha256")
        if (
            not isinstance(artifact_id, str)
            or not artifact_id
            or not isinstance(path, str)
            or not path
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
            or not isinstance(sha256, str)
            or len(sha256) != _SHA256_HEX_LENGTH
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            _invalid_run_response()
        try:
            UUID(artifact_id)
        except ValueError:
            _invalid_run_response()
        projected.append(
            {"id": artifact_id, "path": path, "size_bytes": size_bytes, "sha256": sha256}
        )
    return projected


def _validated_run_outputs(value: object, status: str) -> dict[str, object]:
    """Validate and order result availability and artifact inventory."""
    if (
        not isinstance(value, dict)
        or not {
            "availability",
            "expires_at",
            "result",
            "artifacts",
        }
        <= value.keys()
    ):
        _invalid_run_response()
    availability = value.get("availability")
    expires_at = value.get("expires_at")
    result = value.get("result")
    artifacts = _validated_run_artifacts(value.get("artifacts"))
    if (
        not isinstance(availability, str)
        or availability not in _OUTPUT_AVAILABILITIES
        or (expires_at is not None and not isinstance(expires_at, str))
        or (result is not None and not isinstance(result, dict))
    ):
        _invalid_run_response()
    if (status in {"queued", "running"}) != (availability == "pending"):
        _invalid_run_response()
    if availability in {"pending", "expired"} and result is not None:
        _invalid_run_response()
    if availability == "pending" and artifacts is not None:
        _invalid_run_response()
    if availability in {"available", "expired"} and artifacts is None:
        _invalid_run_response()
    return {
        "availability": availability,
        "expires_at": expires_at,
        "result": result,
        "artifacts": artifacts,
    }


def _validated_public_run_view(payload: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Validate and project one version-1 Engine snapshot in canonical key order."""
    if not {
        "schema_version",
        "run_id",
        "status",
        "created_at",
        "started_at",
        "completed_at",
        "resources",
        "cost",
        "error",
        "outputs",
    } <= payload.keys() or (
        payload.get("schema_version") != 1 or isinstance(payload.get("schema_version"), bool)
    ):
        _invalid_run_response()
    returned_run_id = payload.get("run_id")
    model = payload.get("model")
    status = payload.get("status")
    created_at = payload.get("created_at")
    started_at = payload.get("started_at")
    completed_at = payload.get("completed_at")
    if (
        not isinstance(returned_run_id, str)
        or not _same_run_id(returned_run_id, run_id)
        or (model is not None and (not isinstance(model, str) or not model))
        or not isinstance(status, str)
        or status not in _PUBLIC_RUN_STATUSES
        or not isinstance(created_at, str)
        or not created_at
        or (started_at is not None and not isinstance(started_at, str))
        or (completed_at is not None and not isinstance(completed_at, str))
    ):
        _invalid_run_response()

    return {
        "schema_version": 1,
        "run_id": returned_run_id,
        "model": model,
        "status": status,
        "created_at": created_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "resources": _validated_run_resources(payload.get("resources")),
        "cost": _validated_run_cost(payload.get("cost")),
        "error": _validated_run_error(payload.get("error"), status),
        "outputs": _validated_run_outputs(payload.get("outputs"), status),
    }


def _get_run(run_id: str, token: str) -> dict[str, Any]:
    """Load and validate one account-owned public run."""
    quoted_run_id = urllib.parse.quote(run_id, safe="")
    return _validated_public_run_view(
        _retry_request("GET", f"/v1/runs/{quoted_run_id}", token=token), run_id
    )


def _print_run_view(view: dict[str, Any]) -> None:
    """Write and flush YAML mapping fields to standard output."""
    rendered = yaml.dump(view, Dumper=_RunViewDumper, sort_keys=False, allow_unicode=True)
    sys.stdout.write(rendered)
    sys.stdout.flush()


def _print_run_recovery(run_id: str) -> None:
    """Keep the cancel command's existing recovery guidance on stdout."""
    print("Remote state is unconfirmed. Execution and charges may continue.")
    print(f"run: {run_id}")
    print("Inspect this run before starting another run.")
    print(f"inspect: recurse status {run_id}")
    print(f"cancel: recurse cancel {run_id}")


class _RunOutput:
    """Keep an early run ID and the final outcome in one YAML document."""

    def __init__(self, run_id: str | None = None) -> None:
        """Retain recovery identity even before any output has been written."""
        self.run_id = run_id
        self.admission_attempted = False
        self.model: str | None = None
        self.started = False

    def start(self, run_id: str) -> None:
        """Flush the admitted ID before the first status request."""
        self.run_id = run_id
        _print_run_view({"run_id": run_id})
        self.started = True

    def finish(self, view: dict[str, Any]) -> None:
        """Append fields with one optional typed error and no repeated run ID."""
        view = dict(view)
        error = view.get("error")
        if error is None:
            view.pop("error", None)
        else:
            view["error"] = {"type": "engine", **error}
        if self.model is not None and view.get("model") is None:
            view = {**view, "model": self.model}
        if self.started:
            view = {key: value for key, value in view.items() if key != "run_id"}
        elif self.run_id is not None:
            view = {"run_id": self.run_id, **view}
        _print_run_view(view)

    def fail(self, error: Exception | KeyboardInterrupt, *, status: str | None = None) -> int:
        """Finish a command failure without inventing a remote execution outcome."""
        detail = _run_cli_error(error)
        view: dict[str, Any] = {}
        if status is not None:
            view["status"] = status
        if (self.run_id is not None or self.admission_attempted) and (
            status not in _TERMINAL_RUN_STATUSES
        ):
            recovery = {
                "message": "Remote state is unconfirmed. Execution and charges may continue. "
                "Inspect this run before starting another run."
            }
            if self.run_id is not None:
                recovery["inspect"] = f"recurse status {self.run_id}"
                recovery["cancel"] = f"recurse cancel {self.run_id}"
            else:
                recovery["message"] += " Run identity is unknown; do not blindly retry."
            view["recovery"] = recovery
        view["error"] = {"type": "cli", **detail}
        self.finish(view)
        print(f"error: {detail['code']}: {detail['message']}", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1


def _run(  # noqa: PLR0912,PLR0913 - explicit admission, polling, reporting and Ctrl-C paths
    app_directory: str,
    inputs_source: str | None,
    cpu_limit: float,
    memory_limit_mib: int,
    secret_bindings: list[str] | None = None,
    *,
    non_preemptible: bool = False,
) -> int:
    """Prepare, admit, and observe a run; remote outcomes do not set the exit code."""
    output = _RunOutput()
    try:
        inputs = _read_run_inputs(inputs_source)
        with open(os.devnull, "w") as progress, redirect_stdout(progress):
            resolved_token, resolved_bindings = _resolve_runtime_secret_bindings(secret_bindings)
            token, version_id, output.model = _prepare(
                app_directory,
                token=resolved_token,
                billing_retry_target="the run",
            )
        admission_body = {
            "version_id": version_id,
            "idempotency_key": f"run_{uuid4().hex}",
            "inputs": inputs,
            "cpu_limit": cpu_limit,
            "memory_limit_mib": memory_limit_mib,
        }
        if resolved_bindings:
            admission_body["secret_bindings"] = resolved_bindings
        if non_preemptible:
            admission_body["execution_mode"] = "non_preemptible"
        output.admission_attempted = True
        admitted, token = _run_request("POST", "/v1/runs", token=token, json_body=admission_body)
        run_id = required_field(admitted, "run_id")
        output.run_id = run_id
        if admitted.get("status") != "queued":
            raise ServiceError("the Recurse service returned an invalid run response")
        output.start(run_id)
        while True:
            try:
                quoted_run_id = urllib.parse.quote(run_id, safe="")
                payload, token = _run_request("GET", f"/v1/runs/{quoted_run_id}", token=token)
                view = _validated_public_run_view(payload, run_id)
            except ServiceError as error:
                if error.status_code not in {
                    HTTPStatus.BAD_GATEWAY,
                    HTTPStatus.SERVICE_UNAVAILABLE,
                }:
                    raise
                time.sleep(_POLL_SECONDS)
                continue
            if view["status"] in _TERMINAL_RUN_STATUSES:
                output.finish(view)
                return 0
            time.sleep(_POLL_SECONDS)
    except KeyboardInterrupt as error:
        cancelled_status = None
        if output.admission_attempted:
            try:
                cancel_run_id = output.run_id
                if cancel_run_id is None:
                    # Recover a possibly accepted admission with its original idempotency key.
                    admitted, token = _run_request(
                        "POST", "/v1/runs", token=token, json_body=admission_body
                    )
                    cancel_run_id = required_field(admitted, "run_id")
                    output.start(cancel_run_id)
                cancelled_status = _request_cancellation(cancel_run_id)
            except Exception, KeyboardInterrupt:
                return output.fail(error)
        return output.fail(error, status=cancelled_status)
    except Exception as error:
        if (
            output.run_id is None
            and isinstance(error, ServiceError)
            and error.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        ):
            output.admission_attempted = False
        return output.fail(error)


def _status(run_id: str) -> int:
    """Report retrieval success separately from the run's state."""
    output = _RunOutput(run_id)
    try:
        view = _get_run(run_id, _access_token())
    except (Exception, KeyboardInterrupt) as error:
        return output.fail(error)
    output.finish(view)
    return 0


def _request_cancellation(run_id: str) -> str:
    """Request cancellation and validate the returned identity and state."""
    token = _access_token()
    quoted_run_id = urllib.parse.quote(run_id, safe="")
    response = _retry_request("POST", f"/v1/runs/{quoted_run_id}/cancel", token=token)
    if not _same_run_id(required_field(response, "run_id"), run_id):
        raise ServiceError("the Recurse service returned an invalid run response")
    run_status = required_field(response, "status")
    if run_status not in _RUN_STATUSES:
        raise ServiceError("the Recurse service returned an invalid run response")
    return run_status


def _cancel(run_id: str) -> str:
    """Request cancellation and print its confirmed state for the cancel command."""
    run_status = _request_cancellation(run_id)
    print(f"run: {run_id}")
    print(f"status: {run_status}")
    return run_status


def _artifact_path(output_directory: Path, path: object) -> Path:
    """Resolve one artifact path without allowing escape or ambiguous separators."""
    if not isinstance(path, str):
        raise ServiceError("the Recurse service returned invalid artifact metadata")
    parts = path.split("/")
    if path.startswith("/") or "\\" in path or any(part in {"", ".", ".."} for part in parts):
        raise ServiceError("the Recurse service returned invalid artifact metadata")
    root = output_directory.resolve()
    candidate = root.joinpath(*parts)
    destination = candidate.resolve(strict=False)
    if not destination.is_relative_to(root):
        raise ServiceError("the Recurse service returned invalid artifact metadata")
    if candidate != destination:
        raise _CliError(f"artifact destination path contains a symlink: {candidate}")
    return destination


def _artifacts(run_id: str, output_directory: str) -> None:
    """Download every retained artifact after exact-byte verification."""
    token = _access_token()
    quoted_run_id = urllib.parse.quote(run_id, safe="")
    payload, token = _run_request("GET", f"/v1/runs/{quoted_run_id}", token=token)
    view = _validated_public_run_view(payload, run_id)
    outputs = cast(dict[str, Any], view["outputs"])
    availability = cast(str, outputs["availability"])
    if availability == "pending":
        raise _CliError("run outputs are not finalized")
    if availability == "expired":
        raise _CliError(
            f"run outputs have expired; inspect retained metadata with recurse status {run_id}"
        )
    root = Path(output_directory)
    artifacts = cast(list[dict[str, Any]], outputs["artifacts"])
    for artifact in artifacts:
        artifact_id = required_field(artifact, "id")
        path = _artifact_path(root, artifact.get("path"))
        if path.exists() and not path.is_file():
            raise _CliError(f"artifact destination is not a regular file: {path}")
        grant, token = _authenticated_request(
            "GET",
            f"/v1/runs/{quoted_run_id}/artifacts/" + urllib.parse.quote(artifact_id, safe=""),
            token=token,
        )
        body = _download_artifact(grant)
        temporary_name: str | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
                temporary.write(body)
                temporary_name = temporary.name
            os.replace(temporary_name, path)
        except OSError as error:
            raise _CliError(f"could not save artifact {artifact['path']}: {error}") from error
        finally:
            if temporary_name is not None and os.path.exists(temporary_name):
                os.unlink(temporary_name)
        print(f"downloaded: {artifact['path']}")


def _cpu_limit(value: str) -> float:
    """Parse one supported CPU ceiling for deployment defaults."""
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("CPU must be a number") from None
    if (
        not math.isfinite(parsed)
        or not _MIN_CPU_LIMIT <= parsed <= _MAX_CPU_LIMIT
        or parsed * 8 != int(parsed * 8)
    ):
        raise argparse.ArgumentTypeError("CPU must be from 0.125 to 16 in 0.125 increments")
    return parsed


def _memory_limit_mib(value: str) -> int:
    """Parse one supported memory ceiling for deployment defaults."""
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("memory must be a whole MiB value") from None
    if not _MIN_MEMORY_LIMIT_MIB <= parsed <= _MAX_MEMORY_LIMIT_MIB or parsed % 128:
        raise argparse.ArgumentTypeError(
            "memory must be from 768 to 16384 MiB in 128 MiB increments"
        )
    return parsed


def _top_up_amount(value: str) -> int:
    """Parse a finite USD amount into exact integer cents for Checkout."""
    message = "amount must be from $5.00 to $500.00 with at most two decimals"
    try:
        dollars = Decimal(value)
        cent_amount = dollars.quantize(Decimal("0.01"))
        cents = dollars * 100
    except DecimalException:
        raise argparse.ArgumentTypeError(message) from None
    if (
        dollars != cent_amount
        or cents != cents.to_integral_value()
        or not _MIN_TOP_UP_CENTS <= cents <= _MAX_TOP_UP_CENTS
    ):
        raise argparse.ArgumentTypeError(message)
    return int(cents)


def _credit_code(value: str) -> str:
    """Accept only the opaque public shape of a Recurse credit code."""
    if not _CREDIT_CODE.fullmatch(value):
        raise argparse.ArgumentTypeError("invalid Recurse credit code")
    return value


def _wallet_view(response: dict[str, Any]) -> tuple[int, int]:
    """Validate and return total and spendable micro-USD balances."""
    balance = response.get("balance_microusd")
    available = response.get("available_balance_microusd")
    if (
        isinstance(balance, bool)
        or not isinstance(balance, int)
        or isinstance(available, bool)
        or not isinstance(available, int)
        or available > balance
    ):
        raise ServiceError("the Recurse service returned an invalid wallet response")
    return balance, available


def _print_wallet(response: dict[str, Any], *, confirmation: str | None = None) -> None:
    """Print wallet balances without losing sub-cent usage precision."""
    balance, available = _wallet_view(response)
    if confirmation is not None:
        print(confirmation)
    print(f"Balance: ${Decimal(balance) / 1_000_000:.6f}")
    print(f"Available: ${Decimal(available) / 1_000_000:.6f}")


def _billing_balance() -> None:
    """Print the authenticated account's total and spendable balances."""
    _print_wallet(request("GET", "/v1/billing", token=_access_token()))


def _billing_top_up(amount_cents: int, *, open_browser: bool) -> None:
    """Open hosted Checkout for one exact prepaid wallet amount."""
    response = request(
        "POST",
        "/v1/billing/checkout",
        token=_access_token(),
        json_body={
            "amount_cents": amount_cents,
            "idempotency_key": str(uuid4()),
        },
    )
    _open_hosted_page(required_field(response, "checkout_url"), open_browser=open_browser)


def _billing_redeem(code: str) -> None:
    """Redeem one opaque credit code and print the resulting wallet."""
    response = request(
        "POST",
        "/v1/billing/credit-codes/redeem",
        token=_access_token(),
        json_body={"code": code},
    )
    _print_wallet(response, confirmation="Credit applied.")


def _manage_billing(arguments: argparse.Namespace) -> None:
    """Dispatch one validated wallet subcommand."""
    if arguments.billing_command == "balance":
        _billing_balance()
    elif arguments.billing_command == "top-up":
        _billing_top_up(arguments.amount, open_browser=not arguments.no_open)
    else:
        _billing_redeem(arguments.code)


def _open_hosted_page(url: str, *, open_browser: bool) -> None:
    """Open a hosted page, printing its URL only when no browser opens.

    Args:
        url: The hosted page to open.
        open_browser: Whether to attempt a browser launch.
    """
    if not open_browser:
        print("Continue to Recurse billing here:")
        print(url)
        return
    try:
        opened = webbrowser.open(url)
    except webbrowser.Error:
        opened = False
    if opened:
        print("Opened Recurse billing in your browser.")
    else:
        print("Your browser did not open. Continue to Recurse billing here:")
        print(url)


def _build_parser(*, structured_errors: bool = False) -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = _CliArgumentParser(
        prog="recurse", description="Run, deploy, and manage Recurse applications."
    )
    parser.structured_errors = structured_errors
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="log in to Recurse in your browser")
    commands.add_parser("logout", help="revoke this device login")
    deploy = commands.add_parser("deploy", help="build and deploy an application")
    deploy.add_argument("app", help="application directory containing agent.yaml")
    deploy.add_argument(
        "--as",
        dest="deployment_kind",
        choices=["mcp"],
        required=True,
        help="deployment kind",
    )
    deploy.add_argument(
        "--cpu",
        type=_cpu_limit,
        default=1.0,
        help="default run CPU ceiling (default: 1)",
    )
    deploy.add_argument(
        "--memory-mib",
        type=_memory_limit_mib,
        default=1024,
        help="default run memory ceiling in MiB (default: 1024)",
    )
    deploy.add_argument(
        "--secret",
        action="append",
        metavar="ENV=NAME",
        help="bind an environment variable to an account secret (repeatable)",
    )
    run = commands.add_parser(
        "run",
        help="run an application to completion",
        epilog=(
            "Runs are preemptible by default. Interruption detection is best-effort; "
            "some interruptions may not be reported as preempted. The CLI does not "
            "automatically retry a reported preempted run. Inspect external effects "
            "before deciding whether to start a new run, which may repeat them."
        ),
    )
    run.structured_errors = structured_errors
    run.add_argument("app", help="application directory containing agent.yaml")
    run.add_argument(
        "--inputs",
        metavar="FILE",
        help="JSON object file, or - for standard input (default: empty object)",
    )
    run.add_argument("--cpu", type=_cpu_limit, default=1.0, help="run CPU ceiling (default: 1)")
    run.add_argument(
        "--memory-mib",
        type=_memory_limit_mib,
        default=1024,
        help="run memory ceiling in MiB (default: 1024)",
    )
    run.add_argument(
        "--secret",
        action="append",
        metavar="ENV=NAME",
        help="bind an environment variable to an account secret (repeatable)",
    )
    run.add_argument(
        "--non-preemptible",
        action="store_true",
        help=(
            "avoid Modal Function preemption at 3x Function CPU and memory cost; "
            "other failures can still occur"
        ),
    )
    run_status = commands.add_parser("status", help="inspect a run")
    run_status.structured_errors = structured_errors
    run_status.add_argument("run_id", help="run identifier")
    cancel = commands.add_parser("cancel", help="cancel a run")
    cancel.add_argument("run_id", help="run identifier")
    artifacts = commands.add_parser("artifacts", help="download retained run artifacts")
    artifacts.add_argument("run_id", help="run identifier")
    artifacts.add_argument(
        "--output", default=".", help="destination directory (default: current directory)"
    )
    secret = commands.add_parser("secret", help="manage encrypted runtime secrets")
    secret_commands = secret.add_subparsers(dest="secret_command", required=True)
    secret_set = secret_commands.add_parser("set", help="create or rotate a secret")
    secret_set.add_argument("name", help="lowercase logical secret name")
    secret_set.add_argument(
        "--from-stdin",
        action="store_true",
        help="read the exact UTF-8 value from standard input",
    )
    secret_commands.add_parser("list", help="list secret metadata")
    secret_delete = secret_commands.add_parser("delete", help="destroy a secret")
    secret_delete.add_argument("name", help="lowercase logical secret name")
    secret_delete.add_argument(
        "--yes", action="store_true", help="skip the destructive confirmation"
    )
    mcp = commands.add_parser("mcp", help="connect an MCP host to a deployment")
    mcp_commands = mcp.add_subparsers(dest="mcp_command", required=True)
    serve = mcp_commands.add_parser("serve", help="serve a deployment over local stdio")
    serve.add_argument("deployment_id", help="deployment identifier")
    billing = commands.add_parser("billing", help="manage your Recurse balance")
    billing_commands = billing.add_subparsers(dest="billing_command", required=True)
    billing_commands.add_parser("balance", help="show total and available balance")
    top_up = billing_commands.add_parser("top-up", help="add $5 to $500 through Checkout")
    top_up.add_argument(
        "amount",
        type=_top_up_amount,
        metavar="AMOUNT",
        help="USD amount from 5 to 500 with at most two decimals",
    )
    top_up.add_argument(
        "--no-open", action="store_true", help="print the checkout URL without opening a browser"
    )
    redeem = billing_commands.add_parser("redeem", help="redeem a Recurse credit code")
    redeem.add_argument(
        "code",
        type=_credit_code,
        metavar="CODE",
    )
    return parser


def _cli_error_message(error: RecurseError | ServiceError) -> str:
    """Render a public error without exposing private authentication or server details."""
    message = str(error)
    if isinstance(error, ServiceError):
        if error.status_code == HTTPStatus.UNAUTHORIZED:
            message = "authentication_failed: Sign-in was rejected. Run `recurse login`."
        elif (
            error.status_code is not None and error.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR
        ):
            reason = http.client.responses.get(error.status_code, "Server error")
            message = (
                f"request_failed: {reason} (HTTP {error.status_code}). "
                "The Recurse service could not complete the request."
            )
        else:
            message = f"request_failed: {message}"
    return message


def _run_cli_error(error: Exception | KeyboardInterrupt) -> dict[str, str]:
    """Separate local errors from the Engine's remote execution error object."""
    if isinstance(error, KeyboardInterrupt):
        return {"code": "interrupted", "message": "Interrupted."}
    if isinstance(error, argparse.ArgumentError):
        return {"code": "invalid_arguments", "message": str(error)}
    if isinstance(error, (RecurseError, ServiceError)):
        message = _cli_error_message(error)
        code, separator, detail = message.partition(": ")
        if separator and code in {"request_failed", "authentication_failed"}:
            return {"code": code, "message": detail}
        return {"code": "cli_error", "message": message}
    return {
        "code": "internal_error",
        "message": "The CLI encountered an unexpected internal error.",
    }


def _print_cli_error(error: RecurseError | ServiceError, arguments: argparse.Namespace) -> None:
    """Keep the existing error interface for commands outside run and status."""
    message = _cli_error_message(error)
    if arguments.command == "cancel":
        _print_run_recovery(arguments.run_id)
    print(f"error: {message}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0912 - explicit command dispatch
    """Run the `recurse` command.

    Args:
        argv: Command-line arguments; defaults to ``sys.argv[1:]``.

    Returns:
        Run/status exit 0 when their operation completes, 1 for command errors,
        or 130 on interruption. Other commands retain their existing exit codes.
    """
    argv = sys.argv[1:] if argv is None else argv
    parser = _build_parser(structured_errors=bool(argv and argv[0] in {"run", "status"}))
    try:
        arguments = parser.parse_args(argv)
    except argparse.ArgumentError as error:
        return _RunOutput().fail(error)
    try:
        if arguments.command == "login":
            _login()
        elif arguments.command == "logout":
            _logout()
        elif arguments.command == "deploy":
            _deploy(arguments.app, arguments.cpu, arguments.memory_mib, arguments.secret)
        elif arguments.command == "run":
            return _run(
                arguments.app,
                arguments.inputs,
                arguments.cpu,
                arguments.memory_mib,
                arguments.secret,
                non_preemptible=arguments.non_preemptible,
            )
        elif arguments.command == "status":
            return _status(arguments.run_id)
        elif arguments.command == "cancel":
            _cancel(arguments.run_id)
        elif arguments.command == "artifacts":
            _artifacts(arguments.run_id, arguments.output)
        elif arguments.command == "secret":
            _manage_secret(arguments)
        elif arguments.command == "mcp":
            _serve_mcp(arguments.deployment_id, sys.stdin.buffer, sys.stdout.buffer)
        else:
            _manage_billing(arguments)
    except (RecurseError, ServiceError) as error:
        _print_cli_error(error, arguments)
        return _CLI_ERROR_STATUS
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    return 0
