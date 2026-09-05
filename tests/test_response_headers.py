import gzip
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

import app as proxy_app


class ResponseHeaderTests(unittest.TestCase):
    def proxy(self, upstream: httpx.Response) -> httpx.Response:
        upstream_client = AsyncMock()
        upstream_client.post.return_value = upstream
        with (
            patch.dict(
                os.environ,
                {proxy_app.UPSTREAM_ENV_NAME: "https://allocator.example/api/session"},
            ),
            patch.object(proxy_app.httpx, "AsyncClient") as client_class,
        ):
            client_class.return_value.__aenter__.return_value = upstream_client
            with TestClient(proxy_app.app) as client:
                return client.post("/session", json={}, follow_redirects=False)

    def test_preserves_multiple_authentication_challenges(self):
        challenges = ['Bearer realm="sessions"', 'Basic realm="legacy"']
        response = self.proxy(httpx.Response(
            401,
            json={"detail": "authentication required"},
            headers=[("WWW-Authenticate", value) for value in challenges],
        ))

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get_list("www-authenticate"), challenges)
        self.assertEqual(response.json(), {"detail": "authentication required"})

    def test_preserves_authentication_info(self):
        response = self.proxy(httpx.Response(
            200, json={}, headers={"Authentication-Info": 'nextnonce="next"'},
        ))

        self.assertEqual(response.headers.get("authentication-info"), 'nextnonce="next"')

    def test_preserves_redirects_and_resolves_relative_locations(self):
        for location, expected in (
            ("https://allocator.example/v2/session", "https://allocator.example/v2/session"),
            ("/v2/session", "https://allocator.example/v2/session"),
            ("next?ticket=123", "https://allocator.example/api/next?ticket=123"),
        ):
            with self.subTest(location=location):
                response = self.proxy(httpx.Response(307, headers={"Location": location}))

                self.assertEqual(response.status_code, 307)
                self.assertEqual(response.headers.get("location"), expected)

    def test_preserves_allowed_methods(self):
        response = self.proxy(httpx.Response(405, headers={"Allow": "POST, OPTIONS"}))

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.headers.get("allow"), "POST, OPTIONS")

    def test_preserves_request_ids_and_rate_limit_metadata(self):
        headers = {
            "X-Request-ID": "request-123",
            "X-Correlation-ID": "correlation-456",
            "X-Amzn-Trace-Id": "Root=trace-789",
            "Retry-After": "60",
            "RateLimit": '"session";r=0;t=60',
            "RateLimit-Policy": '"session";q=20;w=60',
            "RateLimit-Remaining": "0",
            "X-RateLimit-Limit": "20",
            "X-RateLimit-Reset": "1788595200",
        }
        response = self.proxy(httpx.Response(429, json={"detail": "limited"}, headers=headers))

        self.assertEqual(response.status_code, 429)
        for name, value in headers.items():
            self.assertEqual(response.headers.get(name), value, name)

    def test_compressed_response_has_correct_body_and_framing(self):
        body = b'{"detail":"upstream error"}'
        compressed = gzip.compress(body)
        upstream = httpx.Response(503, content=compressed, headers={
            "Content-Encoding": "gzip",
            "Content-Length": str(len(compressed)),
            "Content-Type": "application/problem+json",
            "Retry-After": "30",
        })
        response = self.proxy(upstream)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.content, body)
        self.assertEqual(response.headers["content-length"], str(len(body)))
        self.assertEqual(response.headers["content-type"], "application/problem+json")
        self.assertEqual(response.headers["retry-after"], "30")
        self.assertNotIn("content-encoding", response.headers)

    def test_filters_connection_fields_and_keeps_local_response_policy(self):
        response = self.proxy(httpx.Response(429, json={}, headers={
            "Connection": "X-Request-ID, rEtRy-AfTeR",
            "X-Request-ID": "connection-only",
            "Retry-After": "900",
            "Keep-Alive": "timeout=5",
            "Transfer-Encoding": "chunked",
            "Trailer": "X-Checksum",
            "Upgrade": "websocket",
            "Proxy-Authenticate": "Basic",
            "Cache-Control": "public, max-age=3600",
            "Set-Cookie": "upstream_cookie=value",
            "Access-Control-Allow-Origin": "*",
            "WWW-Authenticate": "Bearer",
        }))

        self.assertEqual(response.headers.get("www-authenticate"), "Bearer")
        self.assertEqual(response.headers["cache-control"], "no-store")
        for name in (
            "connection", "x-request-id", "retry-after", "keep-alive",
            "transfer-encoding", "trailer", "upgrade", "proxy-authenticate",
            "set-cookie", "access-control-allow-origin",
        ):
            self.assertNotIn(name, response.headers, name)

    def test_missing_optional_headers_are_not_invented(self):
        response = self.proxy(httpx.Response(200, content=b"{}"))

        self.assertEqual(response.content, b"{}")
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(response.headers["content-length"], "2")
        self.assertEqual(response.headers["cache-control"], "no-store")
        for name in ("retry-after", "www-authenticate", "location", "x-request-id"):
            self.assertNotIn(name, response.headers)
