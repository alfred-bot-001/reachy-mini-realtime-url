from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response


SERVICE_NAME = "reachy-mini-realtime-url"
UPSTREAM_ENV_NAME = "UPSTREAM_SESSION_URL"
UPSTREAM_B_ENV_NAME = "UPSTREAM_SESSION_URL_B"
DEFAULT_TIMEOUT_SECONDS = 10.0
FORWARDED_RESPONSE_HEADERS = frozenset({
    b"content-type",
    b"retry-after",
    b"www-authenticate",
    b"authentication-info",
    b"location",
    b"allow",
    b"request-id",
    b"x-request-id",
    b"x-correlation-id",
    b"x-amzn-requestid",
    b"x-amzn-trace-id",
    b"ratelimit",
})

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(SERVICE_NAME)

app = FastAPI(title="Reachy Mini Realtime URL", version="0.1.0")


@dataclass(frozen=True)
class UpstreamConfig:
    session_urls: tuple[str, ...]
    timeout_s: float


def _timeout_seconds() -> float:
    raw_timeout = os.getenv("REQUEST_TIMEOUT_SECONDS", "").strip()
    if not raw_timeout:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = float(raw_timeout)
    except ValueError:
        logger.warning("Invalid REQUEST_TIMEOUT_SECONDS=%r; using default %.1f", raw_timeout, DEFAULT_TIMEOUT_SECONDS)
        return DEFAULT_TIMEOUT_SECONDS
    return max(0.1, timeout)


def _configured_session_url() -> str | None:
    return os.getenv(UPSTREAM_ENV_NAME, "").strip() or None


def _validate_session_url(session_url: str) -> str:
    parsed = urlsplit(session_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("upstream session URL must be an absolute http(s) URL")
    return session_url


def _upstream_config() -> UpstreamConfig | None:
    session_url = _configured_session_url()
    if session_url is None:
        return None
    session_urls: tuple[str, ...] = (_validate_session_url(session_url),)
    alternative_url = os.getenv(UPSTREAM_B_ENV_NAME, "").strip()
    if alternative_url:
        session_urls += (_validate_session_url(alternative_url),)
    return UpstreamConfig(session_urls=session_urls, timeout_s=_timeout_seconds())


def _no_store_headers() -> dict[str, str]:
    return {"Cache-Control": "no-store"}


def _proxy_response(upstream: httpx.Response, *, session_url: str) -> Response:
    # HTTPX has decoded the body. Recompute framing and retain our cache policy
    # instead of copying Content-Encoding, Content-Length, or Cache-Control.
    response = Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=_no_store_headers(),
    )
    connection_fields = {
        field.strip().lower()
        for name, value in upstream.headers.raw
        if name.lower() == b"connection"
        for field in value.split(b",")
    }
    for name, value in upstream.headers.raw:
        name = name.lower()
        allowed = name in FORWARDED_RESPONSE_HEADERS or name.startswith((b"ratelimit-", b"x-ratelimit-"))
        if not allowed or name in connection_fields:
            continue
        if name == b"location":
            # Relative locations refer to the allocator, not the Space's origin.
            value = urljoin(session_url, value.decode("latin-1")).encode("latin-1")
        # Keep repeated authentication challenges as separate header fields.
        response.raw_headers.append((name, value))
    if "content-type" not in response.headers:
        response.headers["Content-Type"] = "application/json"
    return response


def _bearer_authorization(value: str | None) -> str | None:
    scheme, separator, token = (value or "").partition(" ")
    token = token.strip()
    if not separator or scheme.lower() != "bearer" or not token:
        return None
    return f"Bearer {token}"


def _upstream_headers(request: Request) -> dict[str, str]:
    headers = {
        "X-Reachy-Mini-Realtime-URL": "1",
        # An explicit empty value also prevents httpx from substituting its
        # own User-Agent when the original request did not provide one.
        "User-Agent": request.headers.get("user-agent", ""),
    }
    forwarded_headers = {
        "Content-Type": request.headers.get("content-type"),
        "Accept": request.headers.get("accept"),
        "Authorization": _bearer_authorization(request.headers.get("authorization")),
        "X-Reachy-Mini-Authorization": _bearer_authorization(
            request.headers.get("x-reachy-mini-authorization")
        ),
        "X-Forwarded-For": request.headers.get("x-forwarded-for"),
    }
    headers.update({name: value for name, value in forwarded_headers.items() if value})
    return headers


def _not_configured_response() -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": "upstream_session_url_not_configured",
            "expected_env": UPSTREAM_ENV_NAME,
        },
        status_code=503,
        headers=_no_store_headers(),
    )


def _invalid_config_response(exc: ValueError) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": "invalid_upstream_session_url",
            "detail": str(exc),
        },
        status_code=503,
        headers=_no_store_headers(),
    )


@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse(
        {
            "service": SERVICE_NAME,
            "health": "/health",
            "ready": "/ready",
            "session": "/session",
            "session_url": "/session-url",
        },
        headers=_no_store_headers(),
    )


@app.get("/health")
async def health() -> JSONResponse:
    try:
        configured = _upstream_config() is not None
    except ValueError:
        configured = False
    return JSONResponse({"ok": True, "service": SERVICE_NAME, "configured": configured}, headers=_no_store_headers())


@app.get("/ready")
async def ready() -> JSONResponse:
    try:
        config = _upstream_config()
    except ValueError as exc:
        return _invalid_config_response(exc)
    if config is None:
        return _not_configured_response()
    return JSONResponse({"ok": True, "configured": True}, headers=_no_store_headers())


@app.get("/session-url")
async def session_url() -> JSONResponse:
    try:
        config = _upstream_config()
    except ValueError as exc:
        return _invalid_config_response(exc)
    if config is None:
        return _not_configured_response()
    return JSONResponse({"session_url": random.choice(config.session_urls)}, headers=_no_store_headers())


@app.get("/config")
async def config() -> JSONResponse:
    return await session_url()


@app.post("/session")
async def create_session(request: Request) -> Response:
    try:
        config = _upstream_config()
    except ValueError as exc:
        return _invalid_config_response(exc)
    if config is None:
        return _not_configured_response()

    body = await request.body()
    headers = _upstream_headers(request)
    selected_url = random.choice(config.session_urls)
    backend = "A" if selected_url == config.session_urls[0] else "B"
    started_at = time.perf_counter()

    try:
        async with httpx.AsyncClient(timeout=config.timeout_s) as client:
            upstream_response = await client.post(selected_url, content=body, headers=headers)
    except httpx.TimeoutException:
        logger.warning("Session allocation backend=%s timed out", backend)
        return JSONResponse(
            {"ok": False, "error": "upstream_timeout"},
            status_code=504,
            headers={**_no_store_headers(), "X-Reachy-Mini-Backend": backend},
        )
    except httpx.HTTPError as exc:
        logger.warning("Session allocation backend=%s failed: %s", backend, exc)
        return JSONResponse(
            {"ok": False, "error": "upstream_unreachable"},
            status_code=502,
            headers={**_no_store_headers(), "X-Reachy-Mini-Backend": backend},
        )

    logger.info(
        "Session allocation backend=%s status=%s elapsed_ms=%.0f",
        backend, upstream_response.status_code, (time.perf_counter() - started_at) * 1000,
    )
    response = _proxy_response(upstream_response, session_url=selected_url)
    response.headers["X-Reachy-Mini-Backend"] = backend
    return response
