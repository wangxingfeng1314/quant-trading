"""A股量化交易系统 - Streamlit入口"""
import subprocess
import sys

if __name__ == "__main__":
    subprocess.run([
        sys.executable, "-m", "streamlit", "run",
        "app/main.py",
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
    ])
