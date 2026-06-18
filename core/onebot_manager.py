"""
NapCat 生命周期管理器
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

from utils.logger import setup_logger

logger = setup_logger("onebot_manager")

NAPCAT_DIR = Path("data/napcat")


def is_installed() -> bool:
    """检查 NapCat 是否已安装"""
    for candidate in ["napcat.bat", "napcat.cmd", "napcat.sh",
                      "napcat/napcat.cmd", "napcat/napcat.sh"]:
        if (NAPCAT_DIR / candidate).exists():
            return True
    return (NAPCAT_DIR / "node.exe").exists()


def _get_exe_path() -> Optional[Path]:
    """获取 NapCat 入口路径（优先 node.exe）"""
    if (NAPCAT_DIR / "node.exe").exists():
        return NAPCAT_DIR / "node.exe"
    for candidate in [
        NAPCAT_DIR / "napcat.bat",
        NAPCAT_DIR / "napcat.cmd",
        NAPCAT_DIR / "napcat" / "napcat.cmd",
    ]:
        if candidate.exists():
            return candidate
    return None


def _parse_port(url: str) -> int:
    """从 URL 中提取端口号"""
    try:
        return int(url.rsplit(":", 1)[-1])
    except (ValueError, IndexError):
        return 3000


class NapCatManager:
    """管理 NapCat 的配置、启动、关闭"""

    def __init__(self, settings):
        self.settings = settings
        self.process: Optional[subprocess.Popen] = None
        self._restart_count = 0
        self._max_restarts = 5
        self._health_task: Optional[asyncio.Task] = None

    async def ensure_installed(self) -> bool:
        """检查是否已安装，未安装则提示"""
        if is_installed():
            logger.info("NapCat is installed")
            return True
        logger.warning("NapCat is not installed. Run 'python setup.py' first.")
        print("\n  ⚠ NapCat 未安装\n  请先运行: python setup.py\n")
        return False

    def generate_config(self):
        """生成 NapCat onebot11 配置"""
        http_port = _parse_port(self.settings.onebot_http_url)
        bot_qq = self.settings.bot_qq_id

        config = {
            "network": {
                "httpServers": [
                    {
                        "enable": True,
                        "name": "HTTP",
                        "host": "127.0.0.1",
                        "port": http_port,
                        "enableCors": True,
                        "enableWebsocket": False,
                        "messagePostFormat": "array",
                        "token": self.settings.onebot_access_token,
                        "debug": False,
                    }
                ],
                "httpSseServers": [],
                "httpClients": [],
                "websocketServers": [],
                "websocketClients": [
                    {
                        "enable": True,
                        "name": "CyberHomie",
                        "url": "ws://127.0.0.1:8765/onebot/ws",
                        "reportSelfMessage": False,
                        "messagePostFormat": "array",
                        "token": self.settings.onebot_access_token,
                        "debug": False,
                        "heartInterval": 30000,
                    }
                ],
                "plugins": [],
            },
            "musicSignUrl": "",
            "enableLocalFile2Url": False,
            "parseMultMsg": False,
        }

        config_dir = NAPCAT_DIR / "napcat" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        for filename in [f"onebot11_{bot_qq}.json", "onebot11.json"]:
            config_path = config_dir / filename
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
            logger.info("NapCat config generated: %s", config_path)

    async def start(self):
        """启动 NapCat 子进程"""
        if self.process and self.process.poll() is None:
            logger.warning("NapCat already running (pid=%d)", self.process.pid)
            return

        exe_path = _get_exe_path()
        if not exe_path:
            logger.error("NapCat executable not found")
            return

        cwd = exe_path.parent

        if exe_path.name == "node.exe":
            cmd = [str(exe_path), "index.js"]
            if self.settings.bot_qq_id:
                cmd.extend(["-q", str(self.settings.bot_qq_id)])
        else:
            cmd = [str(exe_path)]

        logger.info("Starting NapCat: %s", cmd)
        try:
            self.process = subprocess.Popen(cmd, cwd=str(cwd))
            logger.info("NapCat started (pid=%d)", self.process.pid)
            self._restart_count = 0
            self._health_task = asyncio.create_task(self._health_check_loop())
        except Exception as e:
            logger.error("Failed to start NapCat: %s", e)

    async def stop(self):
        """优雅关闭，杀掉整个进程树"""
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        if self.process and self.process.poll() is None:
            pid = self.process.pid
            logger.info("Stopping NapCat (pid=%d)...", pid)
            if sys.platform == "win32":
                # taskkill /T 杀掉整个进程树（NapCat 会派生子进程）
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True)
            else:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            logger.info("NapCat stopped")

    async def _health_check_loop(self):
        """每 30 秒检查进程存活，崩溃自动重启"""
        while True:
            try:
                await asyncio.sleep(30)
                if self.process is None or self.process.poll() is not None:
                    exit_code = self.process.returncode if self.process else "N/A"
                    logger.warning("NapCat exited (code=%s)", exit_code)
                    if self._restart_count < self._max_restarts:
                        self._restart_count += 1
                        logger.info("Restarting NapCat (%d/%d)...", self._restart_count, self._max_restarts)
                        await self.start()
                    else:
                        logger.error("NapCat crashed %d times, giving up.", self._max_restarts)
                        break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Health check error: %s", e)
