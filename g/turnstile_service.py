"""
Turnstile 验证服务
"""
import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()


class TurnstileService:
    """Turnstile 验证服务类"""

    def __init__(self, solver_url=None):
        self.yescaptcha_key = os.getenv("YESCAPTCHA_KEY", "").strip()
        self.solver_url = (
            (solver_url or os.getenv("SOLVER_URL") or "http://127.0.0.1:5072")
            .strip()
            .rstrip("/")
        )
        self.yescaptcha_api = "https://api.yescaptcha.com"
        self.last_error = ""

    @staticmethod
    def _stopped(stop_event) -> bool:
        return bool(stop_event is not None and stop_event.is_set())

    @staticmethod
    def _interruptible_sleep(seconds: float, stop_event=None) -> bool:
        """
        可中断 sleep。
        返回 True 表示被 stop_event 打断（应立刻退出）。
        """
        if seconds <= 0:
            return TurnstileService._stopped(stop_event)
        if stop_event is None:
            time.sleep(seconds)
            return False
        # Event.wait 在超时前若被 set 会立刻返回 True
        return stop_event.wait(timeout=seconds)

    def _ensure_local_solver(self, stop_event=None) -> None:
        """本地 Solver 挂了时尝试自动拉起（多线程共用锁）。"""
        if self.yescaptcha_key:
            return
        if self._stopped(stop_event):
            raise RuntimeError("stopped")
        try:
            import solver_manager
        except Exception as e:
            raise Exception(f"无法加载 solver_manager: {e}") from e
        result = solver_manager.ensure_ready(timeout=90.0)
        if self._stopped(stop_event):
            raise RuntimeError("stopped")
        if not result.get("ok") or not result.get("ready"):
            raise Exception(
                f"本地 Solver 离线且自动拉起失败: {result.get('message') or result}。"
                "请查看 logs/turnstile_solver.log"
            )

    def create_task(self, siteurl, sitekey, stop_event=None):
        """
        创建 Turnstile 任务，返回 task_id。
        失败抛异常；若 stop_event 已置位，抛 RuntimeError('stopped')。
        """
        self.last_error = ""
        if self._stopped(stop_event):
            raise RuntimeError("stopped")

        if self.yescaptcha_key:
            url = f"{self.yescaptcha_api}/createTask"
            payload = {
                "clientKey": self.yescaptcha_key,
                "task": {
                    "type": "TurnstileTaskProxyless",
                    "websiteURL": siteurl,
                    "websiteKey": sitekey,
                },
            }
            # 短超时，避免点停止后还卡 30s
            response = requests.post(url, json=payload, timeout=8)
            response.raise_for_status()
            data = response.json()
            if data.get("errorId") != 0:
                raise Exception(f"YesCaptcha创建任务失败: {data.get('errorDescription')}")
            return data["taskId"]

        # 本地 Turnstile Solver：创建接口本身应很快返回 taskId
        url = f"{self.solver_url}/turnstile?url={siteurl}&sitekey={sitekey}"
        last_err = None
        for attempt in range(1, 3):
            if self._stopped(stop_event):
                raise RuntimeError("stopped")
            try:
                response = requests.get(url, timeout=8)
                response.raise_for_status()
                data = response.json()
                task_id = data.get("taskId") or data.get("task_id")
                if not task_id:
                    raise Exception(f"Solver 未返回 taskId: {data}")
                return task_id
            except requests.exceptions.ConnectionError as e:
                last_err = e
                # 10061：进程挂了 → 自动拉起再试一次
                try:
                    self._ensure_local_solver(stop_event=stop_event)
                except RuntimeError:
                    raise
                except Exception as revive_err:
                    raise Exception(
                        f"无法连接本地 Solver {self.solver_url}（5072 未监听），"
                        f"自动恢复失败: {revive_err}"
                    ) from e
                continue
            except requests.exceptions.Timeout as e:
                last_err = e
                if attempt == 1:
                    # 可能刚在启动浏览器池，稍等再试
                    if self._interruptible_sleep(2.0, stop_event):
                        raise RuntimeError("stopped")
                    continue
                raise Exception(
                    f"连接 Solver 超时 {self.solver_url}（可能正在启动浏览器池）。"
                    f" 原始错误: {e}"
                ) from e
        raise Exception(
            f"无法连接本地 Solver {self.solver_url}（WinError 10061 表示 5072 未监听）。"
            f" 原始错误: {last_err}"
        )

    def get_response(
        self,
        task_id,
        max_retries=45,
        initial_delay=0.8,
        retry_delay=1.0,
        stop_event=None,
        request_timeout=4,
    ):
        """
        轮询获取 Turnstile token。
        支持 stop_event：点停止后最多再等一个短 HTTP 超时即退出。
        成功返回 token；失败返回 None（错误在 last_error）。
        """
        self.last_error = ""

        if self._interruptible_sleep(initial_delay, stop_event):
            self.last_error = "已停止"
            return None

        for i in range(max_retries):
            if self._stopped(stop_event):
                self.last_error = "已停止"
                return None

            try:
                if self.yescaptcha_key:
                    url = f"{self.yescaptcha_api}/getTaskResult"
                    payload = {
                        "clientKey": self.yescaptcha_key,
                        "taskId": task_id,
                    }
                    response = requests.post(url, json=payload, timeout=request_timeout)
                    response.raise_for_status()
                    data = response.json()

                    if data.get("errorId") != 0:
                        self.last_error = f"YesCaptcha: {data.get('errorDescription')}"
                        return None

                    if data.get("status") == "ready":
                        token = data.get("solution", {}).get("token")
                        if token:
                            return token
                        self.last_error = "YesCaptcha 结果无 token"
                        return None
                    if data.get("status") == "processing":
                        if self._interruptible_sleep(retry_delay, stop_event):
                            self.last_error = "已停止"
                            return None
                        continue
                    self.last_error = f"YesCaptcha 未知状态: {data.get('status')}"
                    if self._interruptible_sleep(retry_delay, stop_event):
                        self.last_error = "已停止"
                        return None
                    continue

                # 本地 Turnstile Solver：短超时，避免 Solver 挂掉时卡 15s+
                url = f"{self.solver_url}/result?id={task_id}"
                response = requests.get(url, timeout=request_timeout)
                response.raise_for_status()
                data = response.json()
                captcha = (data.get("solution") or {}).get("token")
                status = data.get("status") or data.get("value")

                if captcha:
                    if captcha == "CAPTCHA_FAIL":
                        self.last_error = (
                            "Solver CAPTCHA_FAIL（浏览器打不开 accounts.x.ai 或验证失败，"
                            "请检查网络/代理，见 logs/turnstile_solver.log）"
                        )
                        return None
                    return captcha

                if status in ("processing", "pending", None, ""):
                    if self._interruptible_sleep(retry_delay, stop_event):
                        self.last_error = "已停止"
                        return None
                    continue

                self.last_error = f"Solver 状态: {status or data}"
                if self._interruptible_sleep(retry_delay, stop_event):
                    self.last_error = "已停止"
                    return None
            except requests.exceptions.ConnectionError as e:
                if self._stopped(stop_event):
                    self.last_error = "已停止"
                    return None
                # 轮询中途 Solver 崩了：尝试自愈后继续轮询
                try:
                    if not self.yescaptcha_key:
                        self._ensure_local_solver(stop_event=stop_event)
                        self.last_error = f"Solver 断线已尝试恢复，继续轮询: {e}"
                    else:
                        self.last_error = f"轮询异常: {e}"
                except RuntimeError:
                    self.last_error = "已停止"
                    return None
                except Exception as revive_err:
                    self.last_error = f"Solver 断线且恢复失败: {revive_err}"
                if self._interruptible_sleep(min(retry_delay, 1.5), stop_event):
                    self.last_error = "已停止"
                    return None
            except Exception as e:
                if self._stopped(stop_event):
                    self.last_error = "已停止"
                    return None
                self.last_error = f"轮询异常: {e}"
                # Solver 连不上时也别死磕太久：短 sleep 后继续，便于 stop 立刻生效
                if self._interruptible_sleep(min(retry_delay, 1.0), stop_event):
                    self.last_error = "已停止"
                    return None

        if not self.last_error:
            self.last_error = (
                f"等待 Turnstile 超时（约 {initial_delay + max_retries * retry_delay:.0f}s）"
            )
        return None
