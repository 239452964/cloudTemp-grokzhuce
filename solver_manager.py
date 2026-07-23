"""
Turnstile Solver 进程管理：启动 / 停止 / 健康检查 / 崩溃自愈看门狗。
可被 Web 控制台与 CLI 共用。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.error import URLError, HTTPError
from urllib.request import urlopen

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
PID_FILE = BASE_DIR / "logs" / "turnstile_solver.pid"
LOG_FILE = BASE_DIR / "logs" / "turnstile_solver.log"
STATE_FILE = BASE_DIR / "logs" / "turnstile_solver.state.json"

DEFAULT_SOLVER_URL = "http://127.0.0.1:5072"
DEFAULT_BROWSER = "camoufox"
DEFAULT_THREADS = 4

# 启动/自愈串行，避免多 worker 同时 start 打爆端口
_START_LOCK = threading.Lock()
_WATCHDOG_LOCK = threading.Lock()
_watchdog_stop = threading.Event()
_watchdog_thread: Optional[threading.Thread] = None
_watchdog_log: Optional[Callable[[str, str], None]] = None
_watchdog_interval = 8.0
_last_auto_restart_ts = 0.0
_AUTO_RESTART_COOLDOWN = 20.0


def _ensure_dirs() -> None:
    (BASE_DIR / "logs").mkdir(parents=True, exist_ok=True)


def load_solver_config() -> dict[str, Any]:
    load_dotenv(BASE_DIR / ".env", override=True)
    url = (os.getenv("SOLVER_URL") or DEFAULT_SOLVER_URL).strip().rstrip("/")
    browser = (os.getenv("SOLVER_BROWSER") or DEFAULT_BROWSER).strip() or DEFAULT_BROWSER
    try:
        threads = int(os.getenv("SOLVER_THREADS") or DEFAULT_THREADS)
    except ValueError:
        threads = DEFAULT_THREADS
    threads = max(1, min(threads, 16))
    host = os.getenv("SOLVER_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(os.getenv("SOLVER_PORT") or _port_from_url(url) or 5072)
    except ValueError:
        port = 5072
    debug = (os.getenv("SOLVER_DEBUG") or "1").strip() not in ("0", "false", "False")
    return {
        "url": url,
        "browser": browser,
        "threads": threads,
        "host": host,
        "port": port,
        "debug": debug,
    }


def _port_from_url(url: str) -> Optional[int]:
    try:
        # http://127.0.0.1:5072
        part = url.split("://", 1)[-1]
        if ":" in part:
            return int(part.rsplit(":", 1)[-1].split("/")[0])
    except Exception:
        return None
    return None


def _read_pid() -> Optional[int]:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        return pid if pid > 0 else None
    except Exception:
        return None


def _write_pid(pid: int) -> None:
    _ensure_dirs()
    PID_FILE.write_text(str(pid), encoding="utf-8")


def _write_state(data: dict) -> None:
    _ensure_dirs()
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _clear_pid() -> None:
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except Exception:
        pass


def is_pid_running(pid: Optional[int]) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                text=True,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def port_is_open(host: str, port: int, timeout: float = 0.25) -> bool:
    """
    TCP 探测端口是否可连。

    注意：Solver 在解 Turnstile（Camoufox）时事件循环可能短暂堵死，
    过短 timeout（0.2s）会误判离线。调用方在「进程仍存活」时应放宽超时或二次确认。
    """
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def process_alive_for_port(port: int, pid: Optional[int] = None) -> tuple[bool, Optional[int]]:
    """
    判断 Solver 进程是否仍存活（不依赖瞬时 TCP）。
    返回 (alive, pid)。
    """
    if pid and is_pid_running(pid):
        return True, pid
    listener = find_listening_pid(port)
    if listener and is_pid_running(listener):
        return True, listener
    # Windows 上 netstat 偶发拿不到时，再信 PID 文件
    file_pid = pid or _read_pid()
    if file_pid and is_pid_running(file_pid):
        return True, file_pid
    return False, None


def find_listening_pid(port: int) -> Optional[int]:
    """根据监听端口找回 Solver PID（Windows 上 PID 文件丢失时很有用）。"""
    try:
        if sys.platform == "win32":
            # netstat 中文系统常是 GBK；用 bytes 再解码更稳
            raw = subprocess.check_output(
                ["netstat", "-ano", "-p", "tcp"],
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            out = raw.decode("utf-8", errors="ignore")
            if "LISTENING" not in out.upper() and "监听" not in out:
                out = raw.decode("gbk", errors="ignore")
            needle = f":{port}"
            for line in out.splitlines():
                upper = line.upper()
                # 中文 Windows: 侦听 / LISTENING
                if "LISTENING" not in upper and "侦听" not in line and "監聽" not in line:
                    continue
                if needle not in line:
                    continue
                parts = line.split()
                if len(parts) < 4:
                    continue
                local = parts[1] if len(parts) > 1 else ""
                if not (local.endswith(needle) or f"]:{port}" in local):
                    continue
                try:
                    return int(parts[-1])
                except ValueError:
                    continue
        else:
            out = subprocess.check_output(
                ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            for line in out.splitlines():
                line = line.strip()
                if line.isdigit():
                    return int(line)
    except Exception:
        return None
    return None


# 轮询缓存：避免 /api/status 每次都 tasklist + HTTP 探测把 Flask 堵住
_STATUS_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_STATUS_TTL = 1.5
_DEPS_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_DEPS_TTL = 60.0


def check_http(url: Optional[str] = None, timeout: float = 0.8) -> dict[str, Any]:
    cfg = load_solver_config()
    base = (url or cfg["url"]).rstrip("/")
    result = {
        "url": base,
        "reachable": False,
        "status_code": None,
        "body_preview": "",
        "error": "",
    }
    try:
        with urlopen(base + "/", timeout=timeout) as resp:
            body = resp.read(400).decode("utf-8", errors="ignore")
            result["status_code"] = getattr(resp, "status", 200)
            result["body_preview"] = body[:200]
            result["reachable"] = True
    except HTTPError as e:
        # 某些实现根路径可能 404，但服务已起
        result["status_code"] = e.code
        result["reachable"] = e.code < 500
        result["body_preview"] = str(e.reason)[:200]
    except URLError as e:
        result["error"] = str(e.reason if hasattr(e, "reason") else e)
    except Exception as e:
        result["error"] = str(e)
    return result


def status(force: bool = False, http_timeout: float = 0.8) -> dict[str, Any]:
    now = time.time()
    if (
        not force
        and _STATUS_CACHE["data"] is not None
        and (now - float(_STATUS_CACHE["ts"])) < _STATUS_TTL
    ):
        return _STATUS_CACHE["data"]

    cfg = load_solver_config()
    host = cfg["host"] or "127.0.0.1"
    port = int(cfg["port"] or 5072)

    pid = _read_pid()
    running = is_pid_running(pid)
    if not running and pid:
        # PID 文件过期：清掉，后面用端口/进程再找回
        _clear_pid()
        pid = None

    # TCP 探测：force（看门狗）用更长超时，避免解验证码时假离线
    tcp_timeout = 1.2 if force else 0.5
    tcp_ok = port_is_open(host, port, timeout=tcp_timeout)

    if not running:
        alive, recovered = process_alive_for_port(port, pid)
        if alive and recovered:
            pid = recovered
            running = True
            try:
                _write_pid(recovered)
            except Exception:
                pass
            # 进程在监听端口：即使刚才 TCP 探测失败也再试一次
            if not tcp_ok:
                tcp_ok = port_is_open(host, port, timeout=1.5)

    # 进程仍在但单次 TCP 失败 = 忙/事件循环堵，不是离线
    busy = bool(running and not tcp_ok)
    if busy:
        # 再给一次较长探测机会
        tcp_ok = port_is_open(host, port, timeout=2.0)
        busy = bool(running and not tcp_ok)

    # ready：端口通 或 进程存活（忙也算在线，避免 UI/看门狗误报）
    process_running = bool(running or tcp_ok)
    ready = bool(tcp_ok or running)

    if tcp_ok:
        if force:
            http = check_http(cfg["url"], timeout=max(http_timeout, 1.5))
            if not http.get("reachable"):
                http["reachable"] = True
                http["error"] = http.get("error") or "tcp_ok_http_slow"
        else:
            http = {
                "url": cfg["url"],
                "reachable": True,
                "status_code": None,
                "body_preview": "",
                "error": "",
            }
    elif running:
        # 进程在、端口暂时连不上：标忙碌，不当离线
        http = {
            "url": cfg["url"],
            "reachable": True,  # 对 UI 视为在线
            "status_code": None,
            "body_preview": "",
            "error": "process_busy",
            "busy": True,
        }
    else:
        http = {
            "url": cfg["url"],
            "reachable": False,
            "status_code": None,
            "body_preview": "",
            "error": "port closed",
        }

    deps = check_dependencies()
    data = {
        "configured_url": cfg["url"],
        "browser": cfg["browser"],
        "threads": cfg["threads"],
        "host": host,
        "port": port,
        "pid": pid,
        "process_running": process_running,
        "http_ok": bool(http.get("reachable")),
        "http": http,
        "ready": ready,
        "busy": busy,
        "log_file": str(LOG_FILE.relative_to(BASE_DIR)).replace("\\", "/"),
        "dependencies": deps,
        "message": _status_message(process_running, http, deps, busy=busy),
    }
    _STATUS_CACHE["ts"] = now
    _STATUS_CACHE["data"] = data
    return data


def invalidate_status_cache() -> None:
    _STATUS_CACHE["ts"] = 0.0
    _STATUS_CACHE["data"] = None


def _status_message(
    running: bool, http: dict, deps: dict, *, busy: bool = False
) -> str:
    if not deps.get("ok"):
        return "依赖未就绪: " + ", ".join(deps.get("missing") or [])
    if busy:
        return "Solver 在线（解验证码中/响应慢，非离线）"
    if http.get("reachable") or running:
        return "Solver 在线"
    return "Solver 未运行"


def check_dependencies(force: bool = False) -> dict[str, Any]:
    now = time.time()
    if (
        not force
        and _DEPS_CACHE["data"] is not None
        and (now - float(_DEPS_CACHE["ts"])) < _DEPS_TTL
    ):
        return _DEPS_CACHE["data"]

    missing = []
    details = {}
    for name in ("quart", "rich", "camoufox", "patchright"):
        try:
            __import__(name)
            details[name] = True
        except Exception:
            details[name] = False
            missing.append(name)

    camoufox_browser = False
    camoufox_error = ""
    try:
        from camoufox.pkgman import installed_verstr  # type: ignore

        ver = installed_verstr()
        camoufox_browser = bool(ver)
        details["camoufox_version"] = ver
    except Exception as e:
        # fallback: try launching import path
        try:
            import camoufox  # noqa: F401

            camoufox_browser = True
            details["camoufox_version"] = "unknown"
        except Exception:
            camoufox_error = str(e)
            missing.append("camoufox-browser")

    result = {
        "ok": len(missing) == 0,
        "missing": missing,
        "details": details,
        "camoufox_browser": camoufox_browser,
        "camoufox_error": camoufox_error,
    }
    _DEPS_CACHE["ts"] = now
    _DEPS_CACHE["data"] = result
    return result


def ensure_ready(timeout: float = 120.0) -> dict[str, Any]:
    """
    确保本地 Solver 在线可用（带锁，多 worker 并发安全）。
    - 已 ready：直接返回
    - 进程在但 HTTP 未通：等待
    - 完全离线：自动 start
    """
    st = status(force=True)
    if st.get("ready"):
        return {"ok": True, "message": "Solver 已在线", "started": False, **st}

    with _START_LOCK:
        # 双检：可能别的线程刚拉起来了
        st = status(force=True)
        if st.get("ready"):
            return {"ok": True, "message": "Solver 已在线", "started": False, **st}
        # 已持锁，直接走无锁启动，避免与 start() 死锁
        result = _start_unlocked(wait_ready=True, timeout=timeout)
        result["started"] = True
        return result


def start_watchdog(
    log_fn: Optional[Callable[[str, str], None]] = None,
    interval: float = 8.0,
) -> dict[str, Any]:
    """
    后台看门狗：任务运行期间周期性检测 5072，挂了就自动拉起。
    可重复调用（会更新 log_fn / interval）。
    """
    global _watchdog_thread, _watchdog_log, _watchdog_interval
    with _WATCHDOG_LOCK:
        _watchdog_log = log_fn
        _watchdog_interval = max(3.0, float(interval or 8.0))
        if _watchdog_thread is not None and _watchdog_thread.is_alive():
            _watchdog_stop.clear()
            return {"ok": True, "message": "Solver 看门狗已在运行", "running": True}
        _watchdog_stop.clear()
        _watchdog_thread = threading.Thread(
            target=_watchdog_loop,
            name="SolverWatchdog",
            daemon=True,
        )
        _watchdog_thread.start()
        return {"ok": True, "message": "Solver 看门狗已启动", "running": True}


def stop_watchdog() -> dict[str, Any]:
    """停止看门狗（不停止 Solver 进程本身）。"""
    global _watchdog_thread
    with _WATCHDOG_LOCK:
        _watchdog_stop.set()
        t = _watchdog_thread
        _watchdog_thread = None
    if t is not None and t.is_alive():
        t.join(timeout=2.0)
    return {"ok": True, "message": "Solver 看门狗已停止", "running": False}


def watchdog_running() -> bool:
    t = _watchdog_thread
    return bool(t is not None and t.is_alive() and not _watchdog_stop.is_set())


def _watchdog_emit(message: str, level: str = "warn") -> None:
    fn = _watchdog_log
    if fn:
        try:
            fn(message, level)
        except Exception:
            pass


def _watchdog_loop() -> None:
    global _last_auto_restart_ts
    # 连续 N 次确认「进程真死」才重启，避免解验证码时假离线刷屏
    dead_streak = 0
    while not _watchdog_stop.is_set():
        try:
            st = status(force=True)
            # 进程还在 / 端口通 / 忙碌 = 正常，绝不重启
            if st.get("ready") or st.get("process_running") or st.get("busy"):
                dead_streak = 0
            else:
                dead_streak += 1
                # 至少连续 2 次探测都确认死亡，再拉起（间隔约 8s × 2）
                if dead_streak < 2:
                    _watchdog_emit(
                        "Solver 端口暂不可达，复核中（进程可能正忙，先不重启）…",
                        "info",
                    )
                else:
                    now = time.time()
                    if now - _last_auto_restart_ts < _AUTO_RESTART_COOLDOWN:
                        pass
                    else:
                        _watchdog_emit(
                            "确认 Turnstile Solver 进程已退出，看门狗正在自动拉起…",
                            "warn",
                        )
                        result = ensure_ready(timeout=90.0)
                        _last_auto_restart_ts = time.time()
                        dead_streak = 0
                        if result.get("ok") and (
                            result.get("ready") or result.get("process_running")
                        ):
                            _watchdog_emit(
                                result.get("message")
                                or f"Solver 已自动恢复 (PID={result.get('pid')})",
                                "success",
                            )
                        else:
                            _watchdog_emit(
                                "Solver 自动恢复失败: "
                                + str(result.get("message") or "未知错误")
                                + "（见 logs/turnstile_solver.log）",
                                "error",
                            )
        except Exception as e:
            _watchdog_emit(f"Solver 看门狗异常: {e}", "error")

        # 可中断 sleep
        if _watchdog_stop.wait(timeout=_watchdog_interval):
            break


def start(wait_ready: bool = True, timeout: float = 90.0) -> dict[str, Any]:
    # 外部已可能持锁；此处再拿锁保证 CLI/UI 并发安全
    acquired = _START_LOCK.acquire(blocking=True)
    try:
        return _start_unlocked(wait_ready=wait_ready, timeout=timeout)
    finally:
        if acquired:
            _START_LOCK.release()


def _start_unlocked(wait_ready: bool = True, timeout: float = 90.0) -> dict[str, Any]:
    invalidate_status_cache()
    cfg = load_solver_config()
    deps = check_dependencies()
    if not deps["ok"]:
        return {
            "ok": False,
            "message": "依赖缺失，请先运行: python setup_solver.py",
            "dependencies": deps,
            **status(force=True),
        }

    current = status(force=True)
    if current["ready"]:
        return {"ok": True, "message": "Solver 已在运行", **current}
    if current["process_running"]:
        if wait_ready:
            ready = _wait_http(cfg["url"], timeout=timeout)
            st = status(force=True)
            return {
                "ok": ready,
                "message": "进程已存在，" + ("HTTP 已就绪" if ready else "等待 HTTP 超时"),
                **st,
            }
        return {"ok": True, "message": "进程已存在", **current}

    # 清理僵死 PID，避免误判
    old_pid = _read_pid()
    if old_pid and not is_pid_running(old_pid) and not port_is_open(cfg["host"], int(cfg["port"])):
        _clear_pid()

    _ensure_dirs()
    cmd = [
        sys.executable,
        str(BASE_DIR / "api_solver.py"),
        "--browser_type",
        cfg["browser"],
        "--thread",
        str(cfg["threads"]),
        "--host",
        cfg["host"],
        "--port",
        str(cfg["port"]),
    ]
    if cfg["debug"]:
        cmd.append("--debug")

    log_f = open(LOG_FILE, "a", encoding="utf-8", errors="ignore")
    log_f.write("\n" + "=" * 60 + f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] START {' '.join(cmd)}\n")
    log_f.flush()

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)

    # Windows 默认 GBK，rich/emoji 会崩；强制 UTF-8
    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    # 避免系统坏代理拖死 camoufox 访问 accounts.x.ai（需要代理时再手动开）
    if (os.getenv("SOLVER_USE_SYSTEM_PROXY") or "").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        for k in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "NO_PROXY",
            "no_proxy",
        ):
            child_env.pop(k, None)
    # 进一步避免子进程继承坏代理（camoufox/playwright 也会读）
    child_env["PYTHONUTF8"] = "1"

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdout=log_f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            env=child_env,
        )
    except Exception as e:
        log_f.close()
        invalidate_status_cache()
        return {"ok": False, "message": f"启动失败: {e}", **status(force=True)}

    _write_pid(proc.pid)
    _write_state(
        {
            "pid": proc.pid,
            "cmd": cmd,
            "started_at": time.time(),
            "url": cfg["url"],
            "browser": cfg["browser"],
            "threads": cfg["threads"],
        }
    )
    invalidate_status_cache()

    if not wait_ready:
        return {"ok": True, "message": f"已启动 PID={proc.pid}", **status(force=True)}

    ready = _wait_http(cfg["url"], timeout=timeout)
    # 若进程已退出
    if proc.poll() is not None:
        _clear_pid()
        invalidate_status_cache()
        return {
            "ok": False,
            "message": f"Solver 进程已退出 code={proc.returncode}，请查看 logs/turnstile_solver.log",
            **status(force=True),
        }

    invalidate_status_cache()
    st = status(force=True)
    if ready:
        return {"ok": True, "message": f"Solver 已就绪 (PID={proc.pid})", **st}
    return {
        "ok": False,
        "message": f"已启动 PID={proc.pid}，但 {timeout:.0f}s 内 HTTP 未就绪，请查看日志",
        **st,
    }


def _wait_http(url: str, timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check_http(url, timeout=1.5).get("reachable"):
            invalidate_status_cache()
            return True
        time.sleep(1.0)
    return False


def stop() -> dict[str, Any]:
    invalidate_status_cache()
    pid = _read_pid()
    if not pid or not is_pid_running(pid):
        # 尝试按端口杀掉残留（仅本机 solver 端口）
        _clear_pid()
        st = status(force=True)
        if st["ready"]:
            return {
                "ok": False,
                "message": "检测到 HTTP 在线但无 PID 记录，可能是外部手动启动，请手动关闭对应终端",
                **st,
            }
        return {"ok": True, "message": "Solver 未在运行", **st}

    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            os.kill(pid, signal.SIGTERM)
            time.sleep(1)
            if is_pid_running(pid):
                os.kill(pid, signal.SIGKILL)
    except Exception as e:
        invalidate_status_cache()
        return {"ok": False, "message": f"停止失败: {e}", **status(force=True)}

    # wait exit
    for _ in range(20):
        if not is_pid_running(pid):
            break
        time.sleep(0.25)
    _clear_pid()
    invalidate_status_cache()
    return {"ok": True, "message": f"已停止 PID={pid}", **status(force=True)}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Turnstile Solver 管理器")
    parser.add_argument("action", choices=["status", "start", "stop", "restart", "deps"])
    parser.add_argument("--no-wait", action="store_true", help="start 时不等待 HTTP 就绪")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    if args.action == "deps":
        print(json.dumps(check_dependencies(), ensure_ascii=False, indent=2))
        return
    if args.action == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return
    if args.action == "start":
        print(json.dumps(start(wait_ready=not args.no_wait, timeout=args.timeout), ensure_ascii=False, indent=2))
        return
    if args.action == "stop":
        print(json.dumps(stop(), ensure_ascii=False, indent=2))
        return
    if args.action == "restart":
        stop()
        time.sleep(1)
        print(json.dumps(start(wait_ready=not args.no_wait, timeout=args.timeout), ensure_ascii=False, indent=2))
        return


if __name__ == "__main__":
    main()
