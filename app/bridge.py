# -*- coding: utf-8 -*-
"""模块：本地 RSS 桥子进程管理。职责：拉起/探测/停止/重启
    third_party/china-finance-rss/server.py（默认端口 8053）；
    健康探测走 /healthz；桥挂了自动重启，进程内累计最多 2 次。
    关键依赖：stdlib subprocess/urllib、app.config。"""
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .config import ROOT, settings

# 桥脚本路径与健康检查地址
BRIDGE_SCRIPT = ROOT / "third_party" / "china-finance-rss" / "server.py"
MAX_RESTARTS = 2          # 进程内累计最多自动重启 2 次
PROBE_TIMEOUT = 3.0       # 单次健康探测超时（秒）
START_WAIT = 0.4          # 启动后每次探测间隔（秒）
START_TRIES = 15          # 最多探测次数（约 6 秒内确认起来）


class RssBridge:
    """管理 china-finance-rss 子进程的生命周期。"""

    def __init__(self, port: int = None, script: Path = None):
        self.port = port if port is not None else settings.rss_bridge_port
        self.script = Path(script) if script else BRIDGE_SCRIPT
        self.proc = None
        self.restarts = 0

    # ---------- 探测 ----------
    def is_alive(self) -> bool:
        """桥是否健康：GET /healthz 返回 200 即认为可用。"""
        if self.proc is None or self.proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/healthz", timeout=PROBE_TIMEOUT) as resp:
                return resp.status == 200
        except OSError:
            return False

    # ---------- 启动 ----------
    def start(self) -> bool:
        """拉起桥进程并等待健康。失败给出中文原因并返回 False。"""
        if self.is_alive():
            return True
        if self.proc is not None and self.proc.poll() is None:
            print(f"[bridge] 端口 {self.port} 已有进程响应，直接复用。")
            return True
        if not self.script.exists():
            print(f"[bridge] 错误：找不到桥脚本 {self.script}")
            print(f"[bridge] 怎么办：确认 third_party/china-finance-rss/server.py 存在。")
            return False

        env = dict(os.environ)
        env["PORT"] = str(self.port)
        try:
            self.proc = subprocess.Popen(
                [sys.executable, str(self.script)],
                env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            )
        except OSError as e:
            print(f"[bridge] 错误：桥进程启动失败（{e}）")
            return False

        for _ in range(START_TRIES):
            if self.is_alive():
                print(f"[bridge] RSS 桥已启动：http://127.0.0.1:{self.port}")
                return True
            if self.proc.poll() is not None:
                tail = self._tail()
                print(f"[bridge] 错误：桥进程退出（码 {self.proc.returncode}）。输出：{tail}")
                return False
            time.sleep(START_WAIT)
        tail = self._tail()
        print(f"[bridge] 错误：桥 {self.port} 启动超时，未通过健康检查。输出：{tail}")
        return False

    def ensure(self) -> bool:
        """看护：桥不在则自动重启，进程内累计最多 MAX_RESTARTS 次。"""
        if self.is_alive():
            return True
        if self.restarts >= MAX_RESTARTS:
            return False
        self.restarts += 1
        print(f"[bridge] 桥进程不在，自动重启（第 {self.restarts}/{MAX_RESTARTS} 次）…")
        self.stop(quiet=True)
        return self.start()

    # ---------- 停止 ----------
    def stop(self, quiet: bool = False):
        """停止桥子进程（Windows 直接 terminate，随后 wait 回收）。"""
        if self.proc is None:
            return
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.proc.kill()
                except OSError:
                    pass
        self.proc = None
        if not quiet:
            print(f"[bridge] RSS 桥已停止。")

    # ---------- 内部 ----------
    def _tail(self) -> str:
        """读子进程最后几行输出用于排错。"""
        if self.proc is None or self.proc.stdout is None:
            return ""
        lines = []
        try:
            self.proc.stdout.flush()
            lines = self.proc.stdout.read().splitlines()
        except OSError:
            pass
        return " | ".join(lines[-3:])


# 进程内单例（run.py 与 API 共用同一看护状态）
bridge = RssBridge()
