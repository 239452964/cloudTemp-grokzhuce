"""
一键安装 / 校验 Turnstile Solver 依赖（含 camoufox 浏览器）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent

PACKAGES = [
    "quart>=0.19",
    "rich>=13",
    "camoufox[geoip]",
    "patchright",
]


def run(cmd: list[str], env: dict | None = None) -> int:
    print(">", " ".join(cmd))
    return subprocess.call(cmd, env=env)


def _is_camoufox_ready() -> bool:
    try:
        from camoufox.pkgman import installed_verstr  # type: ignore

        ver = installed_verstr()
        if ver:
            print(f"[*] camoufox 浏览器已安装: {ver}")
            return True
    except Exception:
        pass
    return False


def _env_without_broken_proxy() -> dict:
    """复制环境变量；若本地代理端口不通则去掉代理，避免 GitHub 拉取超时。"""
    env = os.environ.copy()
    proxy_keys = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    )
    proxies = {k: env.get(k) for k in proxy_keys if env.get(k)}
    if not proxies:
        return env

    # 探测常见本机代理端口是否可用
    import socket
    from urllib.parse import urlparse

    def port_open(url: str) -> bool:
        try:
            u = urlparse(url if "://" in url else f"http://{url}")
            host = u.hostname or "127.0.0.1"
            port = u.port or 80
            with socket.create_connection((host, port), timeout=1.5):
                return True
        except Exception:
            return False

    sample = next(iter(proxies.values()))
    if port_open(sample):
        print(f"[*] 使用代理: {sample}")
        return env

    print(f"[!] 检测到代理不可用: {sample}")
    print("[!] 已临时取消代理环境变量，直连拉取（仅本次进程）")
    for k in proxy_keys:
        env.pop(k, None)
    # 保留 NO_PROXY
    return env


def fetch_camoufox() -> int:
    if _is_camoufox_ready():
        print("[*] 跳过 camoufox fetch")
        return 0

    env = _env_without_broken_proxy()
    print("\n[*] 拉取 camoufox 浏览器（约 530MB，可能较久）...")
    code = run([sys.executable, "-m", "camoufox", "fetch"], env=env)
    if code == 0:
        return 0

    print("[-] camoufox fetch 失败")
    print("    常见原因: 系统代理 127.0.0.1:7897 未启动，或 GitHub 无法访问")
    print("    可手动重试:")
    print("      set HTTP_PROXY=")
    print("      set HTTPS_PROXY=")
    print("      set ALL_PROXY=")
    print("      python -m camoufox fetch")
    print("    若必须走代理，请先打开代理软件，再执行上述命令")
    return code


def main() -> int:
    print("=" * 60)
    print("Turnstile Solver 环境安装")
    print("=" * 60)

    code = run([sys.executable, "-m", "pip", "install", "-U", *PACKAGES])
    if code != 0:
        print("[-] pip 安装失败")
        return code

    code = fetch_camoufox()
    if code != 0:
        return code

    # optional chromium for fallback browser_type
    print("\n[*] 安装 patchright chromium（备用浏览器，可选）...")
    run([sys.executable, "-m", "patchright", "install", "chromium"], env=_env_without_broken_proxy())

    print("\n[*] 依赖检查:")
    from solver_manager import check_dependencies, status

    deps = check_dependencies()
    print(deps)
    if not deps.get("ok"):
        print("[-] 仍有缺失:", deps.get("missing"))
        return 1

    print("\n[+] Solver 依赖就绪")
    print("启动: python solver_manager.py start")
    print("或:   TurnstileSolver.bat")
    print("状态: python solver_manager.py status")
    print("当前:", status().get("message"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
