#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_NAME="VLESS Lite"
INSTALL_DIR="/opt/vless-lite-panel"
CONFIG_DIR="/etc/vless-lite-panel"
PANEL_CONFIG="${CONFIG_DIR}/config.json"
XRAY_CONFIG="/usr/local/etc/xray/config.json"
MANAGED_MARKER="${CONFIG_DIR}/managed"
PANEL_SERVICE="vless-lite-panel.service"
XRAY_SERVICE="xray.service"
XRAY_BIN="/usr/local/bin/xray"
XRAY_INSTALL_URL="https://github.com/XTLS/Xray-install/raw/main/install-release.sh"

PANEL_B64='__PANEL_B64__'

red='\033[0;31m'
green='\033[0;32m'
yellow='\033[1;33m'
blue='\033[0;34m'
reset='\033[0m'

info() { printf '%b[INFO]%b %s\n' "$blue" "$reset" "$*"; }
ok() { printf '%b[ OK ]%b %s\n' "$green" "$reset" "$*"; }
warn() { printf '%b[WARN]%b %s\n' "$yellow" "$reset" "$*"; }
die() { printf '%b[FAIL]%b %s\n' "$red" "$reset" "$*" >&2; exit 1; }

require_root() {
    [[ ${EUID:-$(id -u)} -eq 0 ]] || die "请使用 root 运行。"
    command -v systemctl >/dev/null 2>&1 || die "当前系统没有 systemd。"
}

install_packages() {
    local missing=()
    local command_name
    for command_name in curl unzip python3 openssl ss; do
        command -v "$command_name" >/dev/null 2>&1 || missing+=("$command_name")
    done
    ((${#missing[@]} == 0)) && return

    info "安装基础依赖：${missing[*]}"
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -y
        DEBIAN_FRONTEND=noninteractive apt-get install -y curl unzip python3 openssl ca-certificates iproute2
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y curl unzip python3 openssl ca-certificates iproute
    elif command -v yum >/dev/null 2>&1; then
        yum install -y curl unzip python3 openssl ca-certificates iproute
    elif command -v apk >/dev/null 2>&1; then
        apk add --no-cache curl unzip python3 openssl ca-certificates iproute2
    else
        die "无法识别包管理器，请先安装 curl、unzip、python3、openssl、iproute2。"
    fi
}

valid_port() {
    [[ "$1" =~ ^[0-9]+$ ]] && ((10#$1 >= 1 && 10#$1 <= 65535))
}

port_in_use() {
    ss -H -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "(^|[:.])${1}$"
}

prompt_port() {
    local label="$1" default_port="$2" minimum="$3" value
    while true; do
        read -r -p "${label} [${default_port}]: " value
        value="${value:-$default_port}"
        valid_port "$value" || { warn "端口必须在 1-65535。"; continue; }
        ((10#$value >= minimum)) || { warn "该端口必须大于或等于 ${minimum}。"; continue; }
        [[ "$value" != "22" ]] || { warn "不能使用 SSH 默认端口 22。"; continue; }
        port_in_use "$value" && { warn "端口 ${value} 已被占用。"; continue; }
        printf '%s' "$value"
        return
    done
}

detect_public_ip() {
    local ip
    ip="$(curl -4fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
    [[ -n "$ip" ]] || ip="$(curl -6fsS --max-time 8 https://api64.ipify.org 2>/dev/null || true)"
    printf '%s' "$ip"
}

valid_host() {
    [[ "$1" =~ ^[A-Za-z0-9._:-]+$ ]]
}

random_password() {
    openssl rand -hex 10
}

find_api_port() {
    local port
    for port in $(seq 10085 10125); do
        if ! port_in_use "$port"; then
            printf '%s' "$port"
            return
        fi
    done
    die "无法找到空闲的本地统计端口。"
}

install_xray() {
    if [[ -f "$XRAY_CONFIG" && ! -f "$MANAGED_MARKER" ]]; then
        die "检测到现有 Xray 配置，安装器不会覆盖非本项目管理的配置。"
    fi
    if [[ ! -x "$XRAY_BIN" ]]; then
        local installer
        installer="$(mktemp)"
        trap 'rm -f "${installer:-}"' RETURN
        info "通过 XTLS 官方安装器安装 Xray Core（不下载 geodata）"
        curl -fsSL "$XRAY_INSTALL_URL" -o "$installer"
        bash "$installer" install --without-geodata
        rm -f "$installer"
        trap - RETURN
    else
        ok "已检测到 Xray：$($XRAY_BIN version | head -n 1)"
    fi
}

generate_reality_keys() {
    local output
    output="$($XRAY_BIN x25519)"
    private_key="$(awk -F': *' '/PrivateKey|Private key/{print $2; exit}' <<<"$output")"
    public_key="$(awk -F': *' '/Password|PublicKey|Public key/{print $2; exit}' <<<"$output")"
    [[ -n "$private_key" && -n "$public_key" ]] || die "无法解析 Xray REALITY 密钥。输出：$output"
}

write_configs() {
    local salt_and_hash
    salt_and_hash="$(VLP_PASSWORD="$admin_password" python3 - <<'PY'
import hashlib
import os
import secrets

salt = secrets.token_bytes(16)
digest = hashlib.pbkdf2_hmac("sha256", os.environ["VLP_PASSWORD"].encode(), salt, 240_000)
print(salt.hex(), digest.hex())
PY
)"
    read -r admin_salt admin_hash <<<"$salt_and_hash"

    mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$(dirname "$XRAY_CONFIG")"
    printf '%s' "$PANEL_B64" | base64 -d >"${INSTALL_DIR}/panel.py"
    chmod 0755 "${INSTALL_DIR}/panel.py"

    export VLP_PUBLIC_HOST="$public_host"
    export VLP_VPN_PORT="$vpn_port"
    export VLP_UI_PORT="$ui_port"
    export VLP_API_PORT="$api_port"
    export VLP_UUID="$client_uuid"
    export VLP_EMAIL="$client_email"
    export VLP_SNI="$sni"
    export VLP_PRIVATE_KEY="$private_key"
    export VLP_PUBLIC_KEY="$public_key"
    export VLP_SHORT_ID="$short_id"
    export VLP_NODE_NAME="$node_name"
    export VLP_SUB_TOKEN="$subscription_token"
    export VLP_ADMIN_USER="$admin_user"
    export VLP_ADMIN_SALT="$admin_salt"
    export VLP_ADMIN_HASH="$admin_hash"
    export VLP_XRAY_CONFIG="$XRAY_CONFIG"
    export VLP_XRAY_BIN="$XRAY_BIN"

    python3 - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

def env(name):
    return os.environ[name]

xray = {
    "log": {"loglevel": "warning"},
    "api": {"tag": "api", "services": ["StatsService"]},
    "stats": {},
    "policy": {
        "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}},
        "system": {"statsInboundUplink": True, "statsInboundDownlink": True},
    },
    "inbounds": [
        {
            "tag": "api",
            "listen": "127.0.0.1",
            "port": int(env("VLP_API_PORT")),
            "protocol": "dokodemo-door",
            "settings": {"address": "127.0.0.1"},
        },
        {
            "tag": "vless-reality",
            "listen": "0.0.0.0",
            "port": int(env("VLP_VPN_PORT")),
            "protocol": "vless",
            "settings": {
                "clients": [
                    {
                        "id": env("VLP_UUID"),
                        "email": env("VLP_EMAIL"),
                        "flow": "xtls-rprx-vision",
                        "level": 0,
                    }
                ],
                "decryption": "none",
            },
            "streamSettings": {
                "network": "raw",
                "security": "reality",
                "realitySettings": {
                    "show": False,
                    "target": f'{env("VLP_SNI")}:443',
                    "xver": 0,
                    "serverNames": [env("VLP_SNI")],
                    "privateKey": env("VLP_PRIVATE_KEY"),
                    "shortIds": [env("VLP_SHORT_ID")],
                },
            },
            "sniffing": {
                "enabled": True,
                "destOverride": ["http", "tls", "quic"],
                "routeOnly": True,
            },
        },
    ],
    "outbounds": [
        {"tag": "direct", "protocol": "freedom"},
        {"tag": "blocked", "protocol": "blackhole"},
    ],
    "routing": {
        "domainStrategy": "AsIs",
        "rules": [
            {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
        ],
    },
}

panel = {
    "public_host": env("VLP_PUBLIC_HOST"),
    "public_scheme": "http",
    "vpn_port": int(env("VLP_VPN_PORT")),
    "ui_port": int(env("VLP_UI_PORT")),
    "ui_bind": "0.0.0.0",
    "api_server": f'127.0.0.1:{env("VLP_API_PORT")}',
    "uuid": env("VLP_UUID"),
    "email": env("VLP_EMAIL"),
    "sni": env("VLP_SNI"),
    "public_key": env("VLP_PUBLIC_KEY"),
    "short_id": env("VLP_SHORT_ID"),
    "node_name": env("VLP_NODE_NAME"),
    "subscription_token": env("VLP_SUB_TOKEN"),
    "admin_user": env("VLP_ADMIN_USER"),
    "admin_salt": env("VLP_ADMIN_SALT"),
    "admin_hash": env("VLP_ADMIN_HASH"),
    "xray_config": env("VLP_XRAY_CONFIG"),
    "xray_bin": env("VLP_XRAY_BIN"),
    "xray_service": "xray.service",
    "panel_service": "vless-lite-panel.service",
    "created_at": datetime.now(timezone.utc).isoformat(),
}

xray_path = Path(env("VLP_XRAY_CONFIG"))
xray_path.write_text(json.dumps(xray, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
xray_path.chmod(0o644)
panel_path = Path("/etc/vless-lite-panel/config.json")
panel_path.write_text(json.dumps(panel, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
panel_path.chmod(0o600)
PY

    touch "$MANAGED_MARKER"
    chmod 0600 "$MANAGED_MARKER"
}

write_systemd_service() {
    cat >"/etc/systemd/system/${PANEL_SERVICE}" <<EOF
[Unit]
Description=VLESS Lite Web Panel
After=network-online.target xray.service
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
Environment=PYTHONUNBUFFERED=1
Environment=VLP_CONFIG=${PANEL_CONFIG}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/panel.py
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=${CONFIG_DIR} $(dirname "$XRAY_CONFIG")
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
}

validate_and_start() {
    info "校验 Xray 配置"
    "$XRAY_BIN" run -test -config "$XRAY_CONFIG"
    systemctl enable "$XRAY_SERVICE" "$PANEL_SERVICE" >/dev/null
    systemctl restart "$XRAY_SERVICE"
    systemctl restart "$PANEL_SERVICE"
    sleep 2
    systemctl is-active --quiet "$XRAY_SERVICE" || die "Xray 启动失败，请运行 journalctl -u xray -n 100 查看。"
    systemctl is-active --quiet "$PANEL_SERVICE" || die "WebUI 启动失败，请运行 journalctl -u vless-lite-panel -n 100 查看。"
}

open_firewall() {
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
        ufw allow "${vpn_port}/tcp" >/dev/null
        ufw allow "${ui_port}/tcp" >/dev/null
        ok "已添加 UFW 规则。"
    elif command -v firewall-cmd >/dev/null 2>&1 && systemctl is-active --quiet firewalld; then
        firewall-cmd --permanent --add-port="${vpn_port}/tcp" >/dev/null
        firewall-cmd --permanent --add-port="${ui_port}/tcp" >/dev/null
        firewall-cmd --reload >/dev/null
        ok "已添加 firewalld 规则。"
    else
        warn "未检测到已启用的 UFW/firewalld；云服务器还需在安全组放行 ${vpn_port}/TCP 和 ${ui_port}/TCP。"
    fi
}

print_result() {
    local host_uri query node_encoded vless_uri panel_url sub_url
    host_uri="$public_host"
    [[ "$host_uri" == *:* ]] && host_uri="[${host_uri}]"
    query="encryption=none&flow=xtls-rprx-vision&security=reality&sni=$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$sni")&fp=chrome&pbk=${public_key}&sid=${short_id}&type=tcp&headerType=none"
    node_encoded="$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$node_name")"
    vless_uri="vless://${client_uuid}@${host_uri}:${vpn_port}?${query}#${node_encoded}"
    panel_url="http://${host_uri}:${ui_port}/"
    sub_url="http://${host_uri}:${ui_port}/sub/${subscription_token}"

    printf '\n%b%s 部署完成%b\n' "$green" "$PROJECT_NAME" "$reset"
    printf '%s\n' '------------------------------------------------------------'
    printf 'WebUI:       %s\n' "$panel_url"
    printf '管理员账号:  %s\n' "$admin_user"
    printf '管理员密码:  %s\n' "$admin_password"
    printf '订阅链接:    %s\n' "$sub_url"
    printf 'VLESS 链接:  %s\n' "$vless_uri"
    printf '%s\n' '------------------------------------------------------------'
    printf '节点端口: %s/TCP    WebUI端口: %s/TCP\n' "$vpn_port" "$ui_port"
    printf '服务命令: systemctl status xray vless-lite-panel\n'
    printf '卸载命令: bash %s --uninstall\n' "$0"
    warn "WebUI 当前使用 HTTP，请使用强密码，并在云安全组限制管理端口来源 IP。"
}

uninstall_panel() {
    require_root
    systemctl disable --now "$PANEL_SERVICE" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/${PANEL_SERVICE}"
    rm -rf "$INSTALL_DIR" "$CONFIG_DIR"
    systemctl daemon-reload
    ok "VLESS Lite WebUI 和配置已删除。"
    read -r -p "是否同时卸载 Xray Core？[y/N]: " answer
    if [[ "$answer" =~ ^[Yy]$ ]]; then
        local installer
        installer="$(mktemp)"
        curl -fsSL "$XRAY_INSTALL_URL" -o "$installer"
        bash "$installer" remove --purge
        rm -f "$installer"
        ok "Xray Core 已卸载。"
    else
        warn "Xray 仍保留，但其 VLESS 配置不会再由面板管理。"
    fi
}

main() {
    require_root
    if [[ "${1:-}" == "--uninstall" ]]; then
        uninstall_panel
        return
    fi
    install_packages

    if [[ -f "$MANAGED_MARKER" ]]; then
        warn "检测到已有 VLESS Lite 安装。"
        read -r -p "重新生成并覆盖当前节点配置？[y/N]: " reinstall
        [[ "$reinstall" =~ ^[Yy]$ ]] || exit 0
        systemctl stop "$PANEL_SERVICE" "$XRAY_SERVICE" >/dev/null 2>&1 || true
    fi

    local detected_ip default_password
    detected_ip="$(detect_public_ip)"
    while true; do
        read -r -p "服务器公网 IP 或域名 [${detected_ip:-必填}]: " public_host
        public_host="${public_host:-$detected_ip}"
        public_host="${public_host#[}"
        public_host="${public_host%]}"
        [[ -n "$public_host" ]] && valid_host "$public_host" && break
        warn "请输入有效的公网 IP 或域名。"
    done

    vpn_port="$(prompt_port 'VLESS 对外端口' '443' '1')"
    [[ "$vpn_port" == "443" ]] || warn "REALITY 使用非 443 端口时，Xray 会提示存在更高的封锁风险。"
    ui_port="$(prompt_port 'WebUI 对外端口' '2053' '1024')"
    [[ "$vpn_port" != "$ui_port" ]] || die "VLESS 和 WebUI 不能使用同一端口。"

    read -r -p "REALITY 伪装域名 [www.microsoft.com]: " sni
    sni="${sni:-www.microsoft.com}"
    valid_host "$sni" || die "伪装域名格式无效。"
    [[ "$sni" != *:* ]] || die "伪装域名不要填写端口。"

    read -r -p "节点名称 [VLESS-Lite]: " node_name
    node_name="${node_name:-VLESS-Lite}"
    read -r -p "WebUI 管理员账号 [admin]: " admin_user
    admin_user="${admin_user:-admin}"
    [[ "$admin_user" =~ ^[A-Za-z0-9_.-]{1,32}$ ]] || die "管理员账号只能包含字母、数字、点、下划线和横线。"
    default_password="$(random_password)"
    read -r -s -p "WebUI 管理员密码 [回车自动生成]: " admin_password
    printf '\n'
    admin_password="${admin_password:-$default_password}"
    ((${#admin_password} >= 10)) || die "管理员密码至少 10 位。"

    install_xray
    generate_reality_keys
    api_port="$(find_api_port)"
    client_uuid="$($XRAY_BIN uuid)"
    client_email="default@vless-lite"
    short_id="$(openssl rand -hex 8)"
    subscription_token="$(openssl rand -hex 24)"

    write_configs
    write_systemd_service
    validate_and_start
    open_firewall
    print_result
}

main "$@"
