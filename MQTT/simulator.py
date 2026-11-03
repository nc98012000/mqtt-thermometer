"""MQTT 模擬器：模擬多個感測器，定時發送溫度/濕度/電壓資料到公開 broker。

數值為正弦波，每個裝置相位錯開；可用 --period 調週期、--noise 加雜訊。

Topic 格式：<TOPIC_PREFIX>/<device>/telemetry   (JSON)
            <TOPIC_PREFIX>/<device>/status      (online/offline, retained + LWT)

用法：python simulator.py [--interval 秒] [--count 次數] [--period 秒] [--noise 比例]
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
import json
import math
import random
import time
import uuid
from datetime import datetime

import paho.mqtt.client as mqtt

from config import BROKER_HOST, BROKER_PORT, TOPIC_PREFIX, DEVICES, PUBLISH_INTERVAL


TEMP_ALARM = 33.0  # 溫度超過此值標記 alarm


class SimulatedSensor:
    """以正弦波產生感測值：value = offset + amplitude * sin(2π·t/period + phase)

    每個裝置相位錯開 360°/N，三個裝置就是 0°/120°/240°。
    """

    def __init__(self, device_id, index, total, period, noise, start):
        self.device_id = device_id
        self.phase = 2 * math.pi * index / total
        self.period = period
        self.noise = noise
        self.start = start
        self.seq = 0

    def _sine(self, offset, amplitude, t, extra_phase=0.0):
        value = offset + amplitude * math.sin(2 * math.pi * t / self.period + self.phase + extra_phase)
        if self.noise:
            value += random.gauss(0, self.noise * amplitude)
        return value

    def read(self):
        self.seq += 1
        t = time.time() - self.start
        temp = self._sine(25.0, 10.0, t)                     # 15 ~ 35 °C
        hum = self._sine(50.0, 20.0, t, math.pi / 2)         # 30 ~ 70 %，比溫度超前 90°（cos）
        voltage = self._sine(3.3, 0.2, t)                    # 3.1 ~ 3.5 V
        return {
            "device": self.device_id,
            "seq": self.seq,
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "t": round(t, 3),
            "temperature": round(temp, 3),
            "humidity": round(hum, 3),
            "voltage": round(voltage, 4),
            "alarm": temp > TEMP_ALARM,
        }


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        print(f"❌ 連線失敗：{reason_code}")
        return
    print(f"✅ 已連線 {BROKER_HOST}:{BROKER_PORT}")
    for dev in DEVICES:
        client.publish(f"{TOPIC_PREFIX}/{dev}/status", "online", qos=1, retain=True)


def on_disconnect(client, userdata, flags, reason_code, properties):
    print(f"⚠️ 連線中斷：{reason_code}（paho 會自動重連）")


def main():
    parser = argparse.ArgumentParser(description="MQTT 感測器模擬器")
    parser.add_argument("--interval", type=float, default=PUBLISH_INTERVAL, help="發送間隔（秒）")
    parser.add_argument("--count", type=int, default=0, help="發送輪數，0 = 無限")
    parser.add_argument("--period", type=float, default=60.0, help="正弦波週期（秒）")
    parser.add_argument("--noise", type=float, default=0.0, help="雜訊比例（相對振幅，例如 0.05 = 5%%），0 = 純正弦波")
    args = parser.parse_args()

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"psp-sim-{uuid.uuid4().hex[:8]}",
    )
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    # 遺囑訊息：模擬器意外斷線時，broker 會幫第一個裝置發 offline
    client.will_set(f"{TOPIC_PREFIX}/{DEVICES[0]}/status", "offline", qos=1, retain=True)
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
    client.loop_start()

    start = time.time()
    sensors = [SimulatedSensor(d, i, len(DEVICES), args.period, args.noise, start)
               for i, d in enumerate(DEVICES)]
    print(f"🌊 正弦波：週期 {args.period}s，相位差 {360 / len(DEVICES):.0f}°，雜訊 {args.noise:.0%}")
    rounds = 0
    try:
        while args.count == 0 or rounds < args.count:
            for s in sensors:
                data = s.read()
                topic = f"{TOPIC_PREFIX}/{s.device_id}/telemetry"
                client.publish(topic, json.dumps(data, ensure_ascii=False), qos=1)
                flag = " 🔥ALARM" if data["alarm"] else ""
                print(f"→ {topic}  T={data['temperature']}°C H={data['humidity']}% V={data['voltage']}{flag}")
            rounds += 1
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n停止模擬器...")
    finally:
        for dev in DEVICES:
            client.publish(f"{TOPIC_PREFIX}/{dev}/status", "offline", qos=1, retain=True).wait_for_publish(5)
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
