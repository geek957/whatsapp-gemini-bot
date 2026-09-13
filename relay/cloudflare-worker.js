/**
 * Optional: turn a WhatsApp provider webhook into a GitHub `repository_dispatch`.
 *
 * Why a relay is needed at all: GitHub Actions cannot receive inbound HTTP, and
 * `repository_dispatch` requires both an `Authorization` header and a body shaped as
 * {event_type, client_payload}. Webhook providers let you set a URL but not both of
 * those, so ~40 lines of glue sit in between.
 *
 * The bot works without this: the scheduled run polls Green API's queue. Add the relay
 * only when you want replies in ~30s instead of up to ~20 minutes.
 *
 * Deploy (free tier is enough):
 *   npx wrangler deploy relay/cloudflare-worker.js --name whatsapp-relay
 *   npx wrangler secret put GITHUB_TOKEN     # fine-grained PAT, Contents:read + Actions:write on the one repo
 *   npx wrangler secret put RELAY_SECRET     # shared secret, appended to the webhook URL as ?token=
 *   npx wrangler secret put GITHUB_REPO      # e.g. yourname/whatsapp-gemini-bot
 *   npx wrangler secret put COMMAND_PREFIX   # optional, default /ask
 *
 * Then set the webhook URL in the Green API console to:
 *   https://whatsapp-relay.<subdomain>.workers.dev/?token=<RELAY_SECRET>
 *
 * IMPORTANT: enabling a webhookUrl in Green API can stop the pull queue from filling.
 * If you use the relay, set READ_MODE=history so the cron sweep still backfills, and
 * confirm with `python -m bot.cli doctor`.
 */

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("method not allowed", { status: 405 });
    }

    // Constant-time-ish shared secret check on the query string.
    const url = new URL(request.url);
    const token = url.searchParams.get("token") || "";
    if (!env.RELAY_SECRET || !safeEqual(token, env.RELAY_SECRET)) {
      return new Response("forbidden", { status: 403 });
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return new Response("bad json", { status: 400 });
    }

    // Only dispatch on inbound messages that could plausibly trigger work. Filtering
    // here is what keeps a chatty group from burning Actions minutes.
    if (!shouldDispatch(body, env.COMMAND_PREFIX || "/ask")) {
      return new Response("ignored", { status: 200 });
    }

    const repo = env.GITHUB_REPO;
    const response = await fetch(`https://api.github.com/repos/${repo}/dispatches`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "whatsapp-relay",
      },
      body: JSON.stringify({
        event_type: "whatsapp-message",
        // The bot re-reads from the provider rather than trusting this payload, so only
        // a small hint is forwarded. Keeps message content out of GitHub's event log.
        client_payload: {
          chat_id: chatIdOf(body) || "",
          received_at: new Date().toISOString(),
        },
      }),
    });

    if (!response.ok) {
      const detail = await response.text();
      console.log("dispatch failed", response.status, detail.slice(0, 200));
      return new Response("dispatch failed", { status: 502 });
    }
    return new Response("dispatched", { status: 202 });
  },
};

function shouldDispatch(body, prefix) {
  if (body?.typeWebhook !== "incomingMessageReceived") return false;
  const data = body.messageData || {};
  const type = data.typeMessage || "";
  if (type === "imageMessage") return true;
  const text =
    data.textMessageData?.textMessage ||
    data.extendedTextMessageData?.text ||
    "";
  return text.trim().toLowerCase().startsWith(prefix.toLowerCase());
}

function chatIdOf(body) {
  return body?.senderData?.chatId;
}

function safeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
