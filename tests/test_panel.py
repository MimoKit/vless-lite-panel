from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER_TEMPLATE = ROOT / "install.template.sh"
SPEC = importlib.util.spec_from_file_location("vless_lite_panel", ROOT / "panel.py")
assert SPEC is not None and SPEC.loader is not None
panel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(panel)


def sample_config(password: str = "test-password-123") -> dict:
    salt = bytes.fromhex("00112233445566778899aabbccddeeff")
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 240_000)
    return {
        "public_host": "203.0.113.7",
        "public_scheme": "http",
        "vpn_port": 443,
        "ui_port": 2053,
        "ui_bind": "127.0.0.1",
        "api_server": "127.0.0.1:10085",
        "uuid": "11111111-2222-4333-8444-555555555555",
        "email": "default@vless-lite",
        "sni": "www.microsoft.com",
        "public_key": "public-key-value",
        "short_id": "0011223344556677",
        "node_name": "Test Node",
        "subscription_token": "a" * 48,
        "admin_user": "admin",
        "admin_salt": salt.hex(),
        "admin_hash": digest.hex(),
        "xray_config": "/tmp/xray.json",
        "xray_bin": "/usr/local/bin/xray",
        "xray_service": "xray.service",
        "panel_service": "vless-lite-panel.service",
    }


class LinkTests(unittest.TestCase):
    def test_vless_link_contains_reality_parameters(self) -> None:
        link = panel.vless_link(sample_config())
        self.assertTrue(link.startswith("vless://11111111-2222-4333-8444-555555555555@203.0.113.7:443?"))
        self.assertIn("security=reality", link)
        self.assertIn("flow=xtls-rprx-vision", link)
        self.assertIn("pbk=public-key-value", link)
        self.assertTrue(link.endswith("#Test%20Node"))

    def test_ipv6_host_is_bracketed(self) -> None:
        config = sample_config()
        config["public_host"] = "2001:db8::1"
        self.assertIn("@[2001:db8::1]:443?", panel.vless_link(config))
        self.assertTrue(panel.subscription_url(config).startswith("http://[2001:db8::1]:2053/"))

    def test_clash_subscription_contains_reality_node(self) -> None:
        subscription = panel.clash_subscription(sample_config())
        self.assertIn('name: "Test Node"', subscription)
        self.assertIn("type: vless", subscription)
        self.assertIn('encryption: ""', subscription)
        self.assertIn("flow: xtls-rprx-vision", subscription)
        self.assertIn('public-key: "public-key-value"', subscription)
        self.assertIn('short-id: "0011223344556677"', subscription)
        self.assertIn("enhanced-mode: fake-ip", subscription)
        self.assertIn('https://1.1.1.1/dns-query#PROXY', subscription)
        self.assertIn("ipv6: false", subscription)
        self.assertIn("- MATCH,PROXY", subscription)


class InstallerTests(unittest.TestCase):
    def test_defaults_are_compatible_with_mihomo(self) -> None:
        template = INSTALLER_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn('"minClientVer": "0.0.0"', template)
        self.assertIn('sni="${sni:-www.apple.com}"', template)


class StatsTests(unittest.TestCase):
    def test_current_json_stats(self) -> None:
        payload = {
            "stat": [
                {"name": "inbound>>>api>>>traffic>>>uplink", "value": 999},
                {"name": "inbound>>>vless-reality>>>traffic>>>uplink", "value": 123},
                {"name": "inbound>>>vless-reality>>>traffic>>>downlink", "value": 456},
            ]
        }
        self.assertEqual(panel.parse_xray_stats(json.dumps(payload)), {"uplink": 123, "downlink": 456})

    def test_missing_values_are_zero(self) -> None:
        payload = {
            "stat": [
                {"name": "inbound>>>vless-reality>>>traffic>>>uplink"},
                {"name": "inbound>>>vless-reality>>>traffic>>>downlink"},
            ]
        }
        self.assertEqual(panel.parse_xray_stats(json.dumps(payload)), {"uplink": 0, "downlink": 0})

    def test_legacy_stats_output(self) -> None:
        output = '''stat: <
 name: "user>>>default@vless-lite>>>traffic>>>uplink"
 value: 10
>
stat: <
 name: "user>>>default@vless-lite>>>traffic>>>downlink"
 value: 20
>'''
        self.assertEqual(panel.parse_xray_stats(output), {"uplink": 10, "downlink": 20})


class HttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp.name) / "config.json"
        self.config_path.write_text(json.dumps(sample_config()), encoding="utf-8")
        self.original_path = panel.CONFIG_PATH
        panel.CONFIG_PATH = self.config_path
        self.server = panel.PanelServer(("127.0.0.1", 0), panel.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        panel.CONFIG_PATH = self.original_path
        self.temp.cleanup()

    def test_subscription_needs_only_secret_token(self) -> None:
        with urllib.request.urlopen(f"{self.base}/sub/{'a' * 48}", timeout=3) as response:
            self.assertEqual(response.headers.get_content_type(), "text/yaml")
            subscription = response.read().decode()
        self.assertEqual(subscription, panel.clash_subscription(sample_config()))

    def test_legacy_base64_subscription_remains_available(self) -> None:
        with urllib.request.urlopen(f"{self.base}/sub/base64/{'a' * 48}", timeout=3) as response:
            decoded = base64.b64decode(response.read()).decode()
        self.assertEqual(decoded.strip(), panel.vless_link(sample_config()))

    def test_dashboard_requires_basic_auth(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(f"{self.base}/", timeout=3)
        self.assertEqual(context.exception.code, 401)

        token = base64.b64encode(b"admin:test-password-123").decode()
        request = urllib.request.Request(f"{self.base}/", headers={"Authorization": f"Basic {token}"})
        with urllib.request.urlopen(request, timeout=3) as response:
            page = response.read().decode()
        self.assertIn("VLESS Lite", page)


if __name__ == "__main__":
    unittest.main()
