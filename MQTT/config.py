"""MQTT 共用設定（模擬器與訂閱程式共用）"""

# 免費公開 broker（不需帳密，任何人都能看到你的訊息，請勿傳送敏感資料）
# 備選：test.mosquitto.org、broker.hivemq.com
BROKER_HOST = "broker.emqx.io"
BROKER_PORT = 1883

# 公開 broker 大家共用，topic 前綴要夠獨特才不會跟別人撞到
TOPIC_PREFIX = "pspasia/test1"

# 模擬的裝置
DEVICES = ["sensor-01", "sensor-02", "sensor-03"]

# 真實裝置（由 xiaomi_to_mqtt.py 發佈的小米溫濕度計），不由模擬器產生
REAL_DEVICES = ["xiaomi-01"]

# 儀表板 / GUI 顯示用的全部裝置
ALL_DEVICES = DEVICES + REAL_DEVICES

# 發送間隔（秒）
PUBLISH_INTERVAL = 2.0
