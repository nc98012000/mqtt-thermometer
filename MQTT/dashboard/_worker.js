/**
 * Cloudflare Pages _worker.js (極速響應優化版)
 * 
 * 1. 靜態儀表板網頁 (env.ASSETS)
 * 2. Telegram Webhook 機器人 (/webhook)
 * 3. 毫秒級即時快顯：輸入 /temp 即刻回應「⏳ 讀取中...」，獲取數據後無縫原地刷新！
 * 4. 記憶體快取：15 秒內重覆查詢 0.3 秒閃電直出！
 */

import { connect } from 'cloudflare:sockets';

const TELEGRAM_TOKEN = "8749430348:AAEyRAfyHMVP1kg3YHW4nfgqPnGk28ybBag";
const MQTT_HOST = "broker.emqx.io";
const MQTT_PORT = 1883;
const MQTT_TOPIC = "ble/esp32-eth-ble-mqtt-x4-2c8628/raw/A4C1384A09DE";

// 全域記憶體快取 (在 Cloudflare 邊緣節點熱駐留)
let latestReading = {
  temperature: null,
  humidity: null,
  voltage: null,
  battery: null,
  rssi: null,
  updated: 0
};

const enc = new TextEncoder();
const dec = new TextDecoder();

function utf8Field(s) {
  const b = enc.encode(s);
  return [b.length >> 8, b.length & 0xff, ...b];
}

function remainingLength(n) {
  const out = [];
  do {
    let d = n % 128;
    n = Math.floor(n / 128);
    if (n > 0) d |= 0x80;
    out.push(d);
  } while (n > 0);
  return out;
}

// 快速 MQTT 接收器：連線至 EMQX 擷取 TaiwanSensor 閘道器最新廣播 (取得溫度即立刻返回)
async function fetchGatewayPacket(timeoutMs = 7000) {
  const socket = connect({ hostname: MQTT_HOST, port: MQTT_PORT });
  const writer = socket.writable.getWriter();
  const reader = socket.readable.getReader();
  const clientId = 'cf-tg-' + Math.random().toString(16).slice(2, 10);

  // 1. MQTT CONNECT
  const vh = [0, 4, 77, 81, 84, 84, 4, 0x02, 0, 30, ...utf8Field(clientId)];
  await writer.write(new Uint8Array([0x10, ...remainingLength(vh.length), ...vh]));

  // 2. 等候 CONNACK
  const cRes = await reader.read();
  if (!cRes.value || (cRes.value[0] >> 4) !== 2) {
    try { await socket.close(); } catch (_) {}
    return latestReading;
  }

  // 3. MQTT SUBSCRIBE
  const sub = [0, 1, ...utf8Field(MQTT_TOPIC), 0];
  await writer.write(new Uint8Array([0x82, ...remainingLength(sub.length), ...sub]));

  // 4. 等候 SUBACK
  const sRes = await reader.read();
  let buf = new Uint8Array(sRes.value || []);
  const deadline = Date.now() + timeoutMs;

  try {
    while (Date.now() < deadline) {
      while (buf.length >= 2) {
        let mul = 1, len = 0, i = 1, b;
        do {
          if (i >= buf.length) { b = null; break; }
          b = buf[i++];
          len += (b & 0x7f) * mul;
          mul *= 128;
        } while (b & 0x80);

        if (b === null || buf.length < i + len) break;
        const type = buf[0] >> 4;
        const body = buf.slice(i, i + len);
        buf = buf.slice(i + len);

        if (type === 3) { // PUBLISH
          const tl = (body[0] << 8) | body[1];
          const payload = body.slice(2 + tl);
          try {
            const json = JSON.parse(dec.decode(payload));
            if (json.rssi != null) latestReading.rssi = json.rssi;
            for (const s of json.service_data || []) {
              if (/fcd2/i.test(s.uuid || "")) {
                const parsed = parseBTHome(s.data || "");
                Object.assign(latestReading, parsed);
                latestReading.updated = Date.now();
                // 只要一拿到溫度數值，立刻返回，無需繼續等待！
                if (latestReading.temperature != null) {
                  return latestReading;
                }
              }
            }
          } catch (_) {}
        }
      }

      const remaining = deadline - Date.now();
      if (remaining <= 0) break;

      const timer = new Promise((r) => setTimeout(() => r({ timeout: true }), remaining));
      const res = await Promise.race([reader.read(), timer]);
      if (res.timeout || res.done) break;

      const merged = new Uint8Array(buf.length + res.value.length);
      merged.set(buf);
      merged.set(res.value, buf.length);
      buf = merged;
    }
  } finally {
    try { await writer.write(new Uint8Array([0xe0, 0])); } catch (_) {}
    try { await socket.close(); } catch (_) {}
  }

  return latestReading;
}

// BTHome V2 廣播解碼
function parseBTHome(hex) {
  if (!hex) return {};
  const d = new Uint8Array(hex.match(/../g).map((h) => parseInt(h, 16)));
  const out = {};
  if (d.length < 1 || (d[0] & 0x01)) return out;

  let i = 1;
  const u16 = (p) => d[p] | (d[p + 1] << 8);
  while (i < d.length) {
    const id = d[i++];
    if (id === 0x00 || id === 0x10 || id === 0x11) { i += 1; }
    else if (id === 0x01) { out.battery = d[i]; i += 1; }
    else if (id === 0x02) { out.temperature = ((u16(i) << 16) >> 16) * 0.01; i += 2; }
    else if (id === 0x03) { out.humidity = u16(i) * 0.01; i += 2; }
    else if (id === 0x2e) { out.humidity = d[i]; i += 1; }
    else if (id === 0x0c) { out.voltage = u16(i) * 0.001; i += 2; }
    else break;
  }
  return out;
}

// 發送初始 Telegram 訊息
async function sendTelegramMessage(chatId, text, showButton = true) {
  const body = {
    chat_id: chatId,
    text: text,
    parse_mode: "HTML"
  };
  if (showButton) {
    body.reply_markup = {
      inline_keyboard: [
        [{ text: "🔄 重新整理讀取最新溫濕度", callback_data: "refresh" }]
      ]
    };
  }

  const res = await fetch(`https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  return await res.json();
}

// 原地更新已發送的 Telegram 訊息
async function editTelegramMessage(chatId, messageId, text) {
  await fetch(`https://api.telegram.org/bot${TELEGRAM_TOKEN}/editMessageText`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chat_id: chatId,
      message_id: messageId,
      text: text,
      parse_mode: "HTML",
      reply_markup: {
        inline_keyboard: [
          [{ text: "🔄 重新整理讀取最新溫濕度", callback_data: "refresh" }]
        ]
      }
    })
  });
}

function renderMessage(data) {
  const time = new Date(data.updated || Date.now()).toLocaleTimeString("zh-TW", { timeZone: "Asia/Taipei", hour12: false });
  const t = data.temperature != null ? `<b>${data.temperature.toFixed(2)} °C</b>` : "--";
  const h = data.humidity != null ? `<b>${data.humidity.toFixed(1)} %</b>` : "--";
  const v = data.voltage != null ? `${data.voltage.toFixed(3)} V` : "--";
  const b = data.battery != null ? `${data.battery} %` : "--";
  const rssi = data.rssi != null ? `${data.rssi} dBm` : "--";

  let statusIcon = "🟢 正常";
  if (data.temperature != null && data.temperature >= 33) {
    statusIcon = "🔥 高溫警戒";
  }

  return `🌡️ <b>小米溫濕度計即時回報</b>
━━━━━━━━━━━━━━━━━━
• 當前溫度：${t}
• 當前濕度：${h}
• 電池電量：${b} (${v})
• 藍牙訊號：${rssi}
• 系統狀態：${statusIcon}
━━━━━━━━━━━━━━━━━━
🕒 讀取時間：${time}
📡 資料來源：TaiwanSensor 硬體閘道器 (免開電腦)`;
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // 0. 偵錯測試：瀏覽 https://mqtt-thermometer.pages.dev/debug
    if (url.pathname === "/debug") {
      const logs = [];
      try {
        logs.push(`Connecting to ${MQTT_HOST}:${MQTT_PORT}...`);
        const socket = connect({ hostname: MQTT_HOST, port: MQTT_PORT });
        const writer = socket.writable.getWriter();
        const reader = socket.readable.getReader();
        const clientId = 'cf-dbg-' + Math.random().toString(16).slice(2, 10);
        const vh = [0, 4, 77, 81, 84, 84, 4, 0x02, 0, 30, ...utf8Field(clientId)];
        await writer.write(new Uint8Array([0x10, ...remainingLength(vh.length), ...vh]));
        
        const cRes = await reader.read();
        logs.push(`CONNACK: ${Array.from(cRes.value || []).map(b => b.toString(16).padStart(2, '0')).join(' ')}`);

        const sub = [0, 1, ...utf8Field(MQTT_TOPIC), 0];
        await writer.write(new Uint8Array([0x82, ...remainingLength(sub.length), ...sub]));

        const sRes = await reader.read();
        logs.push(`SUBACK: ${Array.from(sRes.value || []).map(b => b.toString(16).padStart(2, '0')).join(' ')}`);

        const start = Date.now();
        while (Date.now() - start < 7000) {
          const rem = 7000 - (Date.now() - start);
          const t = new Promise(r => setTimeout(() => r({ timeout: true }), rem));
          const r = await Promise.race([reader.read(), t]);
          if (r.timeout || r.done) break;
          logs.push(`Packet (${r.value.length}b): ${dec.decode(r.value)}`);
          break;
        }
        await socket.close();
      } catch (e) {
        logs.push(`Error: ${e.message}\n${e.stack}`);
      }
      return new Response(logs.join('\n'), { headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
    }

    // 1. 設定 Webhook: 瀏覽 https://mqtt-thermometer.pages.dev/set-webhook
    if (url.pathname === "/set-webhook") {
      const webhookUrl = `${url.origin}/webhook`;
      const tgRes = await fetch(`https://api.telegram.org/bot${TELEGRAM_TOKEN}/setWebhook?url=${encodeURIComponent(webhookUrl)}`);
      const resJson = await tgRes.json();
      return new Response(JSON.stringify(resJson, null, 2), {
        headers: { "Content-Type": "application/json; charset=utf-8" }
      });
    }

    // 2. 接收 Telegram Webhook POST 訊息
    if (url.pathname === "/webhook" && request.method === "POST") {
      try {
        const update = await request.json();
        let chatId = null;
        let callbackQueryId = null;
        let existingMessageId = null;

        if (update.message) {
          chatId = update.message.chat.id;
          const chatType = update.message.chat.type || "private";
          const text = (update.message.text || "").trim().toLowerCase();

          // 群組模式過濾
          if (chatType === "group" || chatType === "supergroup") {
            const isCommand = text.startsWith("/temp") || 
                              text.startsWith("/start") || 
                              text.includes("@nc9801xaiomibot") ||
                              text.includes("溫濕度") ||
                              text.includes("溫度");
            if (!isCommand) {
              chatId = null;
            }
          }
        } else if (update.callback_query) {
          chatId = update.callback_query.message.chat.id;
          callbackQueryId = update.callback_query.id;
          existingMessageId = update.callback_query.message.message_id;
        }

        if (chatId) {
          // 若點擊按鈕，立即提示正在獲取
          if (callbackQueryId) {
            fetch(`https://api.telegram.org/bot${TELEGRAM_TOKEN}/answerCallbackQuery`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ callback_query_id: callbackQueryId, text: "🔄 正在向閘道器讀取最新數據..." })
            }).catch(() => {});
          }

          const processQuery = async () => {
            // 🚀 極速優化：若 15 秒內已有有效快取，直接 0.2 秒秒回！
            const isFresh = (Date.now() - latestReading.updated < 15000) && (latestReading.temperature != null);
            if (isFresh) {
              if (existingMessageId) {
                await editTelegramMessage(chatId, existingMessageId, renderMessage(latestReading));
              } else {
                await sendTelegramMessage(chatId, renderMessage(latestReading));
              }
              return;
            }

            // ⚡ 0.3 秒即時回應用戶「讀取中...」，解決慢卡感覺！
            let targetMid = existingMessageId;
            if (!targetMid) {
              const placeholderRes = await sendTelegramMessage(chatId, "⏳ 正在連線 TaiwanSensor 閘道器獲取即時溫濕度，請稍候...", false);
              targetMid = placeholderRes?.result?.message_id;
            }

            // 在背景拉取 MQTT 數據 (拿到溫度即刻退出)
            await fetchGatewayPacket(7000);

            // 原地將「讀取中...」更新為漂亮溫濕度報表！
            if (latestReading.temperature != null) {
              if (targetMid) {
                await editTelegramMessage(chatId, targetMid, renderMessage(latestReading));
              } else {
                await sendTelegramMessage(chatId, renderMessage(latestReading));
              }
            } else {
              const errMsg = "⚠️ 暫時未能從閘道器獲取封包，請確認溫濕度計在收訊範圍內，點擊下方重試。";
              if (targetMid) {
                await editTelegramMessage(chatId, targetMid, errMsg);
              } else {
                await sendTelegramMessage(chatId, errMsg);
              }
            }
          };

          if (ctx && ctx.waitUntil) {
            ctx.waitUntil(processQuery());
          } else {
            await processQuery();
          }
        }
      } catch (err) {
        console.error("Webhook error:", err);
      }
      return new Response("OK");
    }

    // 3. 靜態儀表板
    return env.ASSETS.fetch(request);
  }
};
