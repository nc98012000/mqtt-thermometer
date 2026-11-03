"""MQTT 訂閱程式：訂閱模擬器的所有 topic，並印出/記錄收到的資料。

用法：python subscriber.py [--topic 自訂topic] [--log data.csv]
  預設訂閱 <TOPIC_PREFIX>/#（所有裝置的 telemetry 與 status）
"""
import sys
import io

# Windows 控制台 UTF-8 設定（避免重複包裝）
if sys.platform == 'win32':
    try:
        if not isinstance(sys.stdout, io.TextIOWrapper) or sys.stdout.encoding.lower() != 'utf-8':
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
        if not isinstance(sys.stderr, io.TextIOWrapper) or sys.stderr.encoding.lower() != 'utf-8':
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)
    except (AttributeError, ValueError):
        pass

import argparse
import csv
import json
import os
import uuid

import paho.mqtt.client as mqtt

from config import BROKER_HOST, BROKER_PORT, TOPIC_PREFIX

TEMP_ALARM = 33.0  # 溫度超過此值顯示告警（與模擬器一致）

CSV_FIELDS = ["ts", "t", "device", "seq", "temperature", "humidity", "voltage", "alarm"]


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        print(f"❌ 連線失敗：{reason_code}")
        return
    topic = userdata["topic"]
    client.subscribe(topic, qos=1)  # 重連後也會重新訂閱
    print(f"✅ 已連線 {BROKER_HOST}:{BROKER_PORT}，訂閱 {topic}")


def on_message(client, userdata, msg):
    payload = msg.payload.decode("utf-8", errors="replace")

    if msg.topic.endswith("/status"):
        icon = "🟢" if payload == "online" else "🔴"
        retained = "（retained）" if msg.retain else ""
        print(f"{icon} {msg.topic} = {payload}{retained}")
        return

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        print(f"📩 {msg.topic}: {payload}")
        return

    temp = data.get("temperature")
    warn = " 🔥 溫度過高！" if temp is not None and temp > TEMP_ALARM else ""
    print(f"📊 [{data.get('ts')}] {data.get('device')} #{data.get('seq')}  "
          f"T={temp}°C  H={data.get('humidity')}%  V={data.get('voltage')}{warn}")

    writer = userdata.get("csv_writer")
    if writer:
        writer.writerow({k: data.get(k) for k in CSV_FIELDS})
        userdata["csv_file"].flush()


def main():
    parser = argparse.ArgumentParser(description="MQTT 訂閱程式")
    parser.add_argument("--topic", default=f"{TOPIC_PREFIX}/#", help="訂閱的 topic（支援 + / # 萬用字元）")
    parser.add_argument("--log", help="把 telemetry 寫入 CSV 檔")
    args = parser.parse_args()

    userdata = {"topic": args.topic}
    csv_file = None
    if args.log:
        new_file = not os.path.exists(args.log)
        # utf-8-sig 讓 Excel 直接開也不會亂碼
        csv_file = open(args.log, "a", newline="", encoding="utf-8-sig")
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
        if new_file:
            writer.writeheader()
        userdata.update(csv_file=csv_file, csv_writer=writer)
        print(f"📝 記錄到 {args.log}")

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"psp-sub-{uuid.uuid4().hex[:8]}",
        userdata=userdata,
    )
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\n停止訂閱...")
    finally:
        client.disconnect()
        if csv_file:
            csv_file.close()


if __name__ == "__main__":
    main()
