---
title: Reachy Mini Realtime URL
sdk: docker
app_port: 7860
pinned: false
---

# Reachy Mini Realtime URL

Small FastAPI service that gives the Reachy Mini conversation app a stable
session endpoint.

## Configuration

Set this Space variable/secret to the current session allocator URL:

- `UPSTREAM_SESSION_URL`

The service validates that the configured value is an absolute `http` or
`https` URL.

Optional:

- `REQUEST_TIMEOUT_SECONDS`: upstream request timeout, default `10`
- `LOG_LEVEL`: Python logging level, default `INFO`

## Routes

- `GET /health`: process health; does not fail if the upstream URL is missing
- `GET /ready`: readiness; fails if the upstream URL is missing or invalid
- `GET /session-url`: returns the currently configured upstream allocator URL
- `GET /config`: alias for `/session-url`
- `POST /session`: proxies the session allocation request to the upstream URL,
  preserving the incoming `User-Agent` and including valid bearer
  `Authorization`, `X-Reachy-Mini-Authorization`, and `X-Forwarded-For` headers
  when present

## Session response headers

The proxy preserves the upstream status and body, plus `Content-Type`,
`Retry-After`, `WWW-Authenticate`, `Authentication-Info`, `Location`, and `Allow`.
Repeated authentication challenges remain separate fields. Relative `Location`
values are resolved against the upstream session URL.

It also forwards `Request-ID`, `X-Request-ID`, `X-Correlation-ID`,
`X-Amzn-RequestId`, `X-Amzn-Trace-Id`, `RateLimit`, and headers beginning with
`RateLimit-` or `X-RateLimit-`, when present.

Headers named by `Connection` are excluded. Other upstream headers, including
connection controls, cookies, and CORS policy, are not copied. Responses retain
`Cache-Control: no-store`. Since HTTPX decompresses the upstream body, the proxy
recomputes `Content-Length` and does not forward upstream `Content-Encoding`.
