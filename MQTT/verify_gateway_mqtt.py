"""驗證 TaiwanSensor 閘道器解密與 MQTT 轉發腳本"""
import io
import json
import os
import sys
import time

# Windows UTF-8 控制台編碼設定
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

import paho.mqtt.client as mqtt

BROKER_HOST = "broker.emqx.io"
BROKER_PORT = 1883
GATEWAY_ID = "esp32-eth-ble-mqtt-x4-2c8628"
TARGET_MAC = "A4:C1:38:4A:09:DE"

received_count = 0
target_packet_count = 0

def on_connect(client, userdata, flags, rc, props=None):
    print(f"✅ 成功連線至 MQTT Broker: {BROKER_HOST}:{BROKER_PORT}")
    topics = [
        (f"ble/{GATEWAY_ID}/#", 0),
        (f"{GATEWAY_ID}/#", 0),
        (f"+/{GATEWAY_ID}/#", 0),
        (f"homeassistant/+/{GATEWAY_ID}/#", 0),
    ]
    for t, q in topics:
        client.subscribe(t, qos=q)
        print(f"📡 訂閱 Topic: {t}")
    print(f"\n🎯 目標溫濕度計 MAC: {TARGET_MAC}")
    print("⏳ 開始即時監聽閘道器 MQTT 轉發（持續 20 秒）...")
    print("------------------------------------------------------------")

def on_message(client, userdata, msg):
    global received_count, target_packet_count
    received_count += 1
    payload_str = msg.payload.decode("utf-8", errors="replace")
    
    # 檢查是否與目標 MAC 有關（支援帶冒號、不帶冒號、大小寫）
    mac_clean = TARGET_MAC.replace(":", "").lower()
    is_target = (
        TARGET_MAC.lower() in msg.topic.lower() or
        mac_clean in msg.topic.lower() or
        TARGET_MAC.lower() in payload_str.lower() or
        mac_clean in payload_str.lower()
    )
    
    if is_target:
        target_packet_count += 1
        print(f"\n🔥 [捕獲目標溫濕度計數據! #{target_packet_count}]")
        print(f"   Topic: {msg.topic}")
        try:
            parsed = json.loads(payload_str)
            print(f"   解密數值 (JSON): {json.dumps(parsed, indent=2, ensure_ascii=False)}")
        except Exception:
            print(f"   原始 Payload: {payload_str}")
    else:
        # 印出其他來自此閘道器的訊息摘要
        preview = payload_str[:120].replace("\n", " ")
        print(f"📦 [{msg.topic}]: {preview}...")

def main():
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2, 
        client_id=f"verify-tool-{int(time.time())}"
    )
    client.on_connect = on_connect
    client.on_message = on_message
    
    try:
        client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
    except Exception as e:
        print(f"❌ 無法連線至 MQTT Broker: {e}")
        return

    client.loop_start()
    for i in range(20, 0, -1):
        time.sleep(1)
        print(f"   剩餘秒數: {i:02d}s | 閘道器訊息: {received_count} 則 | 目標裝置: {target_packet_count} 則", end="\r", flush=True)
    client.loop_stop()
    client.disconnect()
    
    print("\n------------------------------------------------------------")
    print(f"🏁 監聽結束！")
    print(f"   - 來自閘道器總訊息數: {received_count} 則")
    print(f"   - 目標溫濕度計訊息數: {target_packet_count} 則")
    if target_packet_count > 0:
        print("   🎉 驗證成功：閘道器已正確收到並轉發該溫濕度計的解密數據！")
    else:
        print("   ⚠️ 尚未收到目標溫濕度計訊息，可能原因：")
        print("     1. 手機藍牙背景連線仍佔用溫濕度計（請將手機藍牙關閉）。")
        print("     2. 溫濕度計省電休眠未發送廣播（可拔插電池或按一下按鈕喚醒）。")
        print("     3. 距離閘道器較遠或訊號被阻擋。")

if __name__ == "__main__":
    main()
