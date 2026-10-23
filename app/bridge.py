# -*- coding: utf-8 -*-
"""模块：本地 RSS 桥子进程管理。职责：拉起/探测/停止/重启
    third_party/china-finance-rss/server.py（默认端口 8053）；
    健康探测走 /healthz；桥挂了自动重启，进程内累计最多 2 次。
    关键依赖：stdlib subprocess/urllib、app.config。"""
import os
import socket
import subprocess
import threading
import sys
import time
import urllib.request
from pathlib import Path

from .config import ROOT, settings

# 桥脚本路径与健康检查地址
BRIDGE_SCRIPT = ROOT / "third_party" / "china-finance-rss" / "server.py"
MAX_RESTARTS = 5          # 进程内累计最多自动重启 5 次
PROBE_TIMEOUT = 8.0       # 单次健康探测超时（秒）：留足慢上游（10s 读超时）的余量
START_WAIT = 0.4          # 启动后每次探测间隔（秒）
START_TRIES = 15          # 最多探测次数（约 6 秒内确认起来）


class RssBridge:
    """管理 china-finance-rss 子进程的生命周期。"""

    def __init__(self, port: int = None, script: Path = None):
        self.port = port if port is not None else settings.rss_bridge_port
        self.script = Path(script) if script else BRIDGE_SCRIPT
        self.proc = None
        self.restarts = 0
        self._fail_streak = 0        # 连续探测失败次数（避免偶发慢就杀进程）

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
    def _port_owner_pid(self):
        """找出监听本桥端口的 PID（Windows 用 netstat，macOS/Linux 用 lsof）。"""
        try:
            if os.name == "nt":
                out = subprocess.check_output(["netstat", "-ano", "-p", "tcp"],
                                              text=True, timeout=8, errors="ignore")
                for line in out.splitlines():
                    parts = line.split()
                    if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(":%d" % self.port):
                        try:
                            return int(parts[4])
                        except ValueError:
                            return None
                return None
            out = subprocess.check_output(["lsof", "-ti", "tcp:%d" % self.port, "-sTCP:LISTEN"],
                                          text=True, timeout=8, errors="ignore")
            for line in out.split():
                try:
                    return int(line.strip())
                except ValueError:
                    continue
            return None
        except (OSError, subprocess.SubprocessError):
            return None

    def _pid_is_our_bridge(self, pid: int) -> bool:
        """确认该 PID 是 china-finance-rss 桥（避免误杀其它程序）。"""
        try:
            if os.name == "nt":
                out = subprocess.check_output(
                    ["wmic", "process", "where", "ProcessId=%d" % pid, "get", "CommandLine", "/value"],
                    text=True, timeout=10, errors="ignore")
            else:
                out = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="],
                                              text=True, timeout=10, errors="ignore")
            return "china-finance-rss" in out
        except (OSError, subprocess.SubprocessError):
            return False

    def _clear_stale_port(self) -> bool:
        """端口被僵死的旧桥占用时清掉它（只清本项目的桥，跨平台）。"""
        pid = self._port_owner_pid()
        if not pid or pid == (self.proc.pid if self.proc else -1):
            return False
        if not self._pid_is_our_bridge(pid):
            print(f"[bridge] 端口 {self.port} 被其它进程占用（pid={pid}），不自动清理", flush=True)
            return False
        print(f"[bridge] 发现僵死的旧桥进程 pid={pid}，清理后重启", flush=True)
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=10)
            else:
                os.kill(pid, 9)
            time.sleep(0.6)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def _port_free(self, port: int) -> bool:
        """本机 127.0.0.1 上是否可以绑定该端口。"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # Windows 上 SO_REUSEADDR 允许"抢绑"正在监听的端口，会误判为空闲；
            # 用 SO_EXCLUSIVEADDRUSE 才是真正的独占探测（POSIX 用 SO_REUSEADDR 避开 TIME_WAIT）
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    def _pick_port(self) -> int:
        """优先用配置端口；被占用则向后找空位（最多 +20），都满就让系统分配。"""
        preferred = settings.rss_bridge_port
        for port in range(preferred, preferred + 21):
            if self._port_free(port):
                return port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]
        finally:
            sock.close()

    def start(self) -> bool:
        """拉起桥进程并等待健康。失败给出中文原因并返回 False。"""
        if self.is_alive():
            return True
        if self.proc is not None and self.proc.poll() is None:
            print(f"[bridge] 端口 {self.port} 已有进程响应，直接复用。")
            return True
        # 选端口：首选端口已有健康桥则复用；否则挑一个空闲端口（不写死 8053）
        preferred = settings.rss_bridge_port
        self.port = preferred
        if self.is_alive():
            return True
        self.port = self._pick_port()
        if self.port != preferred:
            print(f"[bridge] 端口 {preferred} 被占用，改用 {self.port}", flush=True)

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
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            )
            try:                          # 写 pid 文件，供停止脚本精确结束（端口是自动分配的）
                pid_file = Path(__file__).resolve().parent.parent / "data" / "logs" / "bridge.pid"
                pid_file.parent.mkdir(parents=True, exist_ok=True)
                pid_file.write_text(str(self.proc.pid), encoding="utf-8")
            except OSError:
                pass
            self._tail_lines = []
            threading.Thread(target=self._drain, args=(self.proc,), daemon=True).start()
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

    def _drain(self, proc):
        """持续读取子进程输出（只保留最后 40 行）；不读会导致 PIPE 写满、桥卡死。"""
        try:
            for line in iter(proc.stdout.readline, ""):
                self._tail_lines.append(line.rstrip())
                if len(self._tail_lines) > 40:
                    del self._tail_lines[:-40]
        except (OSError, ValueError):
            pass

    def ensure(self) -> bool:
        """看护：桥不在则自动重启，进程内累计最多 MAX_RESTARTS 次。"""
        if self.is_alive():
            self._fail_streak = 0
            return True
        # 单次探测失败可能只是桥正在抓慢上游（读超时 10s），先观察一次再动手，避免误杀
        self._fail_streak += 1
        if self._fail_streak < 2:
            print("[bridge] 探测失败 1 次，暂不重启（可能是慢请求）", flush=True)
            return False
        self._fail_streak = 0
        # 进程还在但探测失败（僵死/被挂起）→ 强制干掉再重启
        if self.proc and self.proc.poll() is None:
            print("[bridge] 桥进程存在但不响应，强制重启", flush=True)
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
            self.proc = None
            time.sleep(0.5)
            self._clear_stale_port()
        if self.restarts >= MAX_RESTARTS:
            return False
        self.restarts += 1
        print(f"[bridge] 桥进程不在，自动重启（第 {self.restarts}/{MAX_RESTARTS} 次）…")
        self.stop(quiet=True)
        return self.start()

    # ---------- 停止 ----------
    def _clear_pid_file(self):
        try:
            pid_file = Path(__file__).resolve().parent.parent / "data" / "logs" / "bridge.pid"
            if pid_file.exists():
                pid_file.unlink()
        except OSError:
            pass

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
