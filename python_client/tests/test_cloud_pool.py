"""Keep-alive HTTPS pool used for the IoT gateway (no network)."""
import http.client
import json
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aigosmart import (
    cloud,  # noqa: E402
    const,  # noqa: E402
)


class FakeResponse:
    def __init__(self, status=200, body=b'{"code":200,"data":{}}', will_close=False):
        self.status = status
        self._body = body
        self.will_close = will_close

    def read(self):
        return self._body


class FakeConn:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.closed = False

    def request(self, method, target, body=None, headers=None):
        self.requests.append((method, target, body))

    def getresponse(self):
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.closed = True


class FakePool(cloud.HttpsPool):
    def __init__(self, conns):
        super().__init__()
        self.conns = list(conns)
        self.opened = 0

    def _connect(self, host):
        self.opened += 1
        return self.conns.pop(0)


class TestHttpsPool(unittest.TestCase):
    url = "https://example.invalid/thing/properties/get"

    def test_connection_is_reused(self):
        conn = FakeConn([FakeResponse(), FakeResponse()])
        pool = FakePool([conn])
        pool.post(self.url, b"{}", {})
        pool.post(self.url, b"{}", {})
        self.assertEqual(pool.opened, 1)
        self.assertEqual(len(conn.requests), 2)
        self.assertEqual(conn.requests[0][1], "/thing/properties/get")

    def test_idle_connection_is_not_reused(self):
        conn1 = FakeConn([FakeResponse()])
        conn2 = FakeConn([FakeResponse()])
        pool = FakePool([conn1, conn2])
        with mock.patch.object(cloud.time, "monotonic", side_effect=[0.0, 0.0, 100.0, 100.0]):
            pool.post(self.url, b"{}", {})
            pool.post(self.url, b"{}", {})
        self.assertEqual(pool.opened, 2)
        self.assertTrue(conn1.closed)

    def test_server_close_is_honoured(self):
        conn1 = FakeConn([FakeResponse(will_close=True)])
        conn2 = FakeConn([FakeResponse()])
        pool = FakePool([conn1, conn2])
        pool.post(self.url, b"{}", {})
        pool.post(self.url, b"{}", {})
        self.assertTrue(conn1.closed)
        self.assertEqual(pool.opened, 2)

    def test_stale_connection_retried_for_reads(self):
        conn1 = FakeConn([FakeResponse(), http.client.RemoteDisconnected("gone")])
        conn2 = FakeConn([FakeResponse(body=b'{"code":200,"n":2}')])
        pool = FakePool([conn1, conn2])
        pool.post(self.url, b"{}", {})
        status, body = pool.post(self.url, b"{}", {}, retry_stale=True)
        self.assertEqual((status, json.loads(body)["n"]), (200, 2))
        self.assertTrue(conn1.closed)

    def test_writes_are_never_resent(self):
        conn1 = FakeConn([FakeResponse(), http.client.RemoteDisconnected("gone")])
        pool = FakePool([conn1])
        pool.post(self.url, b"{}", {})
        with self.assertRaises(http.client.RemoteDisconnected):
            pool.post(self.url, b"{}", {}, retry_stale=False)
        self.assertEqual(pool.opened, 1)

    def test_fresh_connection_failure_is_not_retried(self):
        conn1 = FakeConn([ConnectionResetError("reset")])
        pool = FakePool([conn1])
        with self.assertRaises(ConnectionResetError):
            pool.post(self.url, b"{}", {})
        self.assertEqual(pool.opened, 1)

    def test_thread_safety(self):
        conns = [FakeConn([FakeResponse() for _ in range(50)]) for _ in range(8)]
        pool = FakePool(conns)
        errors = []

        def worker():
            try:
                for _ in range(10):
                    pool.post(self.url, b"{}", {})
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertLessEqual(pool.opened, 4)


class TestGatewayCall(unittest.TestCase):
    def setUp(self):
        self.client = cloud.AigoCloudClient()
        self.client._pool = mock.Mock()

    def test_reads_may_retry_writes_may_not(self):
        self.client._pool.post.return_value = (200, b'{"code":200,"data":{}}')
        self.client._call(const.PATH_PROPS_GET, {"iotId": "x"})
        self.assertTrue(self.client._pool.post.call_args.kwargs["retry_stale"])
        self.client._call(const.PATH_PROPS_SET, {"iotId": "x", "items": {}})
        self.assertFalse(self.client._pool.post.call_args.kwargs["retry_stale"])

    def test_error_answers(self):
        self.client._pool.post.return_value = (502, b"<html>bad gateway</html>")
        with self.assertRaises(cloud.AigoApiError) as ctx:
            self.client._call(const.PATH_PROPS_GET, {})
        self.assertIn("HTTP 502", str(ctx.exception))
        self.client._pool.post.return_value = (200, b'{"code":29003,"message":"identityId is blank"}')
        with self.assertRaises(cloud.AigoApiError) as ctx:
            self.client._call(const.PATH_PROPS_GET, {})
        self.assertEqual(ctx.exception.response["code"], 29003)

    def test_single_refresh_under_concurrency(self):
        self.client.iot_token = "t"
        self.client.token_created = 0           # long expired
        calls = []

        def refresh():
            calls.append(1)
            self.client.token_created = cloud.time.time()

        self.client.refresh = refresh
        threads = [threading.Thread(target=self.client.ensure_token) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
