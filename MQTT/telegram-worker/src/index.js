/**
 * 小米溫濕度計 → Telegram（Cloudflare Worker + Durable Object）
 *
 * Durable Object 的 alarm 每 5 秒觸發一次：
 *   1. 以 TCP 直連 MQTT broker，訂閱 retained 的最新遙測
 *   2. 資料新鮮 → 發 Telegram；過舊 → 只在狀態改變時通知一次「離線」
 * Cron（每分鐘）負責保活，確保計時器不會中斷。
 */
import { DurableObject } from 'cloudflare:workers';
import { connect } from 'cloudflare:sockets';

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

/** 最小 MQTT 3.1.1 客戶端：連線、訂閱一個 topic，收集 windowMs 毫秒內的所有 PUBLISH 後離線 */
async function collectPackets(env, windowMs = 3500) {
  const socket = connect({ hostname: env.MQTT_HOST, port: Number(env.MQTT_PORT) });
  const writer = socket.writable.getWriter();
  const reader = socket.readable.getReader();
  const clientId = 'cf-tg-' + Math.random().toString(16).slice(2, 10);
  const packets = [];

  const vh = [0, 4, 77, 81, 84, 84, 4, 0x02, 0, 30, ...utf8Field(clientId)];
  await writer.write(new Uint8Array([0x10, ...remainingLength(vh.length), ...vh]));

  const sub = [0, 1, ...utf8Field(env.TOPIC), 0];
  await writer.write(new Uint8Array([0x82, ...remainingLength(sub.length), ...sub]));

  let buf = new Uint8Array(0);
  const deadline = Date.now() + windowMs;
  try {
    while (Date.now() < deadline) {
      const timer = new Promise((r) => setTimeout(() => r({ timeout: true }), deadline - Date.now()));
      const res = await Promise.race([reader.read(), timer]);
      if (res.timeout || res.done) break;
      const merged = new Uint8Array(buf.length + res.value.length);
      merged.set(buf);
      merged.set(res.value, buf.length);
      buf = merged;

      // 逐一解析封包
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
        if (type === 3) {
          const tl = (body[0] << 8) | body[1];
          const payload = body.slice(2 + tl); // QoS 0：沒有 packet id
          try { packets.push(JSON.parse(dec.decode(payload))); } catch (_) {}
        }
      }
    }
  } finally {
    try { await writer.write(new Uint8Array([0xe0, 0])); } catch (_) {}
    try { await socket.close(); } catch (_) {}
  }
  return packets;
}

/** 解析 BTHome V2（UUID 0xFCD2，小米溫濕度計刷 BTE 開源韌體後的格式，免金鑰） */
function parseBTHome(hex) {
  const d = new Uint8Array(hex.match(/../g).map((h) => parseInt(h, 16)));
  const out = {};
  if (d.length < 1 || (d[0] & 0x01)) return out; // 加密封包略過
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
    else break; // 未知欄位
  }
  return out;
}

function formatMessage(s) {
  const f = (v, digits, unit) => (v != null ? `${v.toFixed(digits)} ${unit}` : '--');
  const time = new Date(s.updated).toLocaleTimeString('zh-TW', { timeZone: 'Asia/Taipei', hour12: false });
  const alarm = s.temperature != null && s.temperature > 33 ? '\n🔥 高溫告警！' : '';
  const rssi = s.rssi != null ? `\n訊號：${s.rssi} dBm` : '';
  return `🌡️ 小米溫濕度計（經 MQTT 閘道器）\n溫度：${f(s.temperature, 1, '°C')}\n濕度：${f(s.humidity, 1, '%')}\n電壓：${f(s.voltage, 3, 'V')}\n電量：${s.battery != null ? s.battery + ' %' : '--'}${rssi}\n時間：${time}${alarm}`;
}

async function sendTelegram(env, text) {
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/sendMessage`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chat_id: env.CHAT_ID, text }),
  });
  if (!r.ok) console.log('telegram error', r.status, await r.text());
  return r.ok;
}

export class Ticker extends DurableObject {
  async fetch(request) {
    const path = new URL(request.url).pathname;
    if (path === '/start') {
      const running = await this.ctx.storage.get('running');
      const alarm = await this.ctx.storage.getAlarm();
      if (!running || alarm === null) {
        await this.ctx.storage.put('running', true);
        await this.ctx.storage.setAlarm(Date.now() + 1000);
      }
      return new Response('started\n');
    }
    if (path === '/stop') {
      await this.ctx.storage.put('running', false);
      await this.ctx.storage.deleteAlarm();
      return new Response('stopped\n');
    }
    const status = {
      running: await this.ctx.storage.get('running'),
      nextAlarm: await this.ctx.storage.getAlarm(),
      sent: (await this.ctx.storage.get('sent')) || 0,
      offline: (await this.ctx.storage.get('offline')) || false,
      lastError: (await this.ctx.storage.get('lastError')) || null,
    };
    return Response.json(status);
  }

  async alarm() {
    if (!(await this.ctx.storage.get('running'))) return;
    const started = Date.now();
    try {
      await this.tick();
      await this.ctx.storage.delete('lastError');
    } catch (e) {
      await this.ctx.storage.put('lastError', String(e && e.message ? e.message : e));
    } finally {
      // 以「開始時間」為基準排下一次，盡量維持 5 秒節奏
      const interval = Number(this.env.INTERVAL_MS || 5000);
      await this.ctx.storage.setAlarm(Math.max(Date.now() + 300, started + interval));
    }
  }

  async tick() {
    // 直接訂閱 MQTT 閘道器轉發的 BLE 原始廣播，合併最新溫濕度／電壓封包
    const packets = await collectPackets(this.env);
    const state = (await this.ctx.storage.get('state')) || {};
    for (const p of packets) {
      for (const sd of p.service_data || []) {
        if (!/fcd2/i.test(sd.uuid || '')) continue;
        Object.assign(state, parseBTHome(sd.data || ''));
        state.updated = Date.now();
        if (p.rssi != null) state.rssi = p.rssi;
      }
    }
    await this.ctx.storage.put('state', state);

    const stale = Number(this.env.STALE_SEC || 60);
    const fresh = state.updated && Date.now() - state.updated <= stale * 1000 && state.temperature != null;
    const wasOffline = (await this.ctx.storage.get('offline')) || false;

    if (fresh) {
      if (wasOffline) await sendTelegram(this.env, '✅ 小米溫濕度計已恢復廣播');
      await this.ctx.storage.put('offline', false);
      if (await sendTelegram(this.env, formatMessage(state))) {
        await this.ctx.storage.put('sent', ((await this.ctx.storage.get('sent')) || 0) + 1);
      }
    } else if (!wasOffline) {
      await this.ctx.storage.put('offline', true);
      await sendTelegram(this.env, `⚠️ 超過 ${stale} 秒沒有收到小米溫濕度計廣播（閘道器收不到或溫濕度計沒電）`);
    }
  }
}

function stub(env) {
  return env.TICKER.get(env.TICKER.idFromName('main'));
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === '/stop' && url.searchParams.get('key') !== env.ADMIN_KEY) {
      return new Response('forbidden\n', { status: 403 });
    }
    if (!['/start', '/stop', '/status'].includes(url.pathname)) {
      return new Response('xiaomi-telegram worker\n/status\n');
    }
    return stub(env).fetch(new Request('https://do' + url.pathname));
  },
  async scheduled(event, env) {
    await stub(env).fetch(new Request('https://do/start'));
  },
};
