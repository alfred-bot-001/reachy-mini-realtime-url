import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import Request

import app as proxy_app


class FakeUpstreamResponse:
    content = b'{"session_id":"session-1"}'
    status_code = 200
    headers = httpx.Headers({"content-type": "application/json"})


class FakeAsyncClient:
    posts: list[dict[str, object]] = []

    def __init__(self, *, timeout: float):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, url: str, *, content: bytes, headers: dict[str, str]):
        self.posts.append(
            {
                "url": url,
                "content": content,
                "headers": headers,
                "timeout": self.timeout,
            }
        )
        return FakeUpstreamResponse()


def request_with_headers(headers: dict[str, str]) -> Request:
    encoded_headers = [
        (name.lower().encode("ascii"), value.encode("ascii"))
        for name, value in headers.items()
    ]
    body_sent = False

    async def receive():
        nonlocal body_sent
        if body_sent:
            return {"type": "http.disconnect"}
        body_sent = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/session",
            "headers": encoded_headers,
        },
        receive,
    )


class SessionProxyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeAsyncClient.posts = []

    async def proxy(self, headers: dict[str, str]) -> dict[str, object]:
        request = request_with_headers(headers)
        with (
            patch.dict(
                os.environ,
                {proxy_app.UPSTREAM_ENV_NAME: "https://allocator.example/session"},
            ),
            patch.object(proxy_app.httpx, "AsyncClient", FakeAsyncClient),
        ):
            response = await proxy_app.create_session(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(FakeAsyncClient.posts), 1)
        return FakeAsyncClient.posts[0]

    async def test_forwards_bearer_authorization_to_upstream(self):
        post = await self.proxy(
            {
                "Authorization": "Bearer hf_user_token",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

        self.assertEqual(post["headers"]["Authorization"], "Bearer hf_user_token")
        self.assertEqual(post["headers"]["Content-Type"], "application/json")
        self.assertEqual(post["headers"]["Accept"], "application/json")
        self.assertEqual(post["headers"]["X-Reachy-Mini-Realtime-URL"], "1")

    async def test_forwards_reachy_authorization_to_upstream(self):
        post = await self.proxy(
            {"X-Reachy-Mini-Authorization": "Bearer hf_user_token"}
        )

        self.assertEqual(
            post["headers"]["X-Reachy-Mini-Authorization"],
            "Bearer hf_user_token",
        )

    async def test_omits_authorization_when_request_has_no_token(self):
        post = await self.proxy({})

        self.assertNotIn("Authorization", post["headers"])
        self.assertNotIn("X-Reachy-Mini-Authorization", post["headers"])
        self.assertNotIn("X-Forwarded-For", post["headers"])

    async def test_does_not_forward_non_bearer_authorization(self):
        post = await self.proxy({"Authorization": "Basic credentials"})

        self.assertNotIn("Authorization", post["headers"])

    async def test_forwards_client_ip_chain_to_upstream(self):
        post = await self.proxy(
            {"X-Forwarded-For": "203.0.113.8, 10.0.0.4"}
        )

        self.assertEqual(
            post["headers"]["X-Forwarded-For"],
            "203.0.113.8, 10.0.0.4",
        )

    async def test_forwards_original_user_agent_to_upstream(self):
        post = await self.proxy({"User-Agent": "ReachyMini/1.2.3"})

        self.assertEqual(post["headers"]["User-Agent"], "ReachyMini/1.2.3")

    async def test_uses_empty_user_agent_when_original_is_missing(self):
        post = await self.proxy({})

        self.assertEqual(post["headers"]["User-Agent"], "")

    async def test_preserves_upstream_retry_after(self):
        for status_code, retry_after in (
            (429, "900"),
            (429, "Sat, 05 Sep 2026 12:00:00 GMT"),
            (503, "60"),
            (429, None),
            (200, None),
        ):
            with self.subTest(status_code=status_code, retry_after=retry_after):
                upstream = httpx.Response(
                    status_code,
                    json={"detail": {"retry_after_s": 900}},
                    headers={"Retry-After": retry_after} if retry_after is not None else {},
                )
                with (
                    patch.dict(
                        os.environ,
                        {proxy_app.UPSTREAM_ENV_NAME: "https://allocator.example/session"},
                    ),
                    patch.object(proxy_app.httpx, "AsyncClient", FakeAsyncClient),
                    patch.object(FakeAsyncClient, "post", AsyncMock(return_value=upstream)),
                ):
                    response = await proxy_app.create_session(request_with_headers({}))

                self.assertEqual(response.status_code, status_code)
                self.assertEqual(response.body, upstream.content)
                self.assertEqual(response.headers["content-type"], "application/json")
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers.get("retry-after"), retry_after)
