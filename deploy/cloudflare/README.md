# Telegram webhook on Cloudflare Workers (free tier)

Why: long-polling only works while the laptop is awake. With a webhook, Telegram delivers button
taps to this Worker, which queues them in KV and acknowledges instantly; the laptop drains the
queue on its next cycle (or on wake) and applies each action through the normal workflow code.
Telegram allows **one** update consumer per bot — while the webhook is set, `getUpdates` returns
409 — so the laptop arbitrates: webhook when the Worker is healthy, long-polling fallback when it
is not (`radar/notify/telegram_webhook.py`, reconciled every cycle).

## One-time setup (≈10 minutes, all free)

1. Cloudflare account (free) → `cd deploy/cloudflare && npx wrangler login` (opens a browser).
2. `npx wrangler kv namespace create TAPS` → paste the printed `id` into `wrangler.toml`.
3. Three Worker secrets (values are never in git):
   - `npx wrangler secret put TELEGRAM_SECRET_TOKEN` — any long random string; Telegram will send
     it back in `X-Telegram-Bot-Api-Secret-Token` on every POST. Generate: `openssl rand -hex 32`.
   - `npx wrangler secret put DRAIN_TOKEN` — another random string; the laptop uses it to drain/ack.
   - `npx wrangler secret put BOT_TOKEN` — the bot token, only so the Worker can answer the tap
     instantly ("queued — applied when the laptop wakes"). Optional but worth it.
4. `npx wrangler deploy` → note the URL, e.g. `https://job-radar-telegram.<you>.workers.dev`.
5. On the laptop, store the three values the same way as the bot token (hidden input, 0600):
   ```
   radar secret set TELEGRAM_WEBHOOK_URL        # https://job-radar-telegram.<you>.workers.dev
   radar secret set TELEGRAM_WEBHOOK_SECRET     # the TELEGRAM_SECRET_TOKEN value
   radar secret set TELEGRAM_DRAIN_TOKEN        # the DRAIN_TOKEN value
   radar telegram-webhook reconcile             # health-checks the Worker, sets the webhook, switches mode
   radar telegram-webhook status
   ```
That's it: the next cycle (and every cycle) reconciles — Worker healthy → webhook mode, polling
listener idles; Worker unreachable → webhook deleted, listener long-polls. Only one consumer is
ever active.

Free-tier budget: Workers 100k requests/day; KV 1,000 writes/day (one per tap), 100k reads/day.
