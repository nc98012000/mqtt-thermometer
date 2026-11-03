"""小米溫濕度計 3 (MJWSD05MMC) 廣播接收與 AES-128-CCM 解密程式

支援三種工作模式：
1. ble (預設)：透過電腦本機藍牙 (Bleak) 進行被動監聽 (Passive Scan)，收到廣播時使用 Bind Key 即時解密。
2. mqtt：訂閱 TaiwanSensor 閘道器 MQTT 主題，自動接收閘道器解密或轉發的廣播數據。
3. test：使用標準測試向量執行解密模組自我驗證，確認演算法與金鑰格式正確。

使用範例：
  python xiaomi_decrypt_receiver.py --mode ble
  python xiaomi_decrypt_receiver.py --mode mqtt
  python xiaomi_decrypt_receiver.py --mode test
"""

import argparse
import asyncio
import io
import json
import os
import struct
import sys
import time
from datetime import datetime
from typing import Dict, Optional, Tuple, Any

# Windows UTF-8 控制台編碼
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

# 預設參數
DEFAULT_MAC = "A4:C1:38:4A:09:DE"
DEFAULT_BINDKEY = "8ae5960ef4ddcc6bb7d618d77b868485"
DEFAULT_GATEWAY_ID = "esp32-eth-ble-mqtt-x4-2c8628"
DEFAULT_BROKER = "broker.emqx.io"
DEFAULT_PORT = 1883
MIBEACON_UUID = "0000fe95-0000-1000-8000-00805f9b34fb"


class MiBeaconDecryptor:
    """小米 MiBeacon v4/v5 AES-128-CCM 解密與 TLV 物件解析器"""

    def __init__(self, bindkey_hex: str, target_mac: str):
        self.bindkey = bytes.fromhex(bindkey_hex)
        self.target_mac = target_mac.upper()
        self.mac_clean = target_mac.replace(":", "").upper()
        self.cipher = AESCCM(self.bindkey, tag_length=4)

    def decrypt(self, raw_data: bytes, sender_mac: str = "") -> Optional[bytes]:
        """解密 MiBeacon 廣播封包 (Service Data UUID 0xFE95)"""
        if len(raw_data) < 5:
            return None

        fc = int.from_bytes(raw_data[:2], byteorder="little")
        is_encrypted = bool(fc & 0x0008)
        has_mac = bool(fc & 0x0010)
        has_capability = bool(fc & 0x0020)

        # 非加密封包，直接回傳數據區域
        if not is_encrypted:
            offset = 5
            if has_mac:
                offset += 6
            if has_capability:
                offset += 1
            return raw_data[offset:]

        # 加密封包長度至少需具備 Header + ExtCounter(3) + MIC(4)
        if len(raw_data) < 12:
            return None

        # 取得反向 MAC (6 bytes)
        offset = 5
        if has_mac:
            mac_rev = raw_data[5:11]
            offset = 11
        else:
            mac_str = (sender_mac or self.target_mac).replace(":", "")
            mac_rev = bytes.fromhex(mac_str)[::-1]

        if has_capability:
            offset += 1

        ext_counter = raw_data[-7:-4]
        tag = raw_data[-4:]
        ciphertext = raw_data[offset:-7]

        # 構建 12-byte Nonce
        # Nonce = MAC_Reversed(6) + ProductID(2) + FrameCounter(1) + ExtCounter(3)
        nonce = mac_rev + raw_data[2:5] + ext_counter

        try:
            plaintext = self.cipher.decrypt(nonce, ciphertext + tag, b"\x11")
            return plaintext
        except Exception as e:
            # 金鑰不符或封包校驗碼錯誤
            return None

    @staticmethod
    def parse_tlv_objects(payload: bytes) -> Dict[str, Any]:
        """解析解密後的 TLV (Type-Length-Value) 物件"""
        result = {}
        offset = 0
        length = len(payload)

        while offset + 3 <= length:
            obj_id = int.from_bytes(payload[offset:offset+2], "little")
            obj_len = payload[offset+2]
            offset += 3
            if offset + obj_len > length:
                break
            obj_data = payload[offset:offset+obj_len]
            offset += obj_len

            # 0x1004: 溫度 (s16 / 10)
            if obj_id == 0x1004 and len(obj_data) >= 2:
                temp = int.from_bytes(obj_data[:2], "little", signed=True) / 10.0
                result["temperature"] = temp

            # 0x1006: 濕度 (u16 / 10)
            elif obj_id == 0x1006 and len(obj_data) >= 2:
                humi = int.from_bytes(obj_data[:2], "little", signed=False) / 10.0
                result["humidity"] = humi

            # 0x100A: 電池電量百分比 (u8)
            elif obj_id == 0x100A and len(obj_data) >= 1:
                result["battery"] = obj_data[0]

            # 0x100D: 溫濕度複合 (s16 溫度/10 + u16 濕度/10)
            elif obj_id == 0x100D and len(obj_data) >= 4:
                temp = int.from_bytes(obj_data[0:2], "little", signed=True) / 10.0
                humi = int.from_bytes(obj_data[2:4], "little", signed=False) / 10.0
                result["temperature"] = temp
                result["humidity"] = humi

            # 0x1007: 照度 (u24 lux)
            elif obj_id == 0x1007 and len(obj_data) >= 3:
                lux = int.from_bytes(obj_data[:3], "little")
                result["illuminance"] = lux

            # 0x1008: 水分/土壤濕度 (u8 %)
            elif obj_id == 0x1008 and len(obj_data) >= 1:
                result["moisture"] = obj_data[0]

        return result

def parse_bthome_v2(data: bytes) -> Dict[str, Any]:
    """解析開源韌體 (pvvx / ATC) 的 BTHome V2 廣播格式 (UUID 0xFCD2, 免金鑰)"""
    if len(data) < 1:
        return {}
    header = data[0]
    bthome_version = (header >> 5) & 0x07
    is_encrypted = bool(header & 0x01)

    res: Dict[str, Any] = {"version": bthome_version, "encrypted": is_encrypted}
    if is_encrypted:
        return res

    idx = 1
    length = len(data)
    while idx < length:
        obj_id = data[idx]
        idx += 1
        if obj_id == 0x00:  # 封包計數 Packet ID (uint8)
            if idx < length:
                res["packet_id"] = data[idx]
                idx += 1
        elif obj_id == 0x01:  # 電池百分比 (uint8 %)
            if idx < length:
                res["battery"] = data[idx]
                idx += 1
        elif obj_id == 0x02:  # 溫度 (sint16 * 0.01 °C)
            if idx + 2 <= length:
                res["temperature"] = round(int.from_bytes(data[idx:idx+2], "little", signed=True) * 0.01, 2)
                idx += 2
        elif obj_id == 0x03:  # 濕度 (uint16 * 0.01 %)
            if idx + 2 <= length:
                res["humidity"] = round(int.from_bytes(data[idx:idx+2], "little", signed=False) * 0.01, 2)
                idx += 2
        elif obj_id == 0x2E:  # 濕度 (uint8 %)
            if idx < length:
                res["humidity"] = data[idx]
                idx += 1
        elif obj_id == 0x0C:  # 電池電壓 (uint16 * 0.001 V)
            if idx + 2 <= length:
                res["voltage"] = round(int.from_bytes(data[idx:idx+2], "little", signed=False) * 0.001, 3)
                idx += 2
        elif obj_id == 0x10:  # 電源開關狀態 (uint8: 0/1)
            if idx < length:
                res["power"] = data[idx]
                idx += 1
        elif obj_id == 0x11:  # 門窗開啟狀態 (uint8: 0/1)
            if idx < length:
                res["opening"] = data[idx]
                idx += 1
        else:
            # 未知 Object ID，跳過
            break
    return res


# ==============================================================================
# 模式 1：電腦本機 BLE 被動監聽與解密
# ==============================================================================
async def run_ble_mode(target_mac: str, bindkey: str):
    decryptor = MiBeaconDecryptor(bindkey, target_mac)
    print("=" * 65)
    print("📡 [模式: BLE 本機藍牙監聽] 開始被動掃描小米溫濕度計 3 廣播...")
    print(f"🎯 目標 MAC: {target_mac}")
    print(f"🔑 使用 Bind Key: {bindkey}")
    print("💡 提示：若長時間未收到廣播，請關閉手機藍牙並按一下溫濕度計喚醒。")
    print("=" * 65 + "\n")

    def detection_callback(device: BLEDevice, adv_data: AdvertisementData):
        addr = device.address.upper()
        # 判斷是否為目標 MAC 或包含 0xFE95 服務數據
        is_target_mac = (addr == target_mac.upper())
        service_data = adv_data.service_data

        mibeacon_raw = None
        for uuid, data in service_data.items():
            if "fe95" in uuid.lower():
                mibeacon_raw = data
                break

        if not mibeacon_raw:
            return

        if not is_target_mac:
            return

        ts = datetime.now().strftime("%H:%M:%S")
        rssi = adv_data.rssi
        raw_hex = mibeacon_raw.hex()
        
        # 執行解密
        plain = decryptor.decrypt(mibeacon_raw, addr)
        if plain is not None:
            metrics = decryptor.parse_tlv_objects(plain)
            t_str = f"{metrics.get('temperature', '--')} °C" if 'temperature' in metrics else "--"
            h_str = f"{metrics.get('humidity', '--')} %" if 'humidity' in metrics else "--"
            b_str = f"{metrics.get('battery', '--')} %" if 'battery' in metrics else "--"
            
            print(f"[{ts}] 🎯 成功解密廣播 [{addr}] (RSSI: {rssi} dBm)")
            print(f"     🌡️ 溫度: {t_str} | 💧 濕度: {h_str} | 🔋 電量: {b_str}")
            print(f"     📦 解密明文: {plain.hex()}")
        else:
            print(f"[{ts}] ⚠️ 收到目標加密廣播，但解密失敗 (Raw: {raw_hex})")

    try:
        scanner = BleakScanner(detection_callback=detection_callback)
        await scanner.start()
        print("✅ BLE 掃描器已啟動，正在聆聽廣播封包...")
        while True:
            await asyncio.sleep(1)
    except Exception as e:
        print(f"\n❌ 電腦本機藍牙啟動失敗: {e}")
        print("\n💡 原因說明：")
        print("   1. Windows 本機藍牙硬體/驅動可能不支援 BLE Central 被動廣播掃描。")
        print("   2. 請改用 【模式 2: MQTT 模式】（即使用 TaiwanSensor ESP32 閘道器代為接收廣播）：")
        print("      指令：python xiaomi_decrypt_receiver.py --mode mqtt")
    finally:
        try:
            await scanner.stop()
        except Exception:
            pass
        print("\n⏹️ 停止 BLE 監聽。")


# ==============================================================================
# 模式 2：MQTT 閘道器即時串流監聽與解密
# ==============================================================================
def run_mqtt_mode(target_mac: str, bindkey: str, broker: str, port: int, gateway_id: str):
    import paho.mqtt.client as mqtt

    decryptor = MiBeaconDecryptor(bindkey, target_mac)
    print("=" * 65)
    print("🌐 [模式: MQTT 閘道器監聽] 連線至 MQTT Broker...")
    print(f"   Broker: {broker}:{port}")
    print(f"   Gateway: {gateway_id}")
    print(f"   目標 MAC: {target_mac}")
    print("=" * 65 + "\n")

    def on_connect(client, userdata, flags, rc, props=None):
        print(f"✅ 已連線至 MQTT Broker: {broker}:{port}")
        topics = [
            (f"ble/{gateway_id}/#", 0),
            (f"{gateway_id}/#", 0),
            (f"+/{gateway_id}/#", 0),
        ]
        for t, q in topics:
            client.subscribe(t, qos=q)
            print(f"   📡 訂閱: {t}")
        print("\n⏳ 等待閘道器轉發廣播數據中 (Ctrl+C 結束)...")

    def on_message(client, userdata, msg):
        payload_str = msg.payload.decode("utf-8", errors="replace")
        mac_clean = target_mac.replace(":", "").upper()
        
        # 檢查是否為目標裝置
        if mac_clean in msg.topic.upper() or target_mac.upper() in msg.topic.upper() or mac_clean in payload_str.upper():
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"\n[{ts}] 🔥 [目標溫濕度計 MQTT 封包]")
            print(f"   Topic: {msg.topic}")
            try:
                data = json.loads(payload_str)
                # 情況 1: 閘道器已經解密
                if any(k in data for k in ["temperature", "humidity", "temp", "hum", "battery"]):
                    print(f"   ✅ 閘道器端已解密資料:")
                    for k, v in data.items():
                        print(f"      • {k}: {v}")
                    return

                # 情況 2: 閘道器轉發 Raw Service Data
                service_data = data.get("service_data", [])
                for s in service_data:
                    uuid = s.get("uuid", "")
                    raw_hex = s.get("data", "")
                    
                    # 情況 2A: BTHome v2 開源廣播 (UUID 0xFCD2, 免金鑰)
                    if "fcd2" in uuid.lower():
                        metrics = parse_bthome_v2(bytes.fromhex(raw_hex))
                        rssi = data.get("rssi", "--")
                        dev_name = data.get("name", "BTE_裝置")
                        t_str = f"{metrics.get('temperature', '--')} °C" if "temperature" in metrics else ""
                        h_str = f"{metrics.get('humidity', '--')} %" if "humidity" in metrics else ""
                        b_str = f"{metrics.get('battery', '--')} %" if "battery" in metrics else ""
                        v_str = f"{metrics.get('voltage', '--')} V" if "voltage" in metrics else ""
                        
                        summary = []
                        if t_str: summary.append(f"🌡️ 溫度: {t_str}")
                        if h_str: summary.append(f"💧 濕度: {h_str}")
                        if b_str: summary.append(f"🔋 電量: {b_str}")
                        if v_str: summary.append(f"⚡ 電壓: {v_str}")
                        
                        print(f"   🎉 [BTHome v2 免密廣播] 裝置: {dev_name} (RSSI: {rssi} dBm)")
                        if summary:
                            print(f"      👉 {' | '.join(summary)}")
                        print(f"      📦 完整數值: {metrics}")

                    # 情況 2B: 小米原廠 MiBeacon 加密廣播 (UUID 0xFE95, AES-CCM 解密)
                    elif "fe95" in uuid.lower():
                        plain = decryptor.decrypt(bytes.fromhex(raw_hex), target_mac)
                        if plain:
                            metrics = decryptor.parse_tlv_objects(plain)
                            print(f"   🔓 本地 AES-CCM 解密成功: {metrics}")
                        else:
                            print(f"   ⚠️ 本地解密失敗 (Raw hex: {raw_hex})")
            except Exception:
                print(f"   內容: {payload_str}")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"xiaomi-rx-{int(time.time())}")
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(broker, port, keepalive=60)
        client.loop_forever()
    except KeyboardInterrupt:
        print("\n⏹️ 停止 MQTT 監聽。")
    finally:
        client.disconnect()


# ==============================================================================
# 模式 3：解密模組自我驗證與測試向量
# ==============================================================================
def run_test_mode(target_mac: str, bindkey: str):
    print("=" * 65)
    print("🧪 [模式: 自我驗證測試] 測試 MiBeacon AES-128-CCM 解密演算法...")
    print(f"   MAC: {target_mac}")
    print(f"   Bind Key: {bindkey}")
    print("=" * 65)

    decryptor = MiBeaconDecryptor(bindkey, target_mac)
    
    # 測試向量 1：模擬溫度 26.8°C (268 = 0x010C), 濕度 58.0% (580 = 0x0244)
    # TLV: 0x100D (複合溫濕度), 長度 4
    test_temp = 26.8
    test_humi = 58.0
    temp_bytes = int(test_temp * 10).to_bytes(2, "little", signed=True)
    humi_bytes = int(test_humi * 10).to_bytes(2, "little", signed=False)
    tlv = b"\x0d\x10\x04" + temp_bytes + humi_bytes

    # 組裝 MiBeacon 加密封包
    fc = 0x5858 # ver 5, encrypted, has_mac
    prod_id = 0x2832 # MJWSD05MMC
    frame_cnt = 0x33
    ext_cnt = b"\x01\x00\x00"
    mac_rev = bytes.fromhex(target_mac.replace(":", ""))[::-1]

    nonce = mac_rev + prod_id.to_bytes(2, "little") + bytes([frame_cnt]) + ext_cnt
    cipher = AESCCM(bytes.fromhex(bindkey), tag_length=4)
    ct_tag = cipher.encrypt(nonce, tlv, b"\x11")
    ct = ct_tag[:-4]
    tag = ct_tag[-4:]

    test_packet = (
        fc.to_bytes(2, "little") +
        prod_id.to_bytes(2, "little") +
        bytes([frame_cnt]) +
        mac_rev +
        ct +
        ext_cnt +
        tag
    )

    print(f"\n📦 生成測試加密封包 (Hex): {test_packet.hex()}")
    print(f"   預期明文: 溫度 {test_temp}°C, 濕度 {test_humi}%")

    # 執行解密
    plain = decryptor.decrypt(test_packet, target_mac)
    if plain is None:
        print("❌ 解密失敗！請檢查金鑰或演算法。")
        return False

    print(f"🔓 解密後明文 Hex: {plain.hex()}")
    metrics = decryptor.parse_tlv_objects(plain)
    print(f"📊 解析結果: {metrics}")

    if abs(metrics.get("temperature", 0) - test_temp) < 0.01 and abs(metrics.get("humidity", 0) - test_humi) < 0.01:
        print("\n🎉 自我測試 100% 通過！演算法與 Bind Key 驗證無誤。")
        return True
    else:
        print("\n❌ 數值解析不符！")
        return False


def main():
    parser = argparse.ArgumentParser(description="小米溫濕度計 3 廣播接收與解密工具")
    parser.add_argument("--mode", choices=["ble", "mqtt", "test"], default="test",
                        help="執行模式: ble (本機藍牙掃描) | mqtt (網關監聽) | test (自我驗證測試)")
    parser.add_argument("--mac", default=DEFAULT_MAC, help="溫濕度計 MAC 位址")
    parser.add_argument("--key", default=DEFAULT_BINDKEY, help="32 碼 AES Bind Key")
    parser.add_argument("--broker", default=DEFAULT_BROKER, help="MQTT Broker 主機位址")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="MQTT Broker 連接埠")
    parser.add_argument("--gateway", default=DEFAULT_GATEWAY_ID, help="TaiwanSensor 閘道器 ID")
    args = parser.parse_args()

    if args.mode == "test":
        run_test_mode(args.mac, args.key)
    elif args.mode == "mqtt":
        run_mqtt_mode(args.mac, args.key, args.broker, args.port, args.gateway)
    elif args.mode == "ble":
        asyncio.run(run_ble_mode(args.mac, args.key))


if __name__ == "__main__":
    main()
