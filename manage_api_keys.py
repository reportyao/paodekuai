#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跑得快 AI 对外 API —— Key 管理工具（CLI）。

网关（api_gateway.py）按 mtime 热加载 keys.json，所以这里改完**立即生效，无需重启**。

用法（在服务器上）：
  # 生成新 key（明文只在这一次输出里出现，务必立刻交给调用方并自行保存）
  sudo python3 manage_api_keys.py add --name "张三公司" --per-min 60 --per-day 20000 --concurrent 1

  # 列出（永远只显示前缀，不泄露明文）
  sudo python3 manage_api_keys.py list

  # 改配额（不换 key，调用方无感）
  sudo python3 manage_api_keys.py set --key pdk_xxx --per-min 120 --per-day 50000

  # 限 IP / 取消限 IP
  sudo python3 manage_api_keys.py set --key pdk_xxx --ips 1.2.3.4,5.6.7.8
  sudo python3 manage_api_keys.py set --key pdk_xxx --ips any

  # 停用 / 启用 / 删除
  sudo python3 manage_api_keys.py disable --key pdk_xxx
  sudo python3 manage_api_keys.py enable  --key pdk_xxx
  sudo python3 manage_api_keys.py remove  --key pdk_xxx        # 永久删除（不可恢复）

  # 看某个 key 最近用量
  sudo python3 manage_api_keys.py usage --key pdk_xxx

约定：
- key 形如 pdk_<22位随机>；明文不落盘（keys.json 里只存同一字符串，靠 600 权限保护）。
- 写文件用"临时文件 + os.replace"原子替换，网关不会读到半截 JSON。
- keys.json 权限自动收紧到 600。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path

DEFAULT_PATH = "/etc/paodekuai-api/keys.json"
KEY_PREFIX = "pdk_"
KEY_BODY_LEN = 22


def load(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {"keys": {}, "defaults": {"per_min": 60, "per_day": 20000, "concurrent": 1}}
    return json.loads(p.read_text(encoding="utf-8"))


def save(path: str, data: dict) -> None:
    """原子写 + 权限 600 + **属主对齐网关进程属主**。

    ⚠ 属主这一行是踩过坑的：网关以 ubuntu 身份运行，若本工具被 sudo 跑，
    os.replace 出来的文件会变成 root:root，ubuntu 网关就再也读不到（热加载静默失败，
    新 key 一律 401，错误日志是 "[gateway] keys 读取失败: Permission denied"）。
    这里显式继承网关进程的属主（取不到就退回文件原属主），避免"加了 key 却用不了"。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        uid, gid = p.stat().st_uid, p.stat().st_gid
    except OSError:
        uid = gid = -1
    if uid == 0:                                   # 现文件属 root ⇒ 尝试改成网关进程属主
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                cl = (proc / "cmdline").read_bytes().decode("utf-8", "replace")
                if "api_gateway.py" not in cl:
                    continue
                st = (proc / "status").read_text(encoding="utf-8")
                uid = gid = -1
                for line in st.splitlines():
                    if line.startswith("Uid:"):
                        uid = int(line.split()[1])
                    elif line.startswith("Gid:"):
                        gid = int(line.split()[1])
                break
            except (OSError, ValueError):
                continue
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    if uid >= 0 and gid >= 0:
        try:
            os.chown(tmp, uid, gid)
        except (OSError, PermissionError):
            pass
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(KEY_BODY_LEN)[:KEY_BODY_LEN]


def find_key(data: dict, key_or_prefix: str) -> str | None:
    """允许用完整 key 或前缀（≥6 位）定位，避免把明文 key 敲进命令行历史。"""
    keys = data.get("keys") or {}
    if key_or_prefix in keys:
        return key_or_prefix
    hits = [k for k in keys if k.startswith(key_or_prefix)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        print(f"✗ 前缀 {key_or_prefix!r} 匹配到 {len(hits)} 个 key，请写得更长", file=sys.stderr)
        return None
    return None


def resolve(path: str, key_or_prefix: str) -> str | None:
    data = load(path)
    k = find_key(data, key_or_prefix)
    if not k:
        print(f"✗ 没找到 key（{key_or_prefix}）", file=sys.stderr)
    return k


def mask(k: str) -> str:
    return k[:8] + "…" if len(k) > 10 else k[:4] + "…"


def cmd_add(a) -> int:
    data = load(a.keys)
    keys = data.setdefault("keys", {})
    k = new_key()
    while k in keys:                                    # 极小概率碰撞
        k = new_key()
    ips = [] if not a.ips or a.ips.lower() == "any" else [x.strip() for x in a.ips.split(",") if x.strip()]
    keys[k] = {
        "name": a.name,
        "per_min": a.per_min,
        "per_day": a.per_day,
        "concurrent": a.concurrent,
        "allowed_ips": ips,
    }
    if a.disabled:
        keys[k]["disabled"] = True
    save(a.keys, data)
    print("=" * 64)
    print("✅ Key 已创建并立即生效（网关按 mtime 热加载，无需重启）")
    print(f"   Key      : {k}")
    print(f"   名称     : {a.name}")
    print(f"   配额     : {a.per_min}/分钟  {a.per_day}/天  并发 {a.concurrent}")
    print(f"   IP 限制  : {', '.join(ips) if ips else '不限'}")
    if a.disabled:
        print("   状态     : 已停用（enable 可启用）")
    print("=" * 64)
    print("⚠️  明文只显示这一次（本工具不保存明文副本），请立刻保存并发给调用方。")
    print(f"   调用方式 : curl -H 'X-API-Key: {k}' https://chinesetestsite.com/pdk-ai/v1/decide")
    return 0


def cmd_list(a) -> int:
    data = load(a.keys)
    keys = data.get("keys") or {}
    if not keys:
        print("（还没有任何 key）")
        return 0
    print("%-14s %-18s %8s %9s %5s %-8s %s" % ("KEY", "名称", "每分钟", "每天", "并发", "状态", "IP 限制"))
    print("-" * 92)
    for k, v in keys.items():
        ips = v.get("allowed_ips") or []
        print("%-14s %-18s %8s %9s %5s %-8s %s" % (
            mask(k), (v.get("name") or "-")[:18], v.get("per_min", "-"), v.get("per_day", "-"),
            v.get("concurrent", "-"), "停用" if v.get("disabled") else "启用",
            ",".join(ips) if ips else "不限"))
    print("-" * 92)
    print("共 %d 个 key。用 `set --key <前缀>` 可改配额；`remove` 永久删除。" % len(keys))
    return 0


def cmd_set(a) -> int:
    k = resolve(a.keys, a.key)
    if not k:
        return 1
    data = load(a.keys)
    v = data["keys"][k]
    for field in ("name", "per_min", "per_day", "concurrent"):
        val = getattr(a, field)
        if val is not None:
            v[field] = val
    if a.ips is not None:
        v["allowed_ips"] = [] if a.ips.lower() == "any" else [x.strip() for x in a.ips.split(",") if x.strip()]
    save(a.keys, data)
    print(f"✅ 已更新 {mask(k)}：{json.dumps(v, ensure_ascii=False)}（立即生效）")
    return 0


def _toggle(a, disabled: bool, label: str) -> int:
    k = resolve(a.keys, a.key)
    if not k:
        return 1
    data = load(a.keys)
    if disabled:
        data["keys"][k]["disabled"] = True
    else:
        data["keys"][k].pop("disabled", None)
    save(a.keys, data)
    print(f"✅ {mask(k)} 已{label}（立即生效）")
    return 0


def cmd_disable(a) -> int:
    return _toggle(a, True, "停用")


def cmd_enable(a) -> int:
    return _toggle(a, False, "启用")


def cmd_remove(a) -> int:
    k = resolve(a.keys, a.key)
    if not k:
        return 1
    if not a.yes:
        print(f"将永久删除 {mask(k)}（{load(a.keys)['keys'][k].get('name')}）。确认请加 --yes")
        return 1
    data = load(a.keys)
    data["keys"].pop(k, None)
    save(a.keys, data)
    print(f"🗑️  已删除 {mask(k)}（不可恢复；如需停用请用 disable）")
    return 0


def cmd_usage(a) -> int:
    """从网关审计日志统计该 key 近 24h 用量（日志不记明文 key，只记名称）。"""
    k = resolve(a.keys, a.key)
    if not k:
        return 1
    name = (load(a.keys)["keys"][k].get("name") or "")
    log = Path(a.log)
    if not log.exists():
        print(f"（审计日志不存在：{log}）")
        return 0
    since = time.time() - 86400
    n = ok = 0
    per_iface = {}
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if name and name not in line:
            continue
        parts = line.split()
        # 找时间戳与状态码：格式为 "<iso时间> key=... name=... path=... status=..."
        if len(parts) < 2:
            continue
        ts = parts[0]
        try:
            when = time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            continue
        if when < since:
            continue
        n += 1
        for p in parts:
            if p.startswith("status="):
                try:
                    st = int(p.split("=", 1)[1])
                except ValueError:
                    continue
                ok += (200 <= st < 300)
            if p.startswith("path="):
                iface = p.split("=", 1)[1]
                per_iface[iface] = per_iface.get(iface, 0) + 1
    print(f"{mask(k)} ({name}) 近 24h：{n} 次调用，成功 {ok}，失败 {n - ok}")
    for iface, c in sorted(per_iface.items(), key=lambda x: -x[1]):
        print(f"   {iface:24s} {c}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="跑得快 AI 对外 API Key 管理")
    ap.add_argument("--keys", default=DEFAULT_PATH, help=f"keys.json 路径（默认 {DEFAULT_PATH}）")
    ap.add_argument("--log", default="/var/log/paodekuai-api.log", help="审计日志（usage 子命令用）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="生成新 key")
    p.add_argument("--name", required=True, help="调用方名称（审计日志里显示这个）")
    p.add_argument("--per-min", type=int, default=60)
    p.add_argument("--per-day", type=int, default=20000)
    p.add_argument("--concurrent", type=int, default=1)
    p.add_argument("--ips", default="", help="逗号分隔的 IP 白名单；any 或留空 = 不限")
    p.add_argument("--disabled", action="store_true", help="创建后先不启用")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("list", help="列出所有 key（脱敏）")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("set", help="修改配额/名称/IP")
    p.add_argument("--key", required=True, help="完整 key 或前缀（≥6 位）")
    p.add_argument("--name")
    p.add_argument("--per-min", type=int)
    p.add_argument("--per-day", type=int)
    p.add_argument("--concurrent", type=int)
    p.add_argument("--ips", help="逗号分隔；any = 不限")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("disable", help="停用（可随时 enable 回来）")
    p.add_argument("--key", required=True)
    p.set_defaults(func=cmd_disable)

    p = sub.add_parser("enable", help="启用")
    p.add_argument("--key", required=True)
    p.set_defaults(func=cmd_enable)

    p = sub.add_parser("remove", help="永久删除（不可恢复）")
    p.add_argument("--key", required=True)
    p.add_argument("--yes", action="store_true", help="确认删除")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("usage", help="近 24h 用量（查审计日志）")
    p.add_argument("--key", required=True)
    p.set_defaults(func=cmd_usage)

    a = ap.parse_args()
    if not Path(a.keys).exists() and a.cmd in ("list", "add"):
        Path(a.keys).parent.mkdir(parents=True, exist_ok=True)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
