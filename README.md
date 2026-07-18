# VLESS Lite Panel

面向小内存 Linux 服务器的一键 VLESS + REALITY 节点部署脚本。

安装器会交互询问：

- 服务器公网 IP 或域名
- VLESS 对外 TCP 端口
- WebUI 对外 TCP 端口
- REALITY 伪装域名
- 节点名称
- WebUI 管理员账号和密码

不使用 Docker、数据库、Node.js、Nginx 或独立前端服务。常驻组件只有 Xray Core 和一个使用
Python 标准库编写的管理进程。

在本项目的空载验证环境中，Xray Core RSS 约 `36MB`，WebUI RSS 约 `23MB`，合计约
`59MB`。实际占用会随架构、连接数和系统版本变化。

## 功能

- VLESS + REALITY + XTLS Vision
- 自动生成 UUID、REALITY 密钥和 Short ID
- 自动生成手机可导入的 `vless://` 分享链接
- 自动生成 Clash Meta/Mihomo 可直接导入的 YAML 订阅链接
- 保留 Base64 VLESS 订阅兼容接口
- WebUI Basic Auth 登录
- 下载/上传总流量和实时速度
- 当前 TCP 连接数
- 启动、停止、重启 Xray
- 清零 Xray 流量统计
- 一键轮换 UUID 和 Short ID
- IPv4、IPv6和域名节点地址
- UFW/firewalld 已启用时自动放行端口
- systemd 开机启动和进程守护
- 卸载入口

## 系统要求

- 使用 systemd 的 Linux
- root 权限
- Debian、Ubuntu、CentOS、Rocky、AlmaLinux 或 Alpine 等常见发行版
- 至少约 60MB 可用内存；建议 128MB 以上
- 一个可从公网访问的 TCP 端口
- WebUI 端口建议通过云安全组限制为自己的 IP

支持的 CPU 架构取决于 XTLS 官方安装器，包括常见的 x86_64、ARM64 等架构。

## 安装

```bash
chmod +x install.sh
sudo ./install.sh
```

安装过程中会明确询问 VLESS 端口和 WebUI 端口，不会占用 SSH 的 `22` 端口，也不允许两个
服务使用同一个端口。

安装完成后终端会输出：

- WebUI 地址
- 管理员账号和密码
- Clash Meta/Mihomo 订阅链接
- VLESS 手机分享链接

还需要在云厂商安全组中放行安装时填写的两个 TCP 端口。

## WebUI

浏览器打开安装完成后输出的 WebUI 地址，使用安装时设置的管理员账号和密码登录。

页面提供：

- 节点在线状态
- 上下行流量和实时速度
- 当前连接数
- VLESS 分享链接与 Clash Meta/Mihomo 订阅链接复制
- Xray 服务控制
- 凭据轮换

WebUI 显示的订阅地址可直接添加到 Mihomo Party、Clash Verge Rev 等使用 Mihomo/Clash Meta
内核的客户端。订阅内容包含 VLESS + REALITY + XTLS Vision 节点、`PROXY` 策略组和默认
`MATCH` 规则。

订阅地址通过 48 字节随机令牌访问，不需要 WebUI 账号密码。轮换节点凭据后，订阅 URL
保持不变，订阅内容自动更新；原 VLESS 链接会立即失效。需要旧式 Base64 VLESS 列表时，
使用 `/sub/base64/<订阅令牌>` 兼容接口。

## 文件位置

```text
/opt/vless-lite-panel/panel.py
/etc/vless-lite-panel/config.json
/usr/local/etc/xray/config.json
/etc/systemd/system/vless-lite-panel.service
```

服务名称：

```bash
systemctl status xray
systemctl status vless-lite-panel
```

日志：

```bash
journalctl -u xray -n 100 --no-pager
journalctl -u vless-lite-panel -n 100 --no-pager
```

## 卸载

```bash
sudo ./install.sh --uninstall
```

卸载时会询问是否同时删除 Xray Core。

## 安全说明

- WebUI 默认通过 HTTP 提供服务。请设置强密码，并在云安全组中限制 WebUI 端口的来源 IP。
- 如果需要公开 WebUI，建议在外层配置 HTTPS 反向代理。
- 安装器发现非本项目管理的现有 Xray 配置时会直接停止，不会覆盖。
- REALITY 通常建议使用 `443/TCP`。其他端口可以使用，但 Xray 会提示非 443 端口存在更高的封锁风险。
- 请遵守服务器所在地和使用所在地的法律法规及服务商条款。

## 开发与测试

修改 `panel.py` 或 `install.template.sh` 后重新生成自包含安装器：

```bash
./build-installer.sh
```

运行测试：

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh
```

`install.sh` 内嵌了完整的 `panel.py`，单独下载这一个文件即可部署。
