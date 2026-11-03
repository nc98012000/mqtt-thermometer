"""小米溫濕度計 (BTHome V2) → MQTT 橋接程式

訂閱 TaiwanSensor 閘道器轉發的 BLE 原始廣播，解析溫度/濕度/電壓/電量後，
以專案統一格式重新發佈到 MQTT（與 simulator.py 相同，儀表板可直接顯示）。

Topic 格式：<TOPIC_PREFIX>/<device>/telemetry  (JSON)
            <TOPIC_PREFIX>/<device>/status     (online/offline, retained + LWT)

用法：python xiaomi_to_mqtt.py [--device xiaomi-01] [--mac A4:C1:38:4A:09:DE]
"""
import argparse
import json
import sys
import time
import uuid
from datetime import datetime

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

import paho.mqtt.client as mqtt

from config import BROKER_HOST, BROKER_PORT, TOPIC_PREFIX
from xiaomi_decrypt_receiver import parse_bthome_v2, DEFAULT_MAC, DEFAULT_GATEWAY_ID

TEMP_ALARM = 33.0


def main():
    parser = argparse.ArgumentParser(description="小米溫濕度計 → MQTT 橋接")
    parser.add_argument("--device", default="xiaomi-01", help="發佈用的裝置名稱")
    parser.add_argument("--mac", default=DEFAULT_MAC, help="溫濕度計 MAC")
    parser.add_argument("--gateway", default=DEFAULT_GATEWAY_ID, help="閘道器 ID")
    args = parser.parse_args()

    mac_clean = args.mac.replace(":", "").upper()
    in_topic = f"ble/{args.gateway}/raw/{mac_clean}"
    out_telemetry = f"{TOPIC_PREFIX}/{args.device}/telemetry"
    out_status = f"{TOPIC_PREFIX}/{args.device}/status"

    # 溫濕度與電壓分在不同封包，需合併最新值
    state = {"temperature": None, "humidity": None, "voltage": None, "battery": None}
    seq = {"n": 0}
    start = time.time()

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"xiaomi-bridge-{uuid.uuid4().hex[:8]}",
    )
    client.will_set(out_status, "offline", qos=1, retain=True)
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    def on_connect(c, u, f, rc, p):
        if rc.is_failure:
            print(f"❌ 連線失敗：{rc}")
            return
        print(f"✅ 已連線 {BROKER_HOST}:{BROKER_PORT}")
        c.subscribe(in_topic, qos=0)
        c.publish(out_status, "online", qos=1, retain=True)
        print(f"📡 來源: {in_topic}\n🚀 發佈: {out_telemetry}")

    def on_message(c, u, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return
        updated = False
        for s in data.get("service_data", []):
            if "fcd2" not in s.get("uuid", "").lower():
                continue
            m = parse_bthome_v2(bytes.fromhex(s.get("data", "")))
            if m.get("encrypted"):
                continue
            for k in state:
                if k in m:
                    state[k] = m[k]
                    updated = True
        if not updated or state["temperature"] is None:
            return

        seq["n"] += 1
        payload = {
            "device": args.device,
            "seq": seq["n"],
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "epoch": round(time.time(), 3),
            "t": round(time.time() - start, 3),
            "temperature": state["temperature"],
            "humidity": state["humidity"],
            "voltage": state["voltage"],
            "battery": state["battery"],
            "rssi": data.get("rssi"),
            "alarm": state["temperature"] > TEMP_ALARM,
        }
        # retain=True：雲端 Worker 一連線就能拿到最新一筆
        c.publish(out_telemetry, json.dumps(payload, ensure_ascii=False), qos=1, retain=True)
        print(f"→ {out_telemetry}  T={payload['temperature']}°C "
              f"H={payload['humidity']}% V={payload['voltage']}")

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\n停止橋接...")
    finally:
        client.publish(out_status, "offline", qos=1, retain=True).wait_for_publish(5)
        client.disconnect()


if __name__ == "__main__":
    main()
