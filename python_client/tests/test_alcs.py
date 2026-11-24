"""Local ALCS client against a fake device built from Alibaba's device SDK.

The fake device mirrors iotkit-embedded v3 (src/dev_model/alcs/alcs_server.c
and alcs_api.c): it verifies the auth sign, derives the session key on its
own side, decrypts requests with the SDK's AES scheme and answers encrypted.
"""
import base64
import hashlib
import hmac
import json
import os
import socket
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aigosmart import local  # noqa: E402
from aigosmart.local import (  # noqa: E402
    COAP_ACK,
    COAP_CONFIRMABLE,
    CODE_POST,
    OPTION_SESSION_ID,
    AlcsDevice,
    AlcsError,
    alcs_decrypt,
    alcs_encrypt,
    build_coap,
    parse_coap,
    parse_discovery,
)

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

PK, DN = "a163VQp0e0e", "fountain01"
ACCESS_KEY, ACCESS_TOKEN = "abcdefgh-123", "c2VjcmV0LXRva2VuLWZyb20tY2xvdWQ="


def _raw_cbc(key, data, encrypt):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    c = Cipher(algorithms.AES(key), modes.CBC(b"a1b1c1d1e1f1g1h1"))
    ctx = c.encryptor() if encrypt else c.decryptor()
    return ctx.update(data) + ctx.finalize()


class TestCoapHeader(unittest.TestCase):
    def test_header_byte_is_version_type_tkl(self):
        pkt = build_coap(COAP_CONFIRMABLE, CODE_POST, 0x1234, b"\x01\x02\x03\x04",
                         "/dev/core/service/dev", b"{}")
        # Ver=1, Type=CON(0), TKL=4  ->  0b01_00_0100
        self.assertEqual(pkt[0], 0x44)
        self.assertEqual(pkt[1], CODE_POST)
        self.assertEqual(pkt[2:4], b"\x12\x34")
        self.assertEqual(pkt[4:8], b"\x01\x02\x03\x04")
        msg = parse_coap(pkt)
        self.assertEqual(msg["type"], COAP_CONFIRMABLE)
        self.assertEqual(msg["token"], b"\x01\x02\x03\x04")
        self.assertEqual(msg["payload"], b"{}")

    def test_options_sorted_with_session_id(self):
        pkt = build_coap(COAP_CONFIRMABLE, CODE_POST, 1, b"tk",
                         "/sys/pk/dn/thing/service/property/get", b"x",
                         options=[(OPTION_SESSION_ID, local.uint_option(300))],
                         content_format=local.CONTENT_OCTET_STREAM)
        opts = parse_coap(pkt)["options"]
        numbers = [n for n, _ in opts]
        self.assertEqual(numbers, sorted(numbers))
        self.assertEqual([v for n, v in opts if n == 11],
                         [b"sys", b"pk", b"dn", b"thing", b"service", b"property", b"get"])
        self.assertEqual(dict(opts)[12], bytes([42]))
        self.assertEqual(dict(opts)[OPTION_SESSION_ID], (300).to_bytes(2, "big"))

    def test_rejects_non_coap(self):
        with self.assertRaises(ValueError):
            parse_coap(b"\x00\x02\x00\x01")       # version 0

    def test_uint_option(self):
        self.assertEqual(local.uint_option(0), b"")
        self.assertEqual(local.uint_option(42), b"\x2a")
        self.assertEqual(local.uint_option(256), b"\x01\x00")


@unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed")
class TestCrypto(unittest.TestCase):
    key = b"0123456789abcdef"

    def test_roundtrip_all_lengths(self):
        for n in range(0, 70):
            data = os.urandom(n)
            enc = alcs_encrypt(data, self.key)
            self.assertEqual(len(enc) % 16, 0)
            self.assertEqual(len(enc), (n & ~15) + 16)
            self.assertEqual(alcs_decrypt(enc, self.key), data)

    def test_sdk_block_scheme(self):
        # full blocks chained with the IV; the padded tail encrypted alone
        data = b"A" * 20
        enc = alcs_encrypt(data, self.key)
        self.assertEqual(enc[:16], _raw_cbc(self.key, data[:16], True))
        tail = data[16:] + bytes([12]) * 12
        self.assertEqual(enc[16:], _raw_cbc(self.key, tail, True))
        # exact multiple of 16: a whole pad block, also encrypted alone
        enc = alcs_encrypt(b"B" * 16, self.key)
        self.assertEqual(enc[16:], _raw_cbc(self.key, bytes([16]) * 16, True))

    def test_bad_length(self):
        with self.assertRaises(ValueError):
            alcs_decrypt(b"short", self.key)

    def test_session_key_and_sign(self):
        sign = local.auth_sign(ACCESS_TOKEN, "R" * 16)
        ref = base64.b64encode(hmac.new(ACCESS_TOKEN.encode(), b"R" * 16,
                                        hashlib.sha1).digest()).decode()
        self.assertEqual(sign, ref)
        key = local.session_key(ACCESS_TOKEN, "clientR", "deviceR")
        ref = hmac.new(ACCESS_TOKEN.encode(), b"clientRdeviceR",
                       hashlib.sha1).hexdigest()[:16].encode()
        self.assertEqual(key, ref)
        self.assertEqual(len(key), 16)


class TestDiscoveryParse(unittest.TestCase):
    def test_sdk_v3_answer(self):
        body = {"id": "1", "code": 200, "data": {"devices": {
            "addr": "0.0.0.0", "port": 5683, "pal": "linkkit-ica",
            "profile": [{"productKey": PK, "deviceName": DN}]}}}
        devs = parse_discovery(body, "192.168.1.50", 5683)
        self.assertEqual(len(devs), 1)
        self.assertEqual((devs[0].addr, devs[0].port), ("192.168.1.50", 5683))
        self.assertEqual(devs[0].dev_id, PK + DN)

    def test_flat_answer(self):
        body = {"params": {"productKey": PK, "deviceName": DN, "port": "5683"}}
        devs = parse_discovery(body, "10.0.0.2", 1234)
        self.assertEqual(devs[0].port, 5683)
        self.assertEqual(devs[0].addr, "10.0.0.2")

    def test_irrelevant_answer(self):
        self.assertEqual(parse_discovery({"code": 200, "data": {}}, "1.2.3.4", 1), [])

    def test_lan_cache(self):
        dev = parse_discovery({"data": {"devices": {"profile": [
            {"productKey": "pkx", "deviceName": "dnx"}]}}}, "10.1.1.1", 5683)[0]
        local.remember_lan_device(dev)
        self.assertEqual(local.known_lan_address("pkx", "dnx"), ("10.1.1.1", 5683))
        local.forget_lan_device("pkx", "dnx")
        self.assertIsNone(local.known_lan_address("pkx", "dnx"))


class FakeAlcsDevice:
    """UDP CoAP server behaving like the SDK's ALCS device side."""

    def __init__(self, props, refuse_sign=False, separate_response=False):
        self.props = dict(props)
        self.refuse_sign = refuse_sign
        self.separate_response = separate_response
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.sessions = {}            # session id -> AES key
        self.writes = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        self._thread.join(2)
        self.sock.close()

    def _reply(self, req, src, payload, code=69, content_format=None):
        token = req["token"]
        if self.separate_response:
            empty_ack = bytes([(1 << 6) | (COAP_ACK << 4), 0]) + req["msg_id"].to_bytes(2, "big")
            self.sock.sendto(empty_ack, src)
            pkt = build_coap(COAP_CONFIRMABLE, code, (req["msg_id"] + 7) & 0xFFFF,
                             token, "", payload, content_format=content_format)
        else:
            pkt = build_coap(COAP_ACK, code, req["msg_id"], token, "", payload,
                             content_format=content_format)
        self.sock.sendto(pkt, src)

    def _run(self):
        while not self._stop.is_set():
            try:
                data, src = self.sock.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            try:
                req = parse_coap(data)
            except ValueError:
                continue
            if req["code"] == 0:
                continue          # our ACK of a separate response
            path = "/" + "/".join(v.decode() for n, v in req["options"] if n == 11)
            if path == f"/dev/{PK}/{DN}/core/service/auth":
                self._auth(req, src)
            elif path.startswith(f"/sys/{PK}/{DN}/thing/service/property/"):
                self._property(req, src, path.rsplit("/", 1)[1])

    def _auth(self, req, src):
        body = json.loads(req["payload"])
        p = body["params"]
        token = ACCESS_TOKEN.encode()
        expected = base64.b64encode(hmac.new(token, p["randomKey"].encode(),
                                             hashlib.sha1).digest()).decode()
        if self.refuse_sign or p["sign"] != expected or p["accessKey"] != ACCESS_KEY \
                or p["prodKey"] != PK or p["deviceName"] != DN \
                or body["method"] != "core/service/auth":
            out = {"id": str(body["id"]), "code": 401, "data": {}}
            self._reply(req, src, json.dumps(out).encode())
            return
        dev_random = "DeViCeRaNdOm1234"
        sid = len(self.sessions) + 1
        session_key = hmac.new(token, (p["randomKey"] + dev_random).encode(),
                               hashlib.sha1).hexdigest()[:16].encode()
        self.sessions[sid] = session_key
        out = {"id": str(body["id"]), "code": 200, "data": {
            "sign": "x", "randomKey": dev_random, "sessionId": sid, "expire": 86400}}
        self._reply(req, src, json.dumps(out).encode())

    def _property(self, req, src, action):
        opts = dict(req["options"])
        sid = int.from_bytes(opts.get(OPTION_SESSION_ID, b""), "big")
        key = self.sessions.get(sid)
        if key is None or opts.get(12) != bytes([42]):
            self._reply(req, src, b"", code=129)      # 4.01 unauthorized
            return
        body = json.loads(alcs_decrypt(req["payload"], key))
        if action == "get":
            wanted = body["params"] or list(self.props)
            data = {k: self.props[k] for k in wanted if k in self.props}
        else:
            self.props.update(body["params"])
            self.writes.append(body["params"])
            data = {}
        out = json.dumps({"id": body["id"], "code": 200, "data": data}).encode()
        self._reply(req, src, alcs_encrypt(out, key), content_format=42)


@unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed")
class TestAlcsSession(unittest.TestCase):
    def make(self, **kw):
        fake = FakeAlcsDevice({"Switch": 1, "mode": 0, "WaterGear": 3}, **kw)
        self.addCleanup(fake.close)
        dev = AlcsDevice("127.0.0.1", fake.port, product_key=PK, device_name=DN,
                         access_key=ACCESS_KEY, access_token=ACCESS_TOKEN, timeout=1.0)
        return fake, dev

    def test_auth_get_set(self):
        fake, dev = self.make()
        self.assertEqual(dev.get_properties(["Switch", "WaterGear"]),
                         {"Switch": 1, "WaterGear": 3})
        self.assertTrue(dev.authenticated)
        dev.set_properties({"WaterGear": 1})
        self.assertEqual(fake.writes, [{"WaterGear": 1}])
        self.assertEqual(dev.get_properties(["WaterGear"]), {"WaterGear": 1})
        self.assertEqual(len(fake.sessions), 1)          # session re-used

    def test_separate_response(self):
        _fake, dev = self.make(separate_response=True)
        self.assertEqual(dev.get_properties(["mode"]), {"mode": 0})

    def test_reauth_after_device_drops_session(self):
        fake, dev = self.make()
        dev.get_properties(["mode"])
        fake.sessions.clear()                             # device rebooted
        self.assertEqual(dev.get_properties(["mode"]), {"mode": 0})
        self.assertEqual(len(fake.sessions), 1)

    def test_wrong_token_refused(self):
        _fake, dev = self.make()
        dev.access_token = "wrong"
        with self.assertRaises(AlcsError):
            dev.get_properties()

    def test_no_keys(self):
        dev = AlcsDevice("127.0.0.1", 9, product_key=PK, device_name=DN)
        with self.assertRaises(AlcsError):
            dev.auth()

    def test_timeout(self):
        sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sink.bind(("127.0.0.1", 0))
        self.addCleanup(sink.close)
        dev = AlcsDevice("127.0.0.1", sink.getsockname()[1], product_key=PK,
                         device_name=DN, access_key=ACCESS_KEY,
                         access_token=ACCESS_TOKEN, timeout=0.3)
        with self.assertRaises(TimeoutError) as ctx:
            dev.auth()
        self.assertNotIn("127.0.0.1", str(ctx.exception))  # no address in reports


@unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed")
class TestLocalFirst(unittest.TestCase):
    """LocalFirstMixin: local read when possible, cloud otherwise."""

    def setUp(self):
        sys.path.insert(0, os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", "..")))
        import importlib.util
        import types
        # load custom_components/aigosmart/local_first.py with its vendored lib
        base = "aigosmart_lf_test"
        pkg = types.ModuleType(base)
        pkg.__path__ = [os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", "..", "custom_components", "aigosmart"))]
        sys.modules[base] = pkg
        spec = importlib.util.spec_from_file_location(
            f"{base}.local_first", os.path.join(pkg.__path__[0], "local_first.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"{base}.local_first"] = mod
        spec.loader.exec_module(mod)
        self.lib_local = sys.modules[f"{base}.lib.local"]

        class Client(mod.LocalFirstMixin):
            def __init__(self):
                super().__init__()
                self.cloud_reads = 0

            def list_devices(self):
                return [{"iotId": "iot1", "productKey": PK, "deviceName": DN}]

            def get_alcs_access_info(self, ids):
                return [{"accessKey": ACCESS_KEY, "accessToken": ACCESS_TOKEN}]

            def get_properties(self, iot_id):
                self.cloud_reads += 1
                return {"Switch": 0, "source": "cloud"}

        self.client = Client()
        self.lib_local.forget_lan_device(PK, DN)

    def tearDown(self):
        self.lib_local.forget_lan_device(PK, DN)

    def test_cloud_when_not_on_lan(self):
        self.assertEqual(self.client.get_properties_prefer_local("iot1")["source"], "cloud")
        self.assertEqual(self.client.local_status["iot1"], "not seen on the LAN")

    def test_local_when_on_lan(self):
        fake = FakeAlcsDevice({"Switch": 1})
        self.addCleanup(fake.close)
        self.lib_local.remember_lan_device(
            self.lib_local.DiscoveredDevice("127.0.0.1", fake.port,
                                            {"productKey": PK, "deviceName": DN}))
        self.assertEqual(self.client.get_properties_prefer_local("iot1", ["Switch"]),
                         {"Switch": 1})
        self.assertEqual(self.client.cloud_reads, 0)
        self.assertEqual(self.client.local_status["iot1"], "ok")
        report = self.client.probe_local("iot1", ["Switch"], discover=False)
        self.assertEqual(report["result"], "ok")
        self.assertEqual(report["properties_read"], ["Switch"])
        self.assertNotIn("127.0.0.1", json.dumps(report))

    def test_fallback_and_backoff_on_refusal(self):
        fake = FakeAlcsDevice({"Switch": 1}, refuse_sign=True)
        self.addCleanup(fake.close)
        self.lib_local.remember_lan_device(
            self.lib_local.DiscoveredDevice("127.0.0.1", fake.port,
                                            {"productKey": PK, "deviceName": DN}))
        self.assertEqual(self.client.get_properties_prefer_local("iot1")["source"], "cloud")
        self.assertIn("auth", self.client.local_status["iot1"])
        # backed off: next read goes straight to the cloud
        self.client.get_properties_prefer_local("iot1")
        self.assertEqual(self.client.cloud_reads, 2)
        self.assertEqual(len(fake.sessions), 0)


if __name__ == "__main__":
    unittest.main()
