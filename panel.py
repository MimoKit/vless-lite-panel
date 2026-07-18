#!/usr/bin/env python3
"""Small VLESS Reality management panel using only the Python standard library."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


CONFIG_PATH = Path(os.environ.get("VLP_CONFIG", "/etc/vless-lite-panel/config.json"))
CONFIG_LOCK = threading.RLock()
STARTED_AT = time.monotonic()


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Invalid object in {path}")
    return value


def atomic_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_config() -> dict[str, Any]:
    with CONFIG_LOCK:
        return read_json(CONFIG_PATH)


def verify_password(password: str, salt_hex: str, expected_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 240_000)
    return hmac.compare_digest(actual, expected)


def host_for_uri(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def vless_link(config: dict[str, Any]) -> str:
    query = urllib.parse.urlencode(
        {
            "encryption": "none",
            "flow": "xtls-rprx-vision",
            "security": "reality",
            "sni": config["sni"],
            "fp": "chrome",
            "pbk": config["public_key"],
            "sid": config["short_id"],
            "type": "tcp",
            "headerType": "none",
        }
    )
    name = urllib.parse.quote(str(config.get("node_name", "VLESS Lite")), safe="")
    host = host_for_uri(str(config["public_host"]))
    return f"vless://{config['uuid']}@{host}:{config['vpn_port']}?{query}#{name}"


def panel_base_url(config: dict[str, Any]) -> str:
    scheme = str(config.get("public_scheme", "http"))
    host = host_for_uri(str(config["public_host"]))
    return f"{scheme}://{host}:{config['ui_port']}"


def subscription_url(config: dict[str, Any]) -> str:
    return f"{panel_base_url(config)}/sub/{config['subscription_token']}"


def legacy_subscription_url(config: dict[str, Any]) -> str:
    return f"{panel_base_url(config)}/sub/base64/{config['subscription_token']}"


def yaml_string(value: Any) -> str:
    """Return a JSON string, which is also a safely quoted YAML scalar."""
    return json.dumps(str(value), ensure_ascii=False)


def clash_subscription(config: dict[str, Any]) -> str:
    node_name = str(config.get("node_name", "VLESS Lite"))
    quoted_name = yaml_string(node_name)
    lines = [
        "# VLESS Lite subscription for Mihomo / Clash Meta",
        "mixed-port: 7890",
        "allow-lan: false",
        "mode: rule",
        "log-level: info",
        "ipv6: false",
        "profile:",
        "  store-selected: true",
        "  store-fake-ip: true",
        "dns:",
        "  enable: true",
        "  ipv6: false",
        "  enhanced-mode: fake-ip",
        "  fake-ip-range: 198.18.0.1/16",
        "  fake-ip-filter-mode: blacklist",
        "  fake-ip-filter:",
        '    - "*.lan"',
        '    - "*.local"',
        '    - "localhost"',
        "  default-nameserver:",
        "    - 223.5.5.5",
        "    - 119.29.29.29",
        "  nameserver:",
        '    - "https://1.1.1.1/dns-query#PROXY"',
        '    - "https://8.8.8.8/dns-query#PROXY"',
        "  proxy-server-nameserver:",
        "    - 223.5.5.5",
        "    - 119.29.29.29",
        "proxies:",
        f"  - name: {quoted_name}",
        "    type: vless",
        f"    server: {yaml_string(config['public_host'])}",
        f"    port: {int(config['vpn_port'])}",
        f"    uuid: {yaml_string(config['uuid'])}",
        '    encryption: ""',
        "    network: tcp",
        "    udp: true",
        "    tls: true",
        "    flow: xtls-rprx-vision",
        f"    servername: {yaml_string(config['sni'])}",
        "    client-fingerprint: chrome",
        "    reality-opts:",
        f"      public-key: {yaml_string(config['public_key'])}",
        f"      short-id: {yaml_string(config['short_id'])}",
        "proxy-groups:",
        "  - name: PROXY",
        "    type: select",
        "    proxies:",
        f"      - {quoted_name}",
        "      - DIRECT",
        "rules:",
        "  - MATCH,PROXY",
    ]
    return "\n".join(lines) + "\n"


def run_command(args: list[str], timeout: int = 10) -> tuple[int, str]:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    return result.returncode, output


def service_status(name: str) -> str:
    code, output = run_command(["systemctl", "is-active", name], timeout=5)
    return output.strip() if code == 0 else (output.strip() or "inactive")


def service_started(name: str) -> str:
    code, output = run_command(
        ["systemctl", "show", name, "--property=ActiveEnterTimestamp", "--value"],
        timeout=5,
    )
    return output.strip() if code == 0 and output.strip() else "-"


def parse_xray_stats(output: str) -> dict[str, int]:
    totals = {"uplink": 0, "downlink": 0}
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("stat"), list):
        for item in payload["stat"]:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = item.get("value", 0)
            if not isinstance(name, str) or not isinstance(value, int):
                continue
            if name == "inbound>>>vless-reality>>>traffic>>>uplink":
                totals["uplink"] = value
            elif name == "inbound>>>vless-reality>>>traffic>>>downlink":
                totals["downlink"] = value
        return totals

    # Compatibility with older Xray CLI output using protobuf text blocks.
    blocks = re.findall(r"stat:\s*<\s*(.*?)^\s*>\s*$", output, flags=re.S | re.M)
    for block in blocks:
        name_match = re.search(r'name:\s*"([^"]+)"', block)
        value_match = re.search(r"value:\s*(\d+)", block)
        if not name_match or not value_match:
            continue
        name = name_match.group(1)
        value = int(value_match.group(1))
        if name.endswith(">>>uplink"):
            totals["uplink"] += value
        elif name.endswith(">>>downlink"):
            totals["downlink"] += value
    return totals


def xray_stats(config: dict[str, Any], reset: bool = False) -> dict[str, Any]:
    command = [
        str(config.get("xray_bin", "/usr/local/bin/xray")),
        "api",
        "statsquery",
        f"--server={config.get('api_server', '127.0.0.1:10085')}",
    ]
    if reset:
        command.append("-reset")
    code, output = run_command(command, timeout=8)
    if code != 0:
        return {"uplink": 0, "downlink": 0, "available": False, "error": output}
    result: dict[str, Any] = parse_xray_stats(output)
    result["available"] = True
    return result


def current_connections(port: int) -> int:
    port_hex = f"{port:04X}"
    count = 0
    for filename in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(filename).read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4:
                continue
            local = fields[1]
            state = fields[3]
            if local.rsplit(":", 1)[-1].upper() == port_hex and state == "01":
                count += 1
    return count


def update_xray_client(new_uuid: str, new_short_id: str) -> None:
    config = load_config()
    xray_path = Path(str(config["xray_config"]))
    with CONFIG_LOCK:
        xray = read_json(xray_path)
        inbounds = xray.get("inbounds")
        if not isinstance(inbounds, list):
            raise ValueError("Xray inbounds are missing")
        found = False
        for inbound in inbounds:
            if not isinstance(inbound, dict) or inbound.get("tag") != "vless-reality":
                continue
            settings = inbound.get("settings")
            stream = inbound.get("streamSettings")
            if not isinstance(settings, dict) or not isinstance(stream, dict):
                raise ValueError("Invalid VLESS inbound")
            clients = settings.get("clients")
            reality = stream.get("realitySettings")
            if not isinstance(clients, list) or not clients or not isinstance(clients[0], dict):
                raise ValueError("VLESS client is missing")
            if not isinstance(reality, dict):
                raise ValueError("REALITY settings are missing")
            clients[0]["id"] = new_uuid
            reality["shortIds"] = [new_short_id]
            found = True
            break
        if not found:
            raise ValueError("Managed VLESS inbound was not found")

        old_xray = xray_path.read_bytes()
        atomic_json(xray_path, xray, mode=0o644)
        xray_bin = str(config.get("xray_bin", "/usr/local/bin/xray"))
        code, output = run_command([xray_bin, "run", "-test", "-config", str(xray_path)], timeout=10)
        if code != 0:
            xray_path.write_bytes(old_xray)
            os.chmod(xray_path, 0o644)
            raise RuntimeError(f"Xray config validation failed: {output}")

        config["uuid"] = new_uuid
        config["short_id"] = new_short_id
        config["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_json(CONFIG_PATH, config)

    code, output = run_command(["systemctl", "restart", str(config["xray_service"])], timeout=20)
    if code != 0:
        raise RuntimeError(f"Xray restart failed: {output}")


PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>VLESS Lite</title>
<style>
:root{--bg:#f4f6f8;--panel:#fff;--text:#17202a;--muted:#66727f;--line:#dce2e8;--blue:#1677ff;--green:#16875c;--red:#c83e4d;--amber:#9a6700;--shadow:0 6px 18px rgba(23,32,42,.08)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;letter-spacing:0}
header{background:#17202a;color:#fff;border-bottom:3px solid #20a06b}header .inner,main{width:min(1080px,calc(100% - 28px));margin:auto}.top{height:64px;display:flex;align-items:center;justify-content:space-between;gap:16px}.brand{font-size:20px;font-weight:750}.subtle{color:#b9c4ce;font-size:12px}.status{display:flex;align-items:center;gap:8px}.dot{width:10px;height:10px;border-radius:50%;background:#7d8790}.dot.online{background:#31c48d;box-shadow:0 0 0 4px rgba(49,196,141,.16)}
main{padding:22px 0 36px}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.metric,.section{background:var(--panel);border:1px solid var(--line);border-radius:6px;box-shadow:var(--shadow)}.metric{padding:16px;min-height:98px}.metric .label{color:var(--muted);font-size:12px}.metric .value{font-size:24px;font-weight:760;margin-top:8px;overflow-wrap:anywhere}.metric .unit{font-size:12px;color:var(--muted);font-weight:500}
.section{margin-top:14px;padding:18px}.section h2{font-size:16px;margin:0 0 14px}.row{display:grid;grid-template-columns:150px minmax(0,1fr) auto;gap:10px;align-items:center;padding:10px 0;border-top:1px solid var(--line)}.row:first-of-type{border-top:0}.row label{font-weight:650}.code{background:#eef2f5;border:1px solid var(--line);border-radius:5px;padding:10px 12px;min-width:0;overflow:auto;white-space:nowrap;font:12px/1.35 ui-monospace,SFMono-Regular,Consolas,monospace;color:#24303b}
button{border:1px solid var(--line);border-radius:5px;background:#fff;color:#17202a;height:36px;padding:0 14px;font-weight:650;cursor:pointer}button:hover{border-color:#91a1b0;background:#f7f9fa}button.primary{background:var(--blue);border-color:var(--blue);color:#fff}button.danger{background:#fff;border-color:#e5a7ae;color:var(--red)}button:disabled{opacity:.55;cursor:not-allowed}.actions{display:flex;gap:8px;flex-wrap:wrap}.notice{position:fixed;right:18px;bottom:18px;background:#17202a;color:#fff;border-radius:5px;padding:11px 14px;box-shadow:var(--shadow);display:none;max-width:min(420px,calc(100vw - 36px))}.notice.show{display:block}.notice.error{background:var(--red)}
@media(max-width:820px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.row{grid-template-columns:1fr}.row button{width:100%}.top{height:auto;padding:14px 0;align-items:flex-start}.status{margin-top:4px}}
@media(max-width:440px){.grid{grid-template-columns:1fr}.metric{min-height:82px}.metric .value{font-size:21px}}
@media(prefers-color-scheme:dark){:root{--bg:#11161b;--panel:#1a2229;--text:#edf2f5;--muted:#9facb7;--line:#34414b;--shadow:none}.code{background:#11171c;color:#dce7ee}button{background:#222c34;color:#edf2f5}button:hover{background:#293640}button.danger{background:#222c34}}
</style>
</head>
<body>
<header><div class="inner top"><div><div class="brand">VLESS Lite</div><div class="subtle" id="nodeName">Loading</div></div><div class="status"><span id="statusDot" class="dot"></span><span id="serviceStatus">Checking</span></div></div></header>
<main>
<div class="grid">
  <div class="metric"><div class="label">下载流量</div><div class="value" id="down">0 B</div><div class="unit" id="downSpeed">0 B/s</div></div>
  <div class="metric"><div class="label">上传流量</div><div class="value" id="up">0 B</div><div class="unit" id="upSpeed">0 B/s</div></div>
  <div class="metric"><div class="label">当前连接</div><div class="value" id="connections">0</div><div class="unit">TCP established</div></div>
  <div class="metric"><div class="label">面板请求</div><div class="value" id="requests">0</div><div class="unit" id="panelUptime">-</div></div>
</div>

<section class="section">
  <h2>连接信息</h2>
  <div class="row"><label>手机分享链接</label><div class="code" id="vlessLink">-</div><button onclick="copyValue('vlessLink')">复制</button></div>
  <div class="row"><label>Clash/Mihomo 订阅</label><div class="code" id="subLink">-</div><button onclick="copyValue('subLink')">复制</button></div>
  <div class="row"><label>节点地址</label><div class="code" id="endpoint">-</div><button onclick="copyValue('endpoint')">复制</button></div>
</section>

<section class="section">
  <h2>服务管理</h2>
  <div class="actions">
    <button class="primary" onclick="serviceAction('restart')">重启节点</button>
    <button onclick="serviceAction('start')">启动</button>
    <button class="danger" onclick="serviceAction('stop')">停止</button>
    <button onclick="resetStats()">清零流量</button>
    <button class="danger" onclick="rotateClient()">重新生成凭据</button>
  </div>
</section>
</main>
<div id="notice" class="notice"></div>
<script>
let previous=null;
function size(n){const u=['B','KB','MB','GB','TB'];let i=0,v=Number(n)||0;while(v>=1024&&i<u.length-1){v/=1024;i++}return `${v.toFixed(i?2:0)} ${u[i]}`}
function toast(text,error=false){const n=document.getElementById('notice');n.textContent=text;n.className='notice show'+(error?' error':'');setTimeout(()=>n.className='notice',3200)}
async function api(path,options={}){const r=await fetch(path,{cache:'no-store',...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});const d=await r.json();if(!r.ok||d.ok===false)throw new Error(d.error||`HTTP ${r.status}`);return d}
async function refresh(){try{const d=await api('/api/status');document.getElementById('nodeName').textContent=d.node_name;document.getElementById('serviceStatus').textContent=d.service_status;document.getElementById('statusDot').className='dot '+(d.service_status==='active'?'online':'');document.getElementById('down').textContent=size(d.downlink);document.getElementById('up').textContent=size(d.uplink);document.getElementById('connections').textContent=d.connections;document.getElementById('requests').textContent=d.requests;document.getElementById('panelUptime').textContent=`面板运行 ${d.panel_uptime}`;document.getElementById('vlessLink').textContent=d.vless_link;document.getElementById('subLink').textContent=d.subscription_url;document.getElementById('endpoint').textContent=d.endpoint;if(previous){const sec=Math.max(1,(Date.now()-previous.t)/1000);document.getElementById('downSpeed').textContent=`${size(Math.max(0,d.downlink-previous.down)/sec)}/s`;document.getElementById('upSpeed').textContent=`${size(Math.max(0,d.uplink-previous.up)/sec)}/s`}previous={t:Date.now(),down:d.downlink,up:d.uplink}}catch(e){toast(e.message,true)}}
async function copyValue(id){try{await navigator.clipboard.writeText(document.getElementById(id).textContent);toast('已复制')}catch(e){toast('复制失败',true)}}
async function serviceAction(action){try{await api('/api/service',{method:'POST',body:JSON.stringify({action})});toast('操作完成');setTimeout(refresh,900)}catch(e){toast(e.message,true)}}
async function resetStats(){if(!confirm('确认清零流量统计？'))return;try{await api('/api/reset-stats',{method:'POST',body:'{}'});previous=null;toast('流量已清零');refresh()}catch(e){toast(e.message,true)}}
async function rotateClient(){if(!confirm('旧链接将立即失效，确认重新生成？'))return;try{await api('/api/rotate',{method:'POST',body:'{}'});toast('新凭据已生成');setTimeout(refresh,1000)}catch(e){toast(e.message,true)}}
refresh();setInterval(refresh,3000);
</script>
</body>
</html>"""


def human_duration(seconds: int) -> str:
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    request_count = 0
    request_lock = threading.Lock()

    def increment_requests(self) -> int:
        with self.request_lock:
            self.request_count += 1
            return self.request_count


class Handler(BaseHTTPRequestHandler):
    server_version = "VLESSLite/1.0"

    def log_message(self, format_string: str, *args: Any) -> None:
        message = format_string % args
        print(f"{self.address_string()} {message}", flush=True)

    def send_bytes(
        self,
        body: bytes,
        content_type: str,
        status: int = HTTPStatus.OK,
        cache: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, value: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        self.send_bytes(
            json.dumps(value, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def unauthorized(self) -> None:
        body = b"Authentication required"
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="VLESS Lite"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authenticated(self) -> bool:
        config = load_config()
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(username, str(config["admin_user"])) and verify_password(
            password,
            str(config["admin_salt"]),
            str(config["admin_hash"]),
        )

    def require_auth(self) -> bool:
        if self.authenticated():
            return True
        self.unauthorized()
        return False

    def valid_post_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            origin_host = urllib.parse.urlsplit(origin).netloc
        except ValueError:
            return False
        return hmac.compare_digest(origin_host, self.headers.get("Host", ""))

    def read_body_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 4096:
            raise ValueError("Request body is too large")
        raw = self.rfile.read(length) if length else b"{}"
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON object required")
        return value

    def do_GET(self) -> None:
        self.server.increment_requests()  # type: ignore[attr-defined]
        config = load_config()
        path = urllib.parse.urlsplit(self.path).path

        sub_prefix = f"/sub/{config['subscription_token']}"
        legacy_sub_prefix = f"/sub/base64/{config['subscription_token']}"
        link_prefix = f"/vless/{config['subscription_token']}"
        if hmac.compare_digest(path, sub_prefix):
            self.send_bytes(
                clash_subscription(config).encode("utf-8"),
                "text/yaml; charset=utf-8",
            )
            return
        if hmac.compare_digest(path, legacy_sub_prefix):
            encoded = base64.b64encode((vless_link(config) + "\n").encode("utf-8"))
            self.send_bytes(encoded, "text/plain; charset=utf-8")
            return
        if hmac.compare_digest(path, link_prefix):
            self.send_bytes((vless_link(config) + "\n").encode("utf-8"), "text/plain; charset=utf-8")
            return

        if not self.require_auth():
            return
        if path == "/":
            self.send_bytes(PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/status":
            stats = xray_stats(config)
            self.send_json(
                {
                    "ok": True,
                    "node_name": config.get("node_name", "VLESS Lite"),
                    "service_status": service_status(str(config["xray_service"])),
                    "service_started": service_started(str(config["xray_service"])),
                    "uplink": stats["uplink"],
                    "downlink": stats["downlink"],
                    "stats_available": stats["available"],
                    "connections": current_connections(int(config["vpn_port"])),
                    "requests": self.server.request_count,  # type: ignore[attr-defined]
                    "panel_uptime": human_duration(int(time.monotonic() - STARTED_AT)),
                    "vless_link": vless_link(config),
                    "subscription_url": subscription_url(config),
                    "endpoint": f"{config['public_host']}:{config['vpn_port']}",
                }
            )
            return
        self.send_json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        self.server.increment_requests()  # type: ignore[attr-defined]
        if not self.require_auth():
            return
        if not self.valid_post_origin():
            self.send_json({"ok": False, "error": "Invalid origin"}, HTTPStatus.FORBIDDEN)
            return
        path = urllib.parse.urlsplit(self.path).path
        try:
            payload = self.read_body_json()
            config = load_config()
            if path == "/api/service":
                action = payload.get("action")
                if action not in {"start", "stop", "restart"}:
                    raise ValueError("Invalid service action")
                code, output = run_command(
                    ["systemctl", str(action), str(config["xray_service"])],
                    timeout=25,
                )
                if code != 0:
                    raise RuntimeError(output or "systemctl failed")
                self.send_json({"ok": True})
                return
            if path == "/api/rotate":
                update_xray_client(str(uuid.uuid4()), secrets.token_hex(8))
                fresh = load_config()
                self.send_json(
                    {
                        "ok": True,
                        "vless_link": vless_link(fresh),
                        "subscription_url": subscription_url(fresh),
                    }
                )
                return
            if path == "/api/reset-stats":
                stats = xray_stats(config, reset=True)
                if not stats["available"]:
                    raise RuntimeError(str(stats.get("error", "Stats API unavailable")))
                self.send_json({"ok": True})
                return
            self.send_json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def main() -> None:
    config = load_config()
    bind = str(config.get("ui_bind", "0.0.0.0"))
    port = int(config["ui_port"])
    server = PanelServer((bind, port), Handler)
    print(f"VLESS Lite panel listening on {bind}:{port}", flush=True)
    server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
