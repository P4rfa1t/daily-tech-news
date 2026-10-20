#!/usr/bin/env python
"""CI 守卫：判断今天的日报是否已由 GitHub Actions 发出，决定本地计划任务要不要跑。

背景：项目迁到 GitHub Actions 后，本机的 Windows 计划任务还在，
两边同时跑就会一天发两封邮件（2026-10-20 就发了两封）。
本机任务由管理员权限创建，没法直接禁用/删除，所以改成在执行入口
(run_news.bat) 里先问一句"CI 今天发过了吗"。

判定顺序（任一命中即视为已处理，直接跳过）：
  1. 本地 cache/send_state.json 里 last_sent_date 是今天
  2. 远端 origin/main 的 cache/send_state.json 里 last_sent_date 是今天
     （Actions 跑完会把缓存 commit 回仓库）
  3. 今天有成功的 Actions 运行（定时或手动）
  4. 今天有正在跑 / 排队的 Actions 运行（等它跑完，别抢跑）

退出码：
  0 -> 今天已处理，本地任务应当跳过
  1 -> 今天还没人处理（CI 没跑或跑挂了），本地任务应当执行
  2 -> 判定不了（网络/凭据问题）-> 保守起见按"已处理"处理，不发信

只用标准库。
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = "P4rfa1t/daily-tech-news"
WORKFLOW = "daily-news.yml"
CST = timezone(timedelta(hours=8))  # 固定 UTC+8，不依赖本机有没有 tzdata

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(msg: str) -> None:
    print(f"[ci-guard] {msg}", flush=True)


def _today() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


def _read_env() -> dict:
    """从 .env 读 GITHUB_TOKEN（不覆盖已有环境变量）。"""
    env = dict(os.environ)
    path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    env.setdefault(key.strip(), val.strip())
        except OSError:
            pass
    return env


def _local_state_date() -> str:
    path = os.path.join(BASE_DIR, "cache", "send_state.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return str(json.load(fh).get("last_sent_date") or "")
    except (OSError, ValueError):
        return ""


def _remote_state_date(token: str) -> str:
    """读远端 main 分支上的 cache/send_state.json。

    不用 `git show origin/main:...`——本机的 origin/main 引用不一定是最新的
    （要先 fetch），而 Actions 每次跑完会把缓存 commit 回仓库，直接读
    GitHub 上的文件最准。
    """
    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/contents/cache/send_state.json?ref=main",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "daily-tech-news-ci-guard",
        },
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        import base64

        raw = base64.b64decode(data["content"]).decode("utf-8")
        return str(json.loads(raw).get("last_sent_date") or "")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError, KeyError):
        return ""


def _fetch_runs(token: str) -> list[dict]:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}/runs?per_page=20",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "daily-tech-news-ci-guard",
        },
    )
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("workflow_runs", []) or []


def _judge_runs(runs: list[dict], today: str) -> tuple[bool, str]:
    """返回 (是否应跳过, 说明)。"""
    pending = False
    for run in runs:
        created = str(run.get("created_at") or "")
        if not created:
            continue
        try:
            day = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(CST).strftime("%Y-%m-%d")
        except ValueError:
            continue
        if day != today:
            continue
        status = str(run.get("status") or "")
        conclusion = str(run.get("conclusion") or "")
        event = str(run.get("event") or "")
        label = f"run #{run.get('run_number')} ({event}, status={status}, conclusion={conclusion or '-'})"
        if status in ("queued", "in_progress", "waiting", "pending", "requested"):
            pending = True
            log(f"今天有正在排队的运行：{label}")
        elif conclusion == "success":
            log(f"今天已有成功的运行：{label}")
            return True, f"Actions 今日已成功运行（{label}）"
    if pending:
        return True, "Actions 今日的运行还在进行中，本地不抢跑"
    return False, "今天没有成功的 Actions 运行"


def main() -> int:
    today = _today()
    log(f"日期（UTC+8）：{today}")

    local = _local_state_date()
    if local == today:
        log(f"本地 cache/send_state.json 显示今天已发过（{local}）")
        return 0

    token = _read_env().get("GITHUB_TOKEN", "")
    if not token:
        log("没有 GITHUB_TOKEN，无法查询远端状态；按保守策略跳过本地发送")
        return 2

    remote = _remote_state_date(token)
    if remote == today:
        log(f"远端 main 的 send_state 显示今天已发过（{remote}）")
        return 0

    try:
        runs = _fetch_runs(token)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log(f"查询 Actions 状态失败：{exc}；按保守策略跳过本地发送")
        return 2

    skip, reason = _judge_runs(runs, today)
    if skip:
        log(f"跳过本地执行：{reason}")
        return 0

    log("今天 CI 没有跑成（或没跑），本地任务继续执行。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
