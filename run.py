"""A股量化交易系统 - Streamlit入口"""
import subprocess
import sys

from app.st_utils import get_dual_stack_server_address, register_static_mimetypes

if __name__ == "__main__":
    register_static_mimetypes()
    listen_addr = get_dual_stack_server_address()
    print("🚀 启动 A股量化交易系统，请访问: http://localhost:8501 或 http://127.0.0.1:8501", flush=True)
    args = [
        "streamlit",
        "run",
        "app/main.py",
        "--server.address",
        listen_addr,
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
        "--browser.serverAddress",
        "127.0.0.1",
    ]
    try:
        from streamlit.web import cli as stcli

        sys.argv = args
        sys.exit(stcli.main())
    except ImportError:
        subprocess.run([sys.executable, "-m", *args])
