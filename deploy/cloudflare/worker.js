/**
 * Job Radar — Telegram webhook receiver (Cloudflare Worker, free tier).
 *
 * Telegram POSTs every update here while the webhook is set. The Worker:
 *   1. rejects anything without the right X-Telegram-Bot-Api-Secret-Token (the value you gave
 *      setWebhook as `secret_token`);
 *   2. writes the update to KV under tap:<update_id> (KV free tier: 1,000 writes/day — taps, not
 *      postings) and returns 200 immediately so Telegram never retries;
 *   3. if BOT_TOKEN is set, answers the callback at once ("queued — applied when the laptop wakes")
 *      so the button feels alive even with the lid closed.
 * The laptop drains with GET /drain and acknowledges with POST /ack, both authenticated by
 * DRAIN_TOKEN. Nothing here mutates Job Radar state; the laptop applies every action through the
 * same code path as a dashboard click.
 */
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/health") return json({ ok: true, ts: Date.now() });

    if (url.pathname === "/telegram" && request.method === "POST") {
      const given = request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "";
      if (!env.TELEGRAM_SECRET_TOKEN || !timingSafeEqual(given, env.TELEGRAM_SECRET_TOKEN)) {
        return new Response("forbidden", { status: 403 });
      }
      let update;
      try {
        update = await request.json();
      } catch {
        return new Response("bad json", { status: 400 });
      }
      if (update && typeof update.update_id === "number") {
        const key = `tap:${String(update.update_id).padStart(12, "0")}`;
        await env.TAPS.put(key, JSON.stringify({ received_at: new Date().toISOString(), update }), {
          expirationTtl: 60 * 60 * 24 * 14, // two weeks: long enough for any vacation
        });
        const cq = update.callback_query;
        if (cq && env.BOT_TOKEN) {
          // instant acknowledgement; the real state change happens on the laptop
          await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/answerCallbackQuery`, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ callback_query_id: cq.id, text: "queued — applied when the laptop wakes" }),
          }).catch(() => {});
        }
      }
      return new Response("ok", { status: 200 });
    }

    // laptop side
    const auth = request.headers.get("Authorization") || "";
    if (!env.DRAIN_TOKEN || !timingSafeEqual(auth, `Bearer ${env.DRAIN_TOKEN}`)) {
      return new Response("forbidden", { status: 403 });
    }
    if (url.pathname === "/drain" && request.method === "GET") {
      const list = await env.TAPS.list({ prefix: "tap:", limit: 200 });
      const items = [];
      for (const k of list.keys) {
        const v = await env.TAPS.get(k.name);
        if (v) items.push({ key: k.name, ...JSON.parse(v) });
      }
      return json({ items, more: !list.list_complete });
    }
    if (url.pathname === "/ack" && request.method === "POST") {
      const body = await request.json().catch(() => ({}));
      const keys = Array.isArray(body.keys) ? body.keys.filter((k) => typeof k === "string" && k.startsWith("tap:")) : [];
      await Promise.all(keys.map((k) => env.TAPS.delete(k)));
      return json({ deleted: keys.length });
    }
    return new Response("not found", { status: 404 });
  },
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let out = 0;
  for (let i = 0; i < a.length; i++) out |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return out === 0;
}
