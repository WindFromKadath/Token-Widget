"""custom_script 执行器（DESIGN.md §4.2）。

协议与 CC Switch 一致：执行 providers.meta.usage_script.code（形如
`({ request: {...}, extractor: function(response){...} })`）。

- 占位符替换在 Python 侧对 code 做字符串级替换（§4.2 步骤 2，含 #2954 变体）
- 优先系统 Node.js（node --version 探测）；无 Node 时该类整体降级 unsupported
- runner.js 以子进程执行：stdout = extractor 返回值 JSON；非零退出 = 失败
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ..credentials import Credentials
from ..models import QuotaResult, QuotaWindow
from .base import CollectorError, clamp_percent, parse_num, redact_secrets

RUNNER_PATH = Path(__file__).parent / "runner.js"

# §4.2：{{apiKey}} 及兼容变体（CC Switch #2954：含空格与 snake_case）
_PLACEHOLDER_NAMES = {
    "apiKey": ("apiKey", "api_key"),
    "baseUrl": ("baseUrl", "base_url"),
    "accessToken": ("accessToken", "access_token"),
    "userId": ("userId", "user_id"),
}
_PLACEHOLDERS = {
    field: tuple(
        f"{{{{{variant}}}}}"
        for name in names
        for variant in (name, f" {name} ")
    )
    for field, names in _PLACEHOLDER_NAMES.items()
}


def node_path() -> str | None:
    return shutil.which("node")


def substitute_placeholders(code: str, creds: Credentials) -> str:
    values = {
        "apiKey": creds.api_key or "",
        "baseUrl": creds.base_url or "",
        "accessToken": creds.access_token or "",
        "userId": creds.user_id or "",
    }
    for field, variants in _PLACEHOLDERS.items():
        for variant in variants:
            code = code.replace(variant, str(values[field]))
    return code


def run_script(code: str, timeout_sec: int = 10) -> Any:
    """子进程执行 runner.js，返回 extractor 输出（已 JSON 解析）。

    错误映射（§4.2）：进程非零退出/超时 -> fetch_failed；
    stdout 非 JSON -> script_failed。
    """
    node = node_path()
    if not node:
        raise CollectorError("unsupported", "未检测到 Node.js")

    payload = json.dumps({"code": code, "timeoutSec": timeout_sec})
    try:
        # 脚本经 stdin 传入子进程：占位符替换后含密钥，不落临时文件（§9）
        proc = subprocess.run(
            [node, str(RUNNER_PATH)],
            input=payload.encode("utf-8"),
            capture_output=True,
            timeout=timeout_sec + 5,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CollectorError("fetch_failed", "脚本执行超时") from exc

    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        # stderr 可能回显含密钥的响应体：进 message 前脱敏（§9）
        raise CollectorError("fetch_failed", redact_secrets(stderr[:200]) or "脚本非零退出")
    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    try:
        return json.loads(stdout)
    except ValueError as exc:
        raise CollectorError("script_failed", f"extractor 输出非 JSON: {stdout[:120]}") from exc


def collect(
    plan_id: str,
    code: str,
    creds: Credentials,
    *,
    timeout_sec: int = 10,
    label: str | None = None,
) -> QuotaResult:
    substituted = substitute_placeholders(code, creds)
    out = run_script(substituted, timeout_sec)
    return map_extractor_output(plan_id, out, label)


# ---------------------------------------------------------------- 映射

def map_extractor_output(
    plan_id: str, out: Any, label: str | None = None
) -> QuotaResult:
    """extractor 返回值 -> QuotaResult（§4.2 字段表）。

    返回数组 = 多套餐：逐项映射为独立窗口。
    isValid:false -> ok=False + invalidMessage（不定 error code）。
    """
    base_label = label or plan_id
    entries = out if isinstance(out, list) else [out]
    windows: list[QuotaWindow] = []
    extras: list[str] = []

    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        if entry.get("isValid") is False:
            return QuotaResult(
                plan_id=plan_id,
                ok=False,
                plan_label=base_label,
                fetched_at=int(time.time()),
                message=entry.get("invalidMessage") or "凭据/配置失效",
            )
        percent = _compute_percent(entry)
        if percent is None:
            remaining = parse_num(entry.get("remaining"))
            if remaining is None:
                continue
            # 余额类：只有剩余金额（无总量/百分比）——percent=None 的余额窗口
            name = entry.get("planName") or (
                base_label if len(entries) == 1 else f"{base_label} {idx + 1}"
            )
            windows.append(
                QuotaWindow(
                    key="balance",
                    label=str(name),
                    used_percent=None,
                    used_text=_balance_text(entry, remaining),
                )
            )
            if entry.get("extra"):
                extras.append(str(entry["extra"]))
            continue
        name = entry.get("planName") or (
            base_label if len(entries) == 1 else f"{base_label} {idx + 1}"
        )
        # 显式 None 合并：resetInSec=0 是合法值（刚重置），不能用 `or` 吞掉
        reset_raw = entry.get("resetInSec")
        if reset_raw is None:
            reset_raw = entry.get("reset_in_sec")
        windows.append(
            QuotaWindow(
                key=str(idx) if len(entries) > 1 else "quota",
                label=str(name),
                used_percent=clamp_percent(percent),
                reset_in_sec=_int_of(reset_raw),
                used_text=_used_text(entry),
            )
        )
        if entry.get("extra"):
            extras.append(str(entry["extra"]))

    if not windows:
        raise CollectorError("parse_failed", "extractor 未返回可映射的额度字段")
    return QuotaResult(
        plan_id=plan_id,
        ok=True,
        windows=windows,
        plan_label=base_label,
        extra=" | ".join(extras) if extras else None,
        fetched_at=int(time.time()),
    )


def _compute_percent(entry: dict) -> float | None:
    """remaining/used/total + unit -> 已用百分比。"""
    unit = str(entry.get("unit") or "").strip().lower()
    total = parse_num(entry.get("total"))
    used = parse_num(entry.get("used"))
    remaining = parse_num(entry.get("remaining"))
    # 显式 None 合并：percent=0（刚重置的窗口）不能用 `or` 吞掉
    percent_raw = entry.get("percent")
    if percent_raw is None:
        percent_raw = entry.get("usedPercent")
    percent = parse_num(percent_raw)

    if unit == "%":
        # 百分比类额度：数值本身即百分比；只有 remaining 时按剩余百分比换算
        if percent is not None:
            return percent
        if used is not None:
            return used
        if remaining is not None:
            return 100.0 - remaining
        return None
    if total and total > 0:
        if used is None and remaining is not None:
            used = total - remaining
        if used is not None:
            return used / total * 100.0
    if percent is not None:
        return percent
    return None


def _used_text(entry: dict) -> str | None:
    unit = str(entry.get("unit") or "").strip()
    total = parse_num(entry.get("total"))
    used = parse_num(entry.get("used"))
    remaining = parse_num(entry.get("remaining"))
    if used is None and remaining is not None and total:
        used = total - remaining
    if total is not None and used is not None:
        prefix = "$" if unit.upper() == "USD" else ""
        return f"{prefix}{used:.2f} / {prefix}{total:.2f}"
    return None


def _balance_text(entry: dict, remaining: float) -> str:
    """余额类金额展示：USD -> $x.xx，CNY -> ¥x.xx，其余带单位。"""
    unit = str(entry.get("unit") or "").strip()
    if unit.upper() == "USD":
        return f"${remaining:.2f}"
    if unit.upper() == "CNY":
        return f"¥{remaining:.2f}"
    if unit:
        return f"{remaining:.2f} {unit}"
    return f"{remaining:.2f}"


def _int_of(value) -> int | None:
    n = parse_num(value)
    return int(n) if n is not None else None
