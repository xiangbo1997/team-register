import os
import json
import sqlite3
import requests
from dotenv import load_dotenv

def test_specific_account():
    load_dotenv()
    base_url = os.getenv("EMAIL_PROVIDER_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    api_key = os.getenv("EMAIL_PROVIDER_API_KEY", "")
    api_prefix = f"{base_url}/api/mailbox-service"
    
    email = "qsbghg98884r@outlook.com"
    
    # 1. 从数据库读取当前生效的凭据
    conn = sqlite3.connect("team_register.db")
    cursor = conn.cursor()
    cursor.execute("SELECT client_id, refresh_token FROM mail_accounts WHERE email = ?", (email,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        print(f"❌ 错误: 数据库中未找到账号 {email}")
        return
    
    client_id, refresh_token = row
    print(f"🔍 正在测试账号: {email}")
    print(f"🔍 Client ID 长度: {len(client_id)}")
    print(f"🔍 Refresh Token 长度: {len(refresh_token)}")

    # 2. 模拟 credentialed-sessions 请求
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        
    payload = {
        "provider": "applemail",
        "purpose": "otp",
        "lease_seconds": 300,
        "session_mode": "credentialed",
        "existing_account": {
            "email": email,
            "credentials": {
                "client_id": client_id,
                "refresh_token": refresh_token,
                "password": "unused"
            }
        }
    }
    
    print(f"\n🚀 正在请求: {api_prefix}/credentialed-sessions")
    try:
        resp = requests.post(f"{api_prefix}/credentialed-sessions", json=payload, headers=headers, timeout=10)
        print(f"📥 响应状态码: {resp.status_code}")
        if resp.status_code == 200:
            print("✅ 成功! 服务端接受了该账号凭据。")
            print(f"会话 ID: {resp.json().get('session_id')}")
        else:
            print(f"❌ 失败: {resp.text}")
            
            if resp.status_code == 404:
                print("\n⚠️ 发现回退逻辑潜在问题，尝试旧版 /sessions 接口...")
                # 尝试扁平化 payload (旧版可能期望的格式)
                old_payload = {
                    "provider": "applemail",
                    "email": email,
                    "refresh_token": refresh_token,
                    "client_id": client_id
                }
                resp_old = requests.post(f"{api_prefix}/sessions", json=old_payload, headers=headers, timeout=10)
                print(f"📥 旧版接口响应: {resp_old.status_code}")
                print(f"📥 响应内容: {resp_old.text}")

    except Exception as e:
        print(f"💥 请求发生异常: {e}")

if __name__ == "__main__":
    test_specific_account()
