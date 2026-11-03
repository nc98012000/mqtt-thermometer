"""啟動 MQTT 溫度計網頁儀表板 (Web Dashboard Launcher)

用法：
  python run_dashboard.py [--port 5050]
"""
import io
import sys

# Windows 控制台 UTF-8 設定（避免 cp950 編碼問題）
if sys.platform == 'win32':
    try:
        if not isinstance(sys.stdout, io.TextIOWrapper) or sys.stdout.encoding.lower() != 'utf-8':
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
        if not isinstance(sys.stderr, io.TextIOWrapper) or sys.stderr.encoding.lower() != 'utf-8':
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)
    except (AttributeError, ValueError):
        pass

import argparse
import http.server
import os
import socketserver
import webbrowser
from functools import partial

PORT = 5050
DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard")

def main():
    parser = argparse.ArgumentParser(description="啟動 MQTT 溫度計 Web 儀表板伺服器")
    parser.add_argument("--port", type=int, default=PORT, help=f"Web 伺服器通訊埠 (預設 {PORT})")
    parser.add_argument("--no-browser", action="store_true", help="不要自動開啟瀏覽器")
    args = parser.parse_args()

    handler = partial(http.server.SimpleHTTPRequestHandler, directory=DASHBOARD_DIR)

    with socketserver.TCPServer(("", args.port), handler) as httpd:
        url = f"http://localhost:{args.port}"
        print("=" * 60)
        print("🌡️  MQTT 感測器溫度計儀表板已啟動！")
        print(f"👉 請開啟瀏覽器：{url}")
        print("💡 按 Ctrl + C 可關閉伺服器")
        print("=" * 60)

        if not args.no_browser:
            webbrowser.open(url)

        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n正在停止伺服器...")

if __name__ == "__main__":
    main()
