import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

import app as proxy_app


URL_A = "https://allocator-a.example/session"
URL_B = "https://allocator-b.example/session"


class ABRoutingTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            proxy_app.UPSTREAM_ENV_NAME: URL_A,
            proxy_app.UPSTREAM_B_ENV_NAME: URL_B,
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.upstream = AsyncMock()
        client_class = patch.object(proxy_app.httpx, "AsyncClient")
        client_class.start().return_value.__aenter__.return_value = self.upstream
        self.addCleanup(client_class.stop)
        self.client = TestClient(proxy_app.app)
        self.addCleanup(self.client.close)

    def test_each_session_selects_from_both_backends(self):
        payload = {"hardware_id": "robot-123"}
        headers = {"X-Reachy-Mini-Authorization": "Bearer test-token"}
        with patch.object(proxy_app.random, "choice", side_effect=[URL_A, URL_B]) as choose:
            for selected_url, backend in ((URL_A, "A"), (URL_B, "B")):
                with self.subTest(backend=backend):
                    upstream_body = {"connect_url": selected_url.replace("/session", "/v1/realtime")}
                    self.upstream.post.return_value = httpx.Response(200, json=upstream_body)
                    response = self.client.post("/session", json=payload, headers=headers)

                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), upstream_body)
                    self.assertEqual(response.headers["x-reachy-mini-backend"], backend)
                    arguments = self.upstream.post.call_args
                    self.assertEqual(arguments.args, (selected_url,))
                    self.assertEqual(arguments.kwargs["content"], b'{"hardware_id":"robot-123"}')
                    self.assertEqual(arguments.kwargs["headers"]["X-Reachy-Mini-Authorization"], "Bearer test-token")
            self.assertEqual(choose.call_count, 2)
            choose.assert_called_with((URL_A, URL_B))

    def test_missing_second_backend_keeps_primary_routing(self):
        os.environ[proxy_app.UPSTREAM_B_ENV_NAME] = ""
        self.upstream.post.return_value = httpx.Response(200, json={})

        response = self.client.post("/session", json={})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-reachy-mini-backend"], "A")
        self.assertEqual(self.upstream.post.call_args.args, (URL_A,))

    def test_readiness_and_allocation_reject_invalid_second_backend(self):
        os.environ[proxy_app.UPSTREAM_B_ENV_NAME] = "not-a-url"
        for path in ("/ready", "/session-url", "/config", "/session"):
            with self.subTest(path=path):
                response = self.client.post(path, json={}) if path == "/session" else self.client.get(path)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["error"], "invalid_upstream_session_url")
        self.assertFalse(self.client.get("/health").json()["configured"])
        self.upstream.post.assert_not_called()

    def test_session_url_routes_select_a_backend_without_allocating(self):
        with patch.object(proxy_app.random, "choice", return_value=URL_B) as choose:
            for path in ("/session-url", "/config"):
                response = self.client.get(path)
                self.assertEqual(response.json(), {"session_url": URL_B})
            choose.assert_called_with((URL_A, URL_B))
        self.upstream.post.assert_not_called()

    def test_timeout_does_not_retry_other_backend(self):
        self.upstream.post.side_effect = httpx.ReadTimeout("upstream timed out")
        with patch.object(proxy_app.random, "choice", return_value=URL_B):
            response = self.client.post("/session", json={})

        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.headers["x-reachy-mini-backend"], "B")
        self.upstream.post.assert_awaited_once()

    def test_selected_backend_resolves_relative_redirects(self):
        self.upstream.post.return_value = httpx.Response(307, headers={"Location": "/next"})
        with patch.object(proxy_app.random, "choice", return_value=URL_B):
            response = self.client.post("/session", json={}, follow_redirects=False)

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "https://allocator-b.example/next")
        self.assertEqual(response.headers["x-reachy-mini-backend"], "B")
