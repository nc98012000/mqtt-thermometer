"""小米溫濕度計 3 (MJWSD05MMC) 藍牙 BLE 直連與讀取測試程式
支援：
1. 掃描附近的小米溫濕度計或指定 MAC (A4:C1:38:4A:09:DE)
2. 建立 BLE GATT 連線
3. 讀取即時溫度、濕度、電池電壓與裝置資訊
4. 監聽即時溫濕度 Notify 廣播
"""
import asyncio
import sys
import io

# Windows UTF-8 控制台設定
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

from bleak import BleakScanner, BleakClient

TARGET_MAC = "A4:C1:38:4A:09:DE"
TARGET_NAME = "MJWSD05MMC"

# 小米標準 GATT 服務與特徵值 UUID
UUID_TEMP_NOTIFY = "ebe0ccc1-7a0a-4b0c-8a1a-6ff2997da3a6"   # 即時溫濕度特徵值
UUID_CLOCK       = "ebe0ccb7-7a0a-4b0c-8a1a-6ff2997da3a6"   # 時間特徵值
UUID_MODEL       = "00002a24-0000-1000-8000-00805f9b34fb"   # 型號 (Model)
UUID_FIRMWARE    = "00002a26-0000-1000-8000-00805f9b34fb"   # 韌體版本 (Firmware)
UUID_HARDWARE    = "00002a27-0000-1000-8000-00805f9b34fb"   # 硬體版本 (Hardware)

def parse_temp_humi(data: bytearray):
    """解析小米溫濕度特徵值格式 (Little-Endian)
    Byte 0-1: 溫度 (int16 / 100)
    Byte 2:   濕度 (uint8 %)
    Byte 3-4: 電池電壓 (uint16 mV)
    """
    if len(data) >= 5:
        temp = int.from_bytes(data[0:2], byteorder='little', signed=True) / 100.0
        humi = data[2]
        vbat = int.from_bytes(data[3:5], byteorder='little', signed=False)
        return temp, humi, vbat
    return None, None, None

def notification_handler(sender, data: bytearray):
    temp, humi, vbat = parse_temp_humi(data)
    if temp is not None:
        print(f"📡 [即時推播] 溫度: {temp:.2f}°C | 濕度: {humi}% | 電池: {vbat} mV ({vbat/1000:.2f}V)")
    else:
        print(f"📡 [即時推播 Raw]: {data.hex()}")

async def main():
    print("=" * 60)
    print("🔍 [步驟 1/3] 開始掃描附近藍牙設備（搜尋 8 秒）...")
    print(f"   目標裝置: {TARGET_NAME} 或 MAC: {TARGET_MAC}")
    print("=" * 60)

    devices = await BleakScanner.discover(timeout=8.0)
    target_device = None

    for d in devices:
        name = d.name or "未知裝置"
        addr = d.address
        rssi = d.rssi
        # 尋找名稱符合或 MAC 符合的設備
        if TARGET_NAME.lower() in name.lower() or addr.upper() == TARGET_MAC.upper():
            print(f"🎯 找到目標設備: [{addr}] {name} (RSSI: {rssi} dBm)")
            target_device = d
        elif "mjwsd" in name.lower() or "lywsd" in name.lower() or "miaomiaoce" in name.lower():
            print(f"💡 發現其他小米感測器: [{addr}] {name} (RSSI: {rssi} dBm)")
            if not target_device:
                target_device = d

    if not target_device:
        print(f"\n⚠️ 未直接掃描到名稱為 {TARGET_NAME} 的裝置，嘗試直接使用 MAC [{TARGET_MAC}] 嘗試連線...")
        target_address = TARGET_MAC
    else:
        target_address = target_device.address

    print("\n" + "=" * 60)
    print(f"🔗 [步驟 2/3] 正在建立與 [{target_address}] 的藍牙 GATT 直連...")
    print("=" * 60)

    try:
        async with BleakClient(target_address, timeout=12.0) as client:
            connected = client.is_connected
            print(f"✅ 連線成功！連線狀態: {connected}")
            print(f"   設備位址: {client.address}")

            print("\n📋 [步驟 3/3] 讀取設備資訊與即時數據...")
            
            # 讀取裝置型號
            try:
                model = await client.read_gatt_char(UUID_MODEL)
                print(f"   🔹 型號: {model.decode(errors='ignore')}")
            except Exception:
                pass

            # 讀取韌體版本
            try:
                fw = await client.read_gatt_char(UUID_FIRMWARE)
                print(f"   🔹 韌體版本: {fw.decode(errors='ignore')}")
            except Exception:
                pass

            # 讀取即時溫濕度特徵值
            print("\n🌡️ 正在讀取即時溫濕度數據...")
            try:
                val = await client.read_gatt_char(UUID_TEMP_NOTIFY)
                temp, humi, vbat = parse_temp_humi(val)
                if temp is not None:
                    print(f"   📊 目前溫度: {temp:.2f} °C")
                    print(f"   📊 目前濕度: {humi} %")
                    print(f"   🔋 電池電壓: {vbat} mV ({vbat/1000:.2f}V)")
                else:
                    print(f"   📊 原始特徵值資料: {val.hex()}")
            except Exception as e:
                print(f"   ⚠️ 讀取溫濕度特徵值失敗: {e}")

            # 訂閱即時通知 10 秒
            print("\n🔔 開啟即時通知 (Notify) 監聽 10 秒...")
            try:
                await client.start_notify(UUID_TEMP_NOTIFY, notification_handler)
                for i in range(10, 0, -1):
                    await asyncio.sleep(1)
                    print(f"   監聽倒數: {i} 秒...", end="\r", flush=True)
                await client.stop_notify(UUID_TEMP_NOTIFY)
                print("\n   監聽結束。")
            except Exception as e:
                print(f"\n   ⚠️ 訂閱 Notify: {e}")

            print("\n🎉 測試完成，主動中斷藍牙連線。")

    except Exception as e:
        print(f"\n❌ 藍牙連線失敗: {e}")
        print("\n💡 排查提示：")
        print("1. 確認電腦本機「藍牙」已開啟。")
        print("2. 確認手機沒有連著溫濕度計（手機藍牙請關閉）。")
        print("3. 把溫濕度計靠近電腦（1公尺內），或按一下溫濕度計喚醒。")

if __name__ == "__main__":
    asyncio.run(main())
