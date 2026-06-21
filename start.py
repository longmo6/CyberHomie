"""
一键启动：本地 LLM (文本 + 视觉) + CyberHomie
"""

import subprocess
import sys
import time
import signal
import urllib.request
import json
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────

LLAMA_SERVER = r"C:\Users\泷墨\Desktop\llama\llama-server.exe"
MODEL_DIR = Path(r"C:\llm_model")

# 文本模型
TEXT_MODEL = MODEL_DIR / "qwen2.5-14b-instruct-q4_k_m.gguf"
TEXT_PORT = 8080

# 视觉模型（可选，文件存在才启动）
VISION_MODEL = MODEL_DIR / "Qwen2-VL-2B-Instruct-Q4_K_M.gguf"
VISION_MMPROJ = MODEL_DIR / "mmproj-Qwen2-VL-2B-Instruct-f16.gguf"
VISION_PORT = 8081

HOST = "127.0.0.1"


# ── 工具函数 ──────────────────────────────────────────


def port_in_use(port):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((HOST, port)) == 0


def start_server(model_path, mmproj_path=None, port=8080, ctx=4096):
    """启动 llama-server，返回 Popen 或 None"""
    if not Path(LLAMA_SERVER).exists():
        print(f"[!] llama-server 不存在: {LLAMA_SERVER}")
        sys.exit(1)
    if not Path(model_path).exists():
        print(f"[!] 模型文件不存在: {model_path}")
        return None
    if port_in_use(port):
        print(f"[*] 端口 {port} 已占用，跳过")
        return None

    cmd = [
        LLAMA_SERVER,
        "-m", str(model_path),
        "--host", HOST,
        "--port", str(port),
        "-c", str(ctx),
        "-ngl", "999",
        "-np", "1",
    ]
    if mmproj_path and Path(mmproj_path).exists():
        cmd.extend(["--mmproj", str(mmproj_path)])

    name = Path(model_path).stem
    print(f"[*] 启动 {name} -> :{port}")
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_ready(port, label="LLM", timeout=120):
    """等待服务就绪"""
    url = f"http://{HOST}:{port}/v1/models"
    print(f"[*] 等待 {label} 加载...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                if data.get("models"):
                    print(f"[+] {label} 就绪: {data['models'][0]['name']}")
                    return True
        except Exception:
            pass
        time.sleep(3)
    print(f"[!] {label} 启动超时")
    return False


def kill_proc(proc):
    """杀掉进程树"""
    if proc and proc.poll() is None:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)


# ── 主流程 ────────────────────────────────────────────


def main():
    procs = []

    def cleanup(*_):
        print("\n[*] 正在关闭...")
        for p in procs:
            kill_proc(p)
        print("[+] 已关闭")
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    # 1. 启动文本模型
    p = start_server(TEXT_MODEL, port=TEXT_PORT, ctx=8192)
    if p:
        procs.append(p)
    if not wait_ready(TEXT_PORT, "文本模型"):
        cleanup()

    # 2. 启动视觉模型（可选）
    if VISION_MODEL.exists() and VISION_MMPROJ.exists():
        p = start_server(VISION_MODEL, mmproj_path=VISION_MMPROJ, port=VISION_PORT, ctx=8192)
        if p:
            procs.append(p)
        if not wait_ready(VISION_PORT, "视觉模型"):
            cleanup()
    else:
        print(f"[*] 视觉模型未找到，跳过（放到 {MODEL_DIR} 即可自动启动）")

    # 3. 启动 CyberHomie
    print("[*] 启动 CyberHomie...")
    bot = subprocess.Popen([sys.executable, "main.py"])
    procs.append(bot)

    try:
        bot.wait()
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()


if __name__ == "__main__":
    main()
