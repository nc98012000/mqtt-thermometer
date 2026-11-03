"""GitHub 自動部署腳本 (一鍵發佈 MQTT 儀表板到 GitHub Pages)

使用方式：
    python deploy_to_github.py --token <你的GitHub_Token> [--repo <倉庫名稱>]

本腳本使用 Python 內建 urllib 呼叫 GitHub REST API，免安裝額外套件。
自動完成：
1. 驗證 GitHub 帳號
2. 自動建立公開倉庫 (預設 mqtt-thermometer)
3. 自動上傳 index.html, style.css, app.js, mqtt.min.js
4. 自動啟用 GitHub Pages
5. 輸出專屬網頁網址
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard")
FILES_TO_UPLOAD = ["index.html", "style.css", "app.js", "mqtt.min.js"]


def github_api(url, token, data=None, method=None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "MQTT-Dashboard-Deployer",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            content = resp.read().decode("utf-8")
            return resp.status, json.loads(content) if content else {}
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        try:
            parsed = json.loads(error_body)
        except Exception:
            parsed = {"message": error_body}
        return e.code, parsed


def main():
    parser = argparse.ArgumentParser(description="一鍵發佈 MQTT 儀表板到 GitHub Pages")
    parser.add_argument("--token", required=True, help="GitHub Personal Access Token (PAT)")
    parser.add_argument("--repo", default="mqtt-thermometer", help="GitHub 倉庫名稱 (預設: mqtt-thermometer)")
    args = parser.parse_args()

    token = args.token.strip()
    repo_name = args.repo.strip()

    print("=" * 60)
    print("🚀 正在連線 GitHub 進行自動設定...")
    print("=" * 60)

    # 1. 驗證使用者身分
    status, user_info = github_api("https://api.github.com/user", token)
    if status != 200:
        print(f"❌ 驗證失敗 (HTTP {status})：{user_info.get('message', 'Token 無效或權限不足')}")
        print("💡 請確認 Token 具有 'repo' 完整權限！")
        sys.exit(1)

    username = user_info.get("login")
    print(f"✅ GitHub 帳號已識別：{username}")

    # 2. 檢查或建立倉庫
    status, repo_info = github_api(f"https://api.github.com/repos/{username}/{repo_name}", token)
    if status == 404:
        print(f"📦 正在建立公開倉庫：{repo_name} ...")
        create_data = {
            "name": repo_name,
            "description": "即時 MQTT 感測器水銀溫度計監控儀表板",
            "private": False,
            "auto_init": True
        }
        status, repo_info = github_api("https://api.github.com/user/repos", token, data=create_data)
        if status not in (200, 201):
            print(f"❌ 建立倉庫失敗：{repo_info.get('message')}")
            sys.exit(1)
        print("✅ 倉庫建立成功！等待初始化...")
        time.sleep(2)
    elif status == 200:
        print(f"📦 倉庫 {repo_name} 已存在，準備更新檔案...")
    else:
        print(f"❌ 存取倉庫異常 (HTTP {status})：{repo_info.get('message')}")
        sys.exit(1)

    # 3. 逐一上傳檔案
    for filename in FILES_TO_UPLOAD:
        filepath = os.path.join(DASHBOARD_DIR, filename)
        if not os.path.exists(filepath):
            print(f"⚠️ 找不到檔案：{filepath}，跳過")
            continue

        with open(filepath, "rb") as f:
            file_bytes = f.read()

        b64_content = base64.b64encode(file_bytes).decode("ascii")

        # 檢查該檔案是否已經在倉庫中 (取得其 sha 用於更新)
        file_api_url = f"https://api.github.com/repos/{username}/{repo_name}/contents/{filename}"
        status, existing_file = github_api(file_api_url, token)
        put_data = {
            "message": f"Deploy {filename} via MQTT Deployer",
            "content": b64_content,
            "branch": "main"
        }
        if status == 200 and "sha" in existing_file:
            put_data["sha"] = existing_file["sha"]

        status, put_res = github_api(file_api_url, token, data=put_data, method="PUT")
        if status in (200, 201):
            print(f"  ✔ 上傳成功：{filename} ({len(file_bytes):,} bytes)")
        else:
            print(f"  ❌ 上傳失敗：{filename} - {put_res.get('message')}")

    # 4. 啟用 GitHub Pages
    pages_api_url = f"https://api.github.com/repos/{username}/{repo_name}/pages"
    pages_data = {
        "source": {
            "branch": "main",
            "path": "/"
        }
    }
    status, pages_res = github_api(pages_api_url, token, data=pages_data, method="POST")
    if status in (201, 200):
        print("✅ GitHub Pages 啟用成功！")
    elif status == 409 or "already" in str(pages_res).lower():
        print("ℹ️ GitHub Pages 已經啟用。")
    else:
        # 有些情況下已經存在但回傳其他狀態
        print(f"ℹ️ GitHub Pages 設定狀態：{pages_res.get('message', 'OK')}")

    expected_url = f"https://{username}.github.io/{repo_name}/"
    print("=" * 60)
    print("🎉 發佈流程完成！")
    print(f"🌐 您的專屬網站網址預計為：")
    print(f"   👉 {expected_url}")
    print("💡 提示：GitHub Pages 首次建置需要約 1~2 分鐘生效，請稍候片刻再開啟。")
    print("=" * 60)


if __name__ == "__main__":
    main()

