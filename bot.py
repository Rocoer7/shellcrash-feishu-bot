#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书长连接机器人 -> SSH 控制路由器 ShellCrash。

安全设计：
- 固定白名单动作，不执行用户自定义 shell。
- 飞书卡片层级参考 ShellCrash 原菜单，但危险/复杂功能先不接入。
- 支持输入向导：在线生成/在线获取配置文件时等待用户发送链接。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)


@dataclass(frozen=True)
class Settings:
    feishu_app_id: str
    feishu_app_secret: str
    router_host: str
    router_port: str
    router_user: str
    router_password: str
    ssh_key_path: str
    allow_all_users: bool
    allowed_open_ids: set[str]
    allowed_chat_ids: set[str]
    max_reply_chars: int


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def csv_set(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def load_settings() -> Settings:
    settings = Settings(
        feishu_app_id=env("FEISHU_APP_ID"),
        feishu_app_secret=env("FEISHU_APP_SECRET"),
        router_host=env("ROUTER_HOST", "192.168.31.1"),
        router_port=env("ROUTER_PORT", "22"),
        router_user=env("ROUTER_USER", "root"),
        router_password=env("ROUTER_PASSWORD"),
        ssh_key_path=env("SSH_KEY_PATH"),
        allow_all_users=env("ALLOW_ALL_USERS", "false").lower() in {"1", "true", "yes", "on"},
        allowed_open_ids=csv_set(env("ALLOWED_OPEN_IDS")),
        allowed_chat_ids=csv_set(env("ALLOWED_CHAT_IDS")),
        max_reply_chars=int(env("MAX_REPLY_CHARS", "3200")),
    )
    missing = []
    if not settings.feishu_app_id:
        missing.append("FEISHU_APP_ID")
    if not settings.feishu_app_secret:
        missing.append("FEISHU_APP_SECRET")
    if not settings.router_host:
        missing.append("ROUTER_HOST")
    if not settings.router_user:
        missing.append("ROUTER_USER")
    if not settings.router_password and not settings.ssh_key_path:
        missing.append("ROUTER_PASSWORD 或 SSH_KEY_PATH")
    if missing:
        raise SystemExit("缺少环境变量：" + ", ".join(missing))
    return settings


LOG_LEVEL = env("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("shellcrash-feishu-bot")
settings = load_settings()

# 用户输入向导状态：open_id -> {action, chat_id, message_id, expire_at}
PENDING_INPUTS: dict[str, dict] = {}
PENDING_TTL_SECONDS = 5 * 60

if settings.allow_all_users:
    logger.warning("当前 ALLOW_ALL_USERS=true，任何能给机器人发消息的人都可以执行白名单命令。测试完成后建议关闭。")

feishu_client = (
    lark.Client.builder()
    .app_id(settings.feishu_app_id)
    .app_secret(settings.feishu_app_secret)
    .log_level(lark.LogLevel.DEBUG if LOG_LEVEL == "DEBUG" else lark.LogLevel.INFO)
    .build()
)


def mask_urls(text: str) -> str:
    # 订阅链接可能包含 token；在线配置相关输出里统一隐藏完整 URL。
    return re.sub(r"https?://[^\s\x1b]+", "https://***已隐藏***", text)


def truncate(text: str) -> str:
    text = text.strip() or "（无输出）"
    if len(text) <= settings.max_reply_chars:
        return text
    return text[: settings.max_reply_chars] + "\n...（输出过长，已截断）"


def ssh_command(remote_script: str, timeout: int = 25, mask_output_urls: bool = False) -> str:
    base_cmd = [
        "ssh",
        "-p",
        settings.router_port,
        "-o",
        "ConnectTimeout=8",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "HostKeyAlgorithms=+ssh-rsa",
        "-o",
        "PubkeyAcceptedAlgorithms=+ssh-rsa",
        "-o",
        "NumberOfPasswordPrompts=1",
    ]

    run_env = os.environ.copy()
    if settings.ssh_key_path:
        base_cmd += ["-i", settings.ssh_key_path]
    else:
        base_cmd = ["sshpass", "-e"] + base_cmd
        run_env["SSHPASS"] = settings.router_password

    target = f"{settings.router_user}@{settings.router_host}"
    cmd = base_cmd + [target, "sh", "-s"]
    logger.debug("执行 SSH 命令：%s", " ".join(shlex.quote(x) for x in cmd))

    proc = subprocess.run(
        cmd,
        input=remote_script,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=run_env,
        check=False,
    )
    output = ""
    if proc.stdout:
        output += proc.stdout
    if proc.stderr:
        output += "\n[stderr]\n" + proc.stderr
    if proc.returncode != 0:
        output += f"\n[exit_code] {proc.returncode}"
    if mask_output_urls:
        output = mask_urls(output)
    return truncate(output)


# ---------------------------- SSH 动作 ----------------------------

def shellcrash_status() -> str:
    return ssh_command(r'''
echo '【ShellCrash 状态】'
if pidof CrashCore >/dev/null 2>&1; then
  echo 'Mihomo / CrashCore：运行中'
  ps w 2>/dev/null | grep '[C]rashCore' || true
else
  echo 'Mihomo / CrashCore：未运行'
fi

echo
echo '【端口】'
(netstat -tulnp 2>/dev/null || ss -tulnp 2>/dev/null) | grep -E '9999|789|909|1053|CrashCore|mihomo|clash' || echo '未发现相关监听端口'

echo
echo '【最近日志】'
tail -n 20 /tmp/ShellCrash/ShellCrash.log 2>/dev/null || echo '未找到日志 /tmp/ShellCrash/ShellCrash.log'
''')


def shellcrash_ports() -> str:
    return ssh_command(r'''
echo '【代理相关监听端口】'
(netstat -tulnp 2>/dev/null || ss -tulnp 2>/dev/null) | grep -E '9999|789|909|1053|CrashCore|mihomo|clash' || echo '未发现相关监听端口'
''')


def shellcrash_log() -> str:
    return ssh_command(r'''
echo '【ShellCrash 最近日志】'
tail -n 100 /tmp/ShellCrash/ShellCrash.log 2>/dev/null || echo '未找到日志 /tmp/ShellCrash/ShellCrash.log'
''')


def shellcrash_start_restart() -> str:
    return ssh_command(r'''
echo '正在启动/重启 ShellCrash...'
/etc/init.d/shellcrash restart 2>&1 || /etc/init.d/shellcrash start 2>&1 || true
sleep 5
if pidof CrashCore >/dev/null 2>&1; then
  echo '结果：运行中'
else
  echo '结果：未运行，请查看日志'
fi
ps w 2>/dev/null | grep '[C]rashCore' || true

echo
echo '【端口】'
(netstat -tulnp 2>/dev/null || ss -tulnp 2>/dev/null) | grep -E '9999|789|909|1053|CrashCore|mihomo|clash' || echo '未发现相关监听端口'
''', timeout=60)


def shellcrash_stop() -> str:
    return ssh_command(r'''
echo '正在停止 ShellCrash...'
/etc/init.d/shellcrash stop 2>&1 || true
sleep 2
if pidof CrashCore >/dev/null 2>&1; then
  echo '结果：仍在运行'
else
  echo '结果：已停止'
fi
''', timeout=40)


def shellcrash_boot_status() -> str:
    return ssh_command(r'''
echo '【开机启动状态】'
if [ -e /etc/rc.d/S99shellcrash ]; then
  echo 'ShellCrash：已设置开机启动'
  ls -l /etc/rc.d/S99shellcrash
else
  echo 'ShellCrash：未设置开机启动'
fi
''')


def shellcrash_boot_enable() -> str:
    return ssh_command(r'''
echo '正在启用 ShellCrash 开机启动...'
/etc/init.d/shellcrash enable 2>&1 || true
if [ -e /etc/rc.d/S99shellcrash ]; then
  echo '结果：已启用开机启动'
else
  echo '结果：启用失败或未生效'
fi
''')


def shellcrash_boot_disable() -> str:
    return ssh_command(r'''
echo '正在禁用 ShellCrash 开机启动...'
/etc/init.d/shellcrash disable 2>&1 || true
if [ -e /etc/rc.d/S99shellcrash ]; then
  echo '结果：仍处于开机启动状态'
else
  echo '结果：已禁用开机启动'
fi
''')


def shellcrash_task_status() -> str:
    return ssh_command(r'''
echo '【自动任务】'
(crontab -l 2>/dev/null || cat /etc/crontabs/root 2>/dev/null || true) | grep -Ei 'shellcrash|ShellCrash|crash|mihomo' || echo '未发现 ShellCrash 相关自动任务'
''')


def shellcrash_config_summary() -> str:
    return ssh_command(r'''
echo '【配置摘要】'
CRASHDIR=/data/ShellCrash
CFG=$CRASHDIR/configs/ShellCrash.cfg
[ -f "$CFG" ] || { echo '未找到 ShellCrash.cfg'; exit 0; }
. "$CFG" 2>/dev/null || true
printf '路由模式：%s\n' "${redir_mod:-未知}"
printf 'DNS 模式：%s\n' "${dns_mod:-未知}"
printf '核心类型：%s\n' "${crashcore:-未知}"
printf '核心版本：%s\n' "${core_v:-未知}"
printf '开机启动：%s\n' "$([ -e /etc/rc.d/S99shellcrash ] && echo 已启用 || echo 未启用)"
if [ -n "$Url" ]; then echo '订阅转换链接：已配置（已隐藏）'; else echo '订阅转换链接：未配置'; fi
if [ -n "$Https" ]; then echo '完整配置链接：已配置（已隐藏）'; else echo '完整配置链接：未配置'; fi
if echo "$crashcore" | grep -q 'singbox'; then config_path=$CRASHDIR/jsons/config.json; else config_path=$CRASHDIR/yamls/config.yaml; fi
if [ -f "$config_path" ]; then
  echo "配置文件：$config_path"
  ls -lh "$config_path"
else
  echo "配置文件：未找到 $config_path"
fi
''')


def shellcrash_update_recorded_config() -> str:
    return ssh_command(r'''
echo '正在更新已记录的配置文件...'
CRASHDIR=/data/ShellCrash
CFG=$CRASHDIR/configs/ShellCrash.cfg
[ -f "$CFG" ] || { echo '未找到 ShellCrash.cfg'; exit 1; }
. "$CFG"
. "$CRASHDIR"/libs/get_config.sh
. "$CRASHDIR"/starts/core_config.sh
if [ -z "$Url" ] && [ -z "$Https" ]; then
  echo '没有找到已记录的订阅/配置链接，请先使用在线生成或在线获取。'
  exit 1
fi
get_core_config
rc=$?
if [ "$rc" = 0 ]; then
  echo '配置文件更新成功。'
  if pidof CrashCore >/dev/null 2>&1; then
    echo '正在重启 ShellCrash 使配置生效...'
    /etc/init.d/shellcrash restart 2>&1 || true
  fi
else
  echo '配置文件更新失败。'
fi
exit "$rc"
''', timeout=120, mask_output_urls=True)


def shellcrash_dns_port_status() -> str:
    return ssh_command(r'''
echo '【系统 DNS / ShellCrash DNS 端口】'
(netstat -tulnp 2>/dev/null || ss -tulnp 2>/dev/null) | grep -E '(:53|:1053)' || echo '未发现 :53 / :1053 监听'
''')


def shellcrash_connectivity_test() -> str:
    return ssh_command(r'''
echo '【代理连通性测试】'
if ! pidof CrashCore >/dev/null 2>&1; then
  echo 'CrashCore 未运行，无法测试代理。'
  exit 1
fi
if command -v curl >/dev/null 2>&1; then
  curl -I -m 12 -x http://127.0.0.1:7890 https://www.gstatic.com/generate_204 2>&1 | sed -n '1,12p'
elif command -v wget >/dev/null 2>&1; then
  http_proxy=http://127.0.0.1:7890 https_proxy=http://127.0.0.1:7890 wget -S --spider -T 12 https://www.gstatic.com/generate_204 2>&1 | sed -n '1,16p'
else
  echo '路由器未找到 curl/wget，无法执行 HTTP 连通性测试。'
fi
''', timeout=25)


def shellcrash_memory_disk() -> str:
    return ssh_command(r'''
echo '【内存/磁盘】'
free -h 2>/dev/null || free 2>/dev/null || true
echo
ps w 2>/dev/null | grep '[C]rashCore' || true
echo
df -h /data /tmp 2>/dev/null || df -h 2>/dev/null | sed -n '1,8p'
''')


def shellcrash_version_info() -> str:
    return ssh_command(r'''
echo '【版本信息】'
CRASHDIR=/data/ShellCrash
printf 'ShellCrash：%s\n' "$(cat "$CRASHDIR/version" 2>/dev/null || echo 未知)"
. "$CRASHDIR/configs/ShellCrash.cfg" 2>/dev/null || true
printf '核心类型：%s\n' "${crashcore:-未知}"
printf '核心版本：%s\n' "${core_v:-未知}"
uname -a
''')


def shellcrash_pac_link() -> str:
    return ssh_command(r'''
echo '【PAC 链接】'
CRASHDIR=/data/ShellCrash
. "$CRASHDIR/configs/ShellCrash.cfg" 2>/dev/null || true
host=$(ubus call network.interface.lan status 2>/dev/null | grep '"address"' | grep -oE '[0-9]{1,3}(\.[0-9]{1,3}){3}' | head -1)
[ -z "$host" ] && host=192.168.31.1
[ -z "$db_port" ] && db_port=9999
echo "http://$host:$db_port/ui/pac"
''')


def shellcrash_core_check() -> str:
    return ssh_command(r'''
echo '【核心文件检查】'
CRASHDIR=/data/ShellCrash
. "$CRASHDIR/configs/ShellCrash.cfg" 2>/dev/null || true
printf '配置核心：%s %s\n' "${crashcore:-未知}" "${core_v:-未知}"
ls -lh /tmp/ShellCrash/CrashCore /data/ShellCrash/CrashCore.tar.gz 2>/dev/null || true
if pidof CrashCore >/dev/null 2>&1; then
  echo 'CrashCore 进程：运行中'
else
  echo 'CrashCore 进程：未运行'
fi
''')


def shellcrash_online_config(link: str, mode: str) -> str:
    quoted_link = shlex.quote(link)
    if mode == "online_generate":
        mode_title = "在线生成配置文件"
        prep = f'''
raw_link={quoted_link}
Url=$(printf '%s' "$raw_link" | sed 's/&/%26/g; s/#.*//')
Https=''
setconfig Https
setconfig Url "'$Url'"
'''
    elif mode == "online_fetch":
        mode_title = "在线获取配置文件"
        prep = f'''
raw_link={quoted_link}
Https=$(printf '%s' "$raw_link")
Url=''
setconfig Https "'$Https'"
setconfig Url
'''
    else:
        return "未知配置模式。"

    return ssh_command(f'''
echo '【{mode_title}】'
CRASHDIR=/data/ShellCrash
CFG=$CRASHDIR/configs/ShellCrash.cfg
[ -f "$CFG" ] || {{ echo '未找到 ShellCrash.cfg'; exit 1; }}
. "$CRASHDIR"/libs/get_config.sh
. "$CRASHDIR"/starts/core_config.sh
{prep}
echo '已写入链接记录（链接内容已隐藏），开始获取配置文件...'
get_core_config
rc=$?
if [ "$rc" = 0 ]; then
  echo '配置文件获取/生成成功。'
  if pidof CrashCore >/dev/null 2>&1; then
    echo '正在重启 ShellCrash 使配置生效...'
    /etc/init.d/shellcrash restart 2>&1 || true
    sleep 4
    pidof CrashCore >/dev/null 2>&1 && echo 'ShellCrash 已重启并运行中。' || echo 'ShellCrash 重启后未检测到运行。'
  else
    echo 'ShellCrash 当前未运行，配置已保存。'
  fi
else
  echo '配置文件获取/生成失败，请检查链接、网络或在线转换服务。'
fi
exit "$rc"
''', timeout=180, mask_output_urls=True)


# ---------------------------- 卡片 ----------------------------

def btn(text: str, value: dict, button_type: str | None = None, confirm: str | None = None) -> dict:
    # 未接入项必须在按钮文案上直说，不能只依赖颜色暗示，避免误解。
    if value.get("noop") and "未接入" not in text:
        text = f"{text}（未接入）"
    item = {"tag": "button", "text": {"tag": "plain_text", "content": text}, "value": value}
    if button_type:
        item["type"] = button_type
    if confirm:
        item["confirm"] = {
            "title": {"tag": "plain_text", "content": "请确认"},
            "text": {"tag": "plain_text", "content": confirm},
        }
    return item


def action_row(buttons: list[dict]) -> dict:
    return {"tag": "action", "actions": buttons}


def note(text: str) -> dict:
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": text}]}


def card(title: str, markdown: str, rows: list[list[dict]], template: str = "blue") -> dict:
    elements = [{"tag": "markdown", "content": markdown}]
    for row in rows:
        elements.append(action_row(row))
    elements.append({"tag": "hr"})
    elements.append(note("说明：带“（未接入）”的按钮只会提示，不会执行；蓝色/红色通常代表已接入且会执行或修改状态。"))
    elements.append(note("文字命令仍可用：/menu、/status、/log、/restart、/cancel。"))
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
        "elements": elements,
    }


def not_ready(label: str) -> dict:
    return {"noop": label}


def build_card(menu: str = "main", pending_action: str | None = None) -> dict:
    back = btn("0 返回主菜单", {"nav": "main"})
    if menu == "main":
        return card(
            "ShellCrash 控制面板",
            "层级参考 ShellCrash 原 `crash` 菜单；按钮带 **（未接入）** 表示当前只占位，不会执行。",
            [
                [btn("1 启动/重启服务", {"action": "start_restart"}, "primary")],
                [btn("2 功能设置", {"nav": "settings"}), btn("3 停止服务", {"action": "stop"}, "danger", "确认停止 ShellCrash？")],
                [btn("4 启动设置", {"nav": "boot"}), btn("5 设置自动任务", {"nav": "tasks"})],
                [btn("6 管理配置文件", {"nav": "config"}), btn("7 访问与控制", {"nav": "access"})],
                [btn("8 工具与优化", {"nav": "tools"}), btn("9 更新与支持", {"nav": "updates"})],
            ],
        )
    if menu == "settings":
        return card(
            "2 功能设置",
            "这些功能会改变路由/DNS/端口等关键配置，第一批先保留层级，暂不执行。",
            [
                [btn("1 路由模式设置", not_ready("路由模式设置")), btn("2 DNS 设置", not_ready("DNS 设置"))],
                [btn("3 透明路由流量过滤", not_ready("透明路由流量过滤")), btn("6 自定义端口及密钥", not_ready("自定义端口及密钥"))],
                [btn("8 IPv6 设置", not_ready("IPv6 设置")), btn("9 重置/备份/还原", not_ready("重置/备份/还原脚本设置"))],
                [back],
            ],
            "grey",
        )
    if menu == "boot":
        return card(
            "4 启动设置",
            "可查看、启用或禁用 ShellCrash 开机启动。",
            [
                [btn("查看开机启动状态", {"action": "boot_status"})],
                [btn("启用开机启动", {"action": "boot_enable"}, "primary", "确认启用 ShellCrash 开机启动？"), btn("禁用开机启动", {"action": "boot_disable"}, "danger", "确认禁用 ShellCrash 开机启动？")],
                [back],
            ],
        )
    if menu == "tasks":
        return card(
            "5 设置自动任务",
            "第一批先接入查看自动任务，修改类后续按需补充。",
            [
                [btn("查看自动任务", {"action": "task_status"})],
                [btn("更新配置定时任务", not_ready("更新配置定时任务")), btn("重启服务定时任务", not_ready("重启服务定时任务"))],
                [back],
            ],
            "grey",
        )
    if menu == "config":
        return card(
            "6 管理配置文件",
            "在线生成/在线获取会进入输入向导：点击后请在 5 分钟内发送链接，或发送 `/cancel` 取消。",
            [
                [btn("1 在线生成配置文件", {"await": "online_generate"}, "primary"), btn("2 在线获取配置文件", {"await": "online_fetch"}, "primary")],
                [btn("3 本地生成配置文件", not_ready("本地生成配置文件")), btn("4 本地上传完整配置文件", not_ready("本地上传完整配置文件"))],
                [btn("5 设置自动更新", not_ready("设置自动更新")), btn("6 自定义配置文件", not_ready("自定义配置文件"))],
                [btn("7 更新配置文件", {"action": "update_recorded_config"}, "primary", "确认使用已记录链接更新配置文件？"), btn("8 还原配置文件", not_ready("还原配置文件"))],
                [btn("9 自定义浏览器 UA", not_ready("自定义浏览器 UA")), btn("查看配置摘要", {"action": "config_summary"})],
                [back],
            ],
        )
    if menu == "access":
        return card(
            "7 访问与控制",
            "本层多涉及公网访问和入站节点，默认暂不接入，避免误开公网入口。",
            [
                [btn("1 公网访问防火墙", not_ready("公网访问防火墙")), btn("2 Telegram 控制机器人", not_ready("Telegram 控制机器人"))],
                [btn("3 DDNS 自动域名", not_ready("DDNS 自动域名")), btn("4 公网 Vmess 入站", not_ready("公网 Vmess 入站"))],
                [btn("5 公网 Shadowsocks 入站", not_ready("公网 Shadowsocks 入站"))],
                [btn("6 Tailscale 内网穿透", not_ready("Tailscale 内网穿透")), btn("7 WireGuard 客户端", not_ready("WireGuard 客户端"))],
                [back],
            ],
            "grey",
        )
    if menu == "tools":
        return card(
            "8 工具与优化",
            "接入了常用查询和测试项；涉及系统改动的工具后续按需补充。",
            [
                [btn("查看运行日志", {"action": "log"}), btn("查看 DNS 端口占用", {"action": "dns_port"})],
                [btn("测试代理连通性", {"action": "connectivity"}), btn("查看内存/磁盘", {"action": "memory_disk"})],
                [btn("查看路由规则", not_ready("查看 ShellCrash 相关路由规则")), btn("新手引导", not_ready("ShellCrash 新手引导"))],
                [back],
            ],
        )
    if menu == "updates":
        return card(
            "9 更新与支持",
            "第一批接入查看类功能；脚本/核心/数据库更新后续再做确认流程。",
            [
                [btn("查看版本信息", {"action": "version_info"}), btn("查看 PAC 链接", {"action": "pac_link"})],
                [btn("检查核心文件", {"action": "core_check"}), btn("更新数据库文件", not_ready("更新数据库文件"))],
                [btn("更新管理脚本", not_ready("更新管理脚本")), btn("切换核心文件", not_ready("切换核心文件"))],
                [btn("安装 Dashboard", not_ready("安装 Dashboard")), btn("卸载 ShellCrash", not_ready("卸载 ShellCrash（不接入）"), "danger")],
                [back],
            ],
            "grey",
        )
    if menu == "await_config":
        title = "在线生成配置文件" if pending_action == "online_generate" else "在线获取配置文件"
        desc = "请发送订阅/分享链接，机器人会通过 Subconverter 生成配置。" if pending_action == "online_generate" else "请发送订阅提供商直接给出的 Clash/Mihomo 完整配置链接。"
        return card(
            title,
            f"{desc}\n\n要求：\n- 仅支持 http/https 链接\n- 5 分钟内有效\n- 发送 `/cancel` 可取消\n- 回复中会隐藏完整链接，避免泄露 token",
            [[btn("取消输入", {"cancel_pending": True}), btn("返回配置文件菜单", {"nav": "config"})]],
            "orange",
        )
    return build_card("main")


def reply(message_id: str, text: str) -> None:
    request = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            ReplyMessageRequestBody.builder()
            .msg_type("text")
            .content(json.dumps({"text": truncate(text)}, ensure_ascii=False))
            .build()
        )
        .build()
    )
    response = feishu_client.im.v1.message.reply(request)
    if not response.success():
        logger.error("回复飞书消息失败 code=%s msg=%s log_id=%s", response.code, response.msg, response.get_log_id())


def reply_card(message_id: str, menu: str = "main") -> None:
    request = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            ReplyMessageRequestBody.builder()
            .msg_type("interactive")
            .content(json.dumps(build_card(menu), ensure_ascii=False))
            .build()
        )
        .build()
    )
    response = feishu_client.im.v1.message.reply(request)
    if not response.success():
        logger.error("回复飞书卡片失败 code=%s msg=%s log_id=%s", response.code, response.msg, response.get_log_id())


ACTIONS: dict[str, Callable[[], str]] = {
    "start_restart": shellcrash_start_restart,
    "stop": shellcrash_stop,
    "status": shellcrash_status,
    "ports": shellcrash_ports,
    "log": shellcrash_log,
    "boot_status": shellcrash_boot_status,
    "boot_enable": shellcrash_boot_enable,
    "boot_disable": shellcrash_boot_disable,
    "task_status": shellcrash_task_status,
    "config_summary": shellcrash_config_summary,
    "update_recorded_config": shellcrash_update_recorded_config,
    "dns_port": shellcrash_dns_port_status,
    "connectivity": shellcrash_connectivity_test,
    "memory_disk": shellcrash_memory_disk,
    "version_info": shellcrash_version_info,
    "pac_link": shellcrash_pac_link,
    "core_check": shellcrash_core_check,
}

SLASH_COMMANDS: dict[str, Callable[[], str]] = {
    "/help": lambda: help_text(),
    "/status": shellcrash_status,
    "/ports": shellcrash_ports,
    "/log": shellcrash_log,
    "/start": shellcrash_start_restart,
    "/stop": shellcrash_stop,
    "/restart": shellcrash_start_restart,
    "/update": shellcrash_update_recorded_config,
}


def help_text() -> str:
    return """ShellCrash 飞书桥接机器人命令：
/help    查看帮助
/menu    显示 ShellCrash 原层级控制面板
/whoami  查看 sender_id 和 chat_id，用于配置白名单
/status  查看 ShellCrash 状态
/ports   查看代理端口
/log     查看最近日志
/start   启动/重启 ShellCrash
/stop    停止 ShellCrash
/restart 启动/重启 ShellCrash
/cancel  取消当前输入向导

按钮菜单已参考 ShellCrash 原 1～9 层级；暂未接入项会提示，不会执行危险操作。"""


# ---------------------------- 消息与回调 ----------------------------

def extract_text(content: str) -> str:
    try:
        data = json.loads(content or "{}")
        text = data.get("text", "")
    except Exception:
        text = content or ""
    text = re.sub(r"<at[^>]*>.*?</at>", " ", text)
    text = re.sub(r"^@\S+\s+", "", text.strip())
    return text.strip()


def parse_command(text: str) -> str | None:
    match = re.search(r"/(help|menu|whoami|status|ports|log|start|stop|restart|update|cancel)\b", text, re.I)
    if not match:
        return None
    return "/" + match.group(1).lower()


def is_allowed(sender_id: str, chat_id: str, command: str) -> tuple[bool, str]:
    if command in {"/help", "/whoami"}:
        return True, ""
    if settings.allowed_chat_ids and chat_id not in settings.allowed_chat_ids:
        return False, "当前会话不在 ALLOWED_CHAT_IDS 白名单内。"
    if settings.allow_all_users:
        return True, ""
    if sender_id in settings.allowed_open_ids:
        return True, ""
    return False, "你不在 ALLOWED_OPEN_IDS 白名单内。请先用 /whoami 获取 sender_id 后配置到 .env。"


def validate_url(text: str) -> tuple[bool, str]:
    value = text.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False, "请输入 http/https 开头的有效链接。"
    if len(value) > 3000:
        return False, "链接过长，请检查是否粘贴了多余内容。"
    return True, value


LONG_ACTIONS = {
    "start_restart": "启动/重启服务",
    "stop": "停止服务",
    "update_recorded_config": "更新配置文件",
}


def run_action_async(message_id: str, action: str) -> None:
    def worker() -> None:
        if action in LONG_ACTIONS:
            reply(message_id, f"已收到：{LONG_ACTIONS[action]}，正在后台执行。\n如果飞书界面偶尔提示回调超时，请以稍后的结果回复为准。")
        try:
            result = ACTIONS[action]()
        except subprocess.TimeoutExpired:
            logger.exception("执行按钮动作超时：%s", action)
            result = "执行超时：路由器 SSH 或 ShellCrash 响应太慢。"
        except Exception as exc:
            logger.exception("执行按钮动作失败：%s", action)
            result = f"执行失败：{type(exc).__name__}: {exc}"
        reply(message_id, f"按钮执行 `{action}` 的结果：\n\n{result}")
    threading.Thread(target=worker, daemon=True).start()


def run_online_config_async(message_id: str, mode: str, link: str) -> None:
    def worker() -> None:
        title = "在线生成配置文件" if mode == "online_generate" else "在线获取配置文件"
        try:
            result = shellcrash_online_config(link, mode)
        except subprocess.TimeoutExpired:
            logger.exception("%s 超时", title)
            result = f"{title} 超时：请检查路由器网络或订阅服务。"
        except Exception as exc:
            logger.exception("%s 失败", title)
            result = f"{title} 失败：{type(exc).__name__}: {exc}"
        reply(message_id, f"{title} 处理完成：\n\n{result}")
    threading.Thread(target=worker, daemon=True).start()


def card_response(card_data: dict | None = None, toast: str = "", toast_type: str = "info") -> P2CardActionTriggerResponse:
    payload: dict = {}
    if toast:
        payload["toast"] = {"type": toast_type, "content": toast}
    if card_data is not None:
        payload["card"] = {"type": "raw", "data": card_data}
    return P2CardActionTriggerResponse(payload)


def on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    event = data.event
    value = event.action.value if event and event.action else {}
    sender_id = event.operator.open_id if event and event.operator else ""
    chat_id = event.context.open_chat_id if event and event.context else ""
    message_id = event.context.open_message_id if event and event.context else ""
    logger.info("收到卡片按钮 sender_id=%s chat_id=%s message_id=%s value=%s", sender_id, chat_id, message_id, value)

    nav = (value or {}).get("nav")
    if nav:
        if nav in {"main", "settings", "boot", "tasks", "config", "access", "tools", "updates"}:
            return card_response(build_card(nav))
        return card_response(toast="未知菜单。", toast_type="warning")

    noop = (value or {}).get("noop")
    if noop:
        return card_response(toast=f"暂未接入：{noop}", toast_type="warning")

    if (value or {}).get("cancel_pending"):
        PENDING_INPUTS.pop(sender_id, None)
        return card_response(build_card("config"), "已取消输入。")

    await_action = (value or {}).get("await")
    if await_action in {"online_generate", "online_fetch"}:
        allowed, reason = is_allowed(sender_id, chat_id, "/status")
        if not allowed:
            return card_response(toast="拒绝执行：" + reason, toast_type="warning")
        PENDING_INPUTS[sender_id] = {
            "action": await_action,
            "chat_id": chat_id,
            "message_id": message_id,
            "expire_at": time.time() + PENDING_TTL_SECONDS,
        }
        return card_response(build_card("await_config", await_action), "请在 5 分钟内发送链接。")

    action = (value or {}).get("action")
    if action in ACTIONS:
        allowed, reason = is_allowed(sender_id, chat_id, "/status")
        if not allowed:
            return card_response(toast="拒绝执行：" + reason, toast_type="warning")
        if not message_id:
            return card_response(toast="无法获取消息上下文，请改用文字命令。", toast_type="warning")
        run_action_async(message_id, action)
        return card_response(toast=f"已收到 `{action}`，稍后在当前消息下回复结果。")

    return card_response(toast="未知按钮。", toast_type="warning")


def handle_pending_input(message_id: str, sender_id: str, chat_id: str, text: str) -> bool:
    pending = PENDING_INPUTS.get(sender_id)
    if not pending:
        return False
    if time.time() > pending.get("expire_at", 0):
        PENDING_INPUTS.pop(sender_id, None)
        reply(message_id, "输入已超时，请重新点击配置文件菜单中的对应按钮。")
        return True
    if pending.get("chat_id") and pending.get("chat_id") != chat_id:
        reply(message_id, "当前输入向导属于另一个会话，请回到原会话继续，或发送 /cancel 取消。")
        return True
    if text.strip().lower() == "/cancel":
        PENDING_INPUTS.pop(sender_id, None)
        reply(message_id, "已取消当前输入向导。")
        return True
    ok, value = validate_url(text)
    if not ok:
        reply(message_id, value + "\n如需取消，请发送 /cancel。")
        return True
    action = pending.get("action")
    PENDING_INPUTS.pop(sender_id, None)
    reply(message_id, "已收到链接，开始处理配置文件...\n这一步可能需要 10～60 秒。完整链接不会在回复中显示。")
    run_online_config_async(message_id, action, value)
    return True


def on_message(data: P2ImMessageReceiveV1) -> None:
    event = data.event
    message = event.message
    sender = event.sender
    message_id = message.message_id
    chat_id = message.chat_id
    sender_id = sender.sender_id.open_id if sender and sender.sender_id else ""
    text = extract_text(message.content)
    command = parse_command(text)
    logger.info("收到消息 chat_id=%s sender_id=%s text=%r command=%s", chat_id, sender_id, text, command)

    if command == "/whoami":
        reply(message_id, f"sender_id(open_id)：{sender_id}\nchat_id：{chat_id}\n\n建议把 sender_id 填到 .env 的 ALLOWED_OPEN_IDS。")
        return

    if command == "/cancel":
        if PENDING_INPUTS.pop(sender_id, None):
            reply(message_id, "已取消当前输入向导。")
        else:
            reply(message_id, "当前没有等待输入的向导。")
        return

    if command is None and handle_pending_input(message_id, sender_id, chat_id, text):
        return

    if not command:
        return

    if command == "/menu":
        allowed, reason = is_allowed(sender_id, chat_id, "/status")
        if not allowed:
            reply(message_id, "拒绝显示菜单：" + reason)
            return
        reply_card(message_id, "main")
        return

    allowed, reason = is_allowed(sender_id, chat_id, command)
    if not allowed:
        reply(message_id, "拒绝执行：" + reason)
        return

    if command in SLASH_COMMANDS:
        try:
            result = SLASH_COMMANDS[command]()
        except subprocess.TimeoutExpired:
            logger.exception("执行命令超时：%s", command)
            result = "执行超时：路由器 SSH 或 ShellCrash 响应太慢。"
        except Exception as exc:
            logger.exception("执行命令失败：%s", command)
            result = f"执行失败：{type(exc).__name__}: {exc}"
        reply(message_id, result)


def main() -> None:
    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_card_action_trigger(on_card_action)
        .build()
    )
    ws_client = lark.ws.Client(
        settings.feishu_app_id,
        settings.feishu_app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.DEBUG if LOG_LEVEL == "DEBUG" else lark.LogLevel.INFO,
    )
    logger.info("启动飞书长连接客户端... app_id=%s router=%s", settings.feishu_app_id, settings.router_host)
    ws_client.start()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("收到退出信号")
        sys.exit(0)
