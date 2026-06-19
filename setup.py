"""
CyberHomie 快速配置向导
运行 python setup.py 进行交互式配置
"""

import json
import zipfile
import urllib.request
from pathlib import Path

# ── 常量 ──────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).parent
ENV_PATH = PROJECT_ROOT / ".env"
DATA_DIR = PROJECT_ROOT / "data"

NAPCAT_RELEASE = "https://github.com/NapNeko/NapCatQQ/releases/latest/download/NapCat.Shell.Windows.Node.zip"
NAPCAT_DIR = DATA_DIR / "napcat"

# ── 工具函数 ──────────────────────────────────────────


def banner():
    print(r"""
╔══════════════════════════════════════════════╗
║          CyberHomie 快速配置向导             ║
╚══════════════════════════════════════════════╝
  所有步骤均可回车跳过，稍后重新运行
  python setup.py 或手动编辑 .env 补充配置
""")


def ask(prompt, default="", required=False):
    """带默认值的输入，回车跳过返回默认值或空字符串"""
    hint = " [按回车跳过]" if not required and not default else ""
    if default:
        raw = input(f"  {prompt} [{default}]{hint}: ").strip()
        return raw if raw else default
    else:
        raw = input(f"  {prompt}{hint}: ").strip()
        return raw


def ask_int(prompt, default=0):
    """输入整数"""
    while True:
        raw = input(f"  {prompt} [{default}]: ").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            print("  ⚠ 请输入数字")


def download_file(url, dest_dir, desc="文件"):
    """带进度条的文件下载"""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n  正在下载 {desc}...")
    print(f"  URL: {url}")

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CyberHomie-Setup/1.0"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            filename = url.split("/")[-1]
            filepath = dest_dir / filename

            downloaded = 0
            block_size = 8192

            with open(filepath, "wb") as f:
                while True:
                    chunk = resp.read(block_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)

                    if total > 0:
                        pct = downloaded * 100 // total
                        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
                        size_mb = downloaded / 1024 / 1024
                        total_mb = total / 1024 / 1024
                        print(f"\r  [{bar}] {pct}% ({size_mb:.1f}/{total_mb:.1f} MB)", end="", flush=True)

            print(f"\n  ✓ 下载完成: {filepath}")
            return filepath

    except Exception as e:
        print(f"\n  ✗ 下载失败: {e}")
        print("  请检查网络连接，或手动下载后放到 data/napcat/ 目录")
        return None


def extract_zip(zip_path, dest_dir):
    """解压 zip 文件"""
    print(f"  正在解压到 {dest_dir}...")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)
    print("  ✓ 解压完成")
    Path(zip_path).unlink(missing_ok=True)
    print("  ✓ 已清理 zip 文件")


# ── 配置生成 ──────────────────────────────────────────


def gen_napcat_config(bot_qq_id, http_port, access_token=""):
    """生成 NapCat onebot11 配置"""
    config_dir = NAPCAT_DIR / "napcat" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

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
                    "token": access_token,
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
                    "token": access_token,
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

    for filename in [f"onebot11_{bot_qq_id}.json", "onebot11.json"]:
        config_path = config_dir / filename
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    print(f"  ✓ 配置已生成: {config_dir}/onebot11_{bot_qq_id}.json")


def gen_env(bot_qq_id, group_ids, api_key, base_url, model, vision_model, http_port, access_token):
    """生成 .env 文件"""
    lines = [
        "# NapCat",
        f"ONEBOT_HTTP_URL=http://127.0.0.1:{http_port}",
        f"ONEBOT_ACCESS_TOKEN={access_token}",
        "",
        "# Bot Identity",
        f"BOT_QQ_ID={bot_qq_id}",
        f"TARGET_GROUP_IDS={group_ids}",
        "",
        "# LLM",
        f"LLM_API_KEY={api_key}",
        f"LLM_BASE_URL={base_url}",
        f"LLM_MODEL={model}",
        f"LLM_VISION_MODEL={vision_model}",
        "",
        "# Resource mode: true = 高耗, false = 低耗",
        "HIGH_RESOURCE_MODE=true",
        "",
        "# Humanizer",
        "BASE_REPLY_PROBABILITY=0.15",
        "ACTIVE_HOUR_START=10",
        "ACTIVE_HOUR_END=2",
        "SESSION_GAP_MIN=20",
        "SESSION_GAP_MAX=90",
        "SESSION_DURATION_MIN=3",
        "SESSION_DURATION_MAX=10",
        "",
        "# Database",
        "DB_PATH=data/cyberhomie.db",
    ]

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  ✓ 配置已生成: {ENV_PATH}")


# ── 主流程 ────────────────────────────────────────────


def main():
    banner()

    existing = {}
    if ENV_PATH.exists():
        print("  ℹ 检测到已有 .env 配置，将使用现有值作为默认值\n")
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    existing[k.strip()] = v.strip()

    # ── Step 1: 基本配置 ──
    print("━" * 48)
    print("  [1/4] 基本配置 (可跳过)")
    print("━" * 48)
    print()

    bot_qq_id = ask_int("Bot QQ 号", int(existing.get("BOT_QQ_ID", 0)))
    group_ids = ask("监听的群号 (逗号分隔)", existing.get("TARGET_GROUP_IDS", ""))
    if not group_ids:
        print("  ℹ 群号为空，启动后需在 .env 中配置 TARGET_GROUP_IDS")
    print()

    # ── Step 2: LLM 配置 ──
    print("━" * 48)
    print("  [2/4] LLM 配置 (可跳过)")
    print("━" * 48)
    print("  ℹ 留空则使用默认值，稍后可编辑 .env 或重新运行 setup.py")
    print()

    api_key = ask("API Key", existing.get("LLM_API_KEY", ""))
    base_url = ask("Base URL", existing.get("LLM_BASE_URL", "https://api.xiaomimimo.com/v1"))
    model = ask("模型", existing.get("LLM_MODEL", "mimo-v2.5-pro"))
    vision_model = ask("视觉模型 (图片理解)", existing.get("LLM_VISION_MODEL", "mimo-v2.5"))
    print()

    # ── Step 3: 下载 NapCat ──
    print("━" * 48)
    print("  [3/4] 下载 NapCat")
    print("━" * 48)

    if any((NAPCAT_DIR / p).exists() for p in ["napcat.bat", "napcat.cmd", "node.exe"]):
        print("\n  ✓ NapCat 已安装，跳过下载")
    else:
        zip_path = download_file(NAPCAT_RELEASE, DATA_DIR, "NapCat.Shell")
        if zip_path:
            extract_zip(zip_path, NAPCAT_DIR)
        else:
            print("  ⚠ 下载失败，请手动下载后放到 data/napcat/ 目录")
            print(f"  下载地址: {NAPCAT_RELEASE}")

    # ── Step 4: 生成配置 ──
    print()
    print("━" * 48)
    print("  [4/4] 生成配置")
    print("━" * 48)
    print()

    http_port = 3000
    access_token = existing.get("ONEBOT_ACCESS_TOKEN", "")

    gen_env(bot_qq_id, group_ids, api_key, base_url, model, vision_model, http_port, access_token)
    gen_napcat_config(bot_qq_id, http_port, access_token)

    print()
    print("═" * 48)
    print("  ✓ 配置完成！")
    print()
    print("  启动机器人:  python main.py")
    print("  修改配置:    python setup.py 或直接编辑 .env")
    print("  首次启动会显示二维码，手机 QQ 扫码登录")
    print("═" * 48)


if __name__ == "__main__":
    main()
