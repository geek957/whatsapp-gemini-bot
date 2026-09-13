/**
 * Shareable trigger UI + optional webhook relay, in one Cloudflare Worker.
 *
 * Why a Worker: triggering a workflow needs a GitHub token with Actions write access, and a
 * static page cannot hold one without publishing it. The token lives here as a Worker secret,
 * so the people you share the link with need no GitHub account, no token, and no repo access —
 * only the URL and a passcode.
 *
 * Routes
 *   GET  /            the page: one button, live status
 *   POST /api/run     dispatch the workflow (body: {passcode, mode})
 *   GET  /api/runs    recent run status for the page to poll
 *   POST /webhook     optional: Green API webhook -> repository_dispatch for instant replies
 *
 * Deploy (free tier is enough):
 *   npx wrangler deploy relay/cloudflare-worker.js --name whatsapp-gemini
 *   npx wrangler secret put GITHUB_TOKEN    # fine-grained PAT, Actions: read+write on the repo
 *   npx wrangler secret put GITHUB_REPO     # geek957/whatsapp-gemini-bot
 *   npx wrangler secret put RUN_PASSCODE    # what you share with the other person
 *   npx wrangler secret put RELAY_SECRET    # only if you use /webhook
 *
 * Anyone holding the URL and passcode can start runs, which spend Gemini quota. Treat the
 * passcode as a password, and rotate it with `wrangler secret put RUN_PASSCODE`.
 */

const WORKFLOW = "whatsapp-gemini.yml";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/") {
      return html(PAGE);
    }
    if (request.method === "POST" && url.pathname === "/api/run") {
      return handleRun(request, env);
    }
    if (request.method === "GET" && url.pathname === "/api/runs") {
      return handleRuns(request, env);
    }
    if (request.method === "POST" && url.pathname === "/webhook") {
      return handleWebhook(request, env, url);
    }
    return new Response("not found", { status: 404 });
  },
};

// ---------------------------------------------------------------- trigger API

async function handleRun(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "bad request" }, 400);
  }

  if (!env.RUN_PASSCODE || !safeEqual(String(body.passcode || ""), env.RUN_PASSCODE)) {
    // Deliberately vague, and slowed a little to make guessing tedious.
    await new Promise((resolve) => setTimeout(resolve, 700));
    return json({ error: "Wrong passcode." }, 403);
  }

  const inputs = {};
  if (body.mode === "dry") inputs.dry_run = true;
  if (body.mode === "doctor") inputs.doctor = true;

  const before = await listRuns(env);
  const previousId = before[0]?.id ?? 0;

  const dispatched = await gh(env, `/actions/workflows/${WORKFLOW}/dispatches`, {
    method: "POST",
    body: JSON.stringify({ ref: "main", inputs }),
  });
  if (!dispatched.ok) {
    return json({ error: `GitHub refused the request (${dispatched.status}).` }, 502);
  }
  return json({ ok: true, previousId });
}

async function handleRuns(request, env) {
  const passcode = new URL(request.url).searchParams.get("passcode") || "";
  if (!env.RUN_PASSCODE || !safeEqual(passcode, env.RUN_PASSCODE)) {
    return json({ error: "forbidden" }, 403);
  }
  const runs = await listRuns(env);
  return json({
    runs: runs.slice(0, 5).map((run) => ({
      id: run.id,
      number: run.run_number,
      status: run.status,
      conclusion: run.conclusion,
      event: run.event,
      created_at: run.created_at,
      url: run.html_url,
    })),
  });
}

async function listRuns(env) {
  const response = await gh(env, `/actions/workflows/${WORKFLOW}/runs?per_page=5`);
  if (!response.ok) return [];
  const data = await response.json();
  return data.workflow_runs || [];
}

function gh(env, path, options = {}) {
  return fetch(`https://api.github.com/repos/${env.GITHUB_REPO}${path}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "Content-Type": "application/json",
      "User-Agent": "whatsapp-gemini-trigger",
    },
  });
}

// -------------------------------------------------------------- webhook relay

async function handleWebhook(request, env, url) {
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
  if (!shouldDispatch(body, env.COMMAND_PREFIX || "/ask")) {
    return new Response("ignored", { status: 200 });
  }
  const response = await gh(env, "/dispatches", {
    method: "POST",
    body: JSON.stringify({
      event_type: "whatsapp-message",
      // The bot re-reads from Green API, so no message content is forwarded into GitHub's
      // event log; only a hint that something arrived.
      client_payload: { chat_id: body?.senderData?.chatId || "", received_at: new Date().toISOString() },
    }),
  });
  return new Response(response.ok ? "dispatched" : "dispatch failed", { status: response.ok ? 202 : 502 });
}

function shouldDispatch(body, prefix) {
  const hook = body?.typeWebhook;
  if (hook !== "incomingMessageReceived" && hook !== "outgoingMessageReceived") return false;
  const data = body.messageData || {};
  if (data.typeMessage === "imageMessage") return true;
  const text = data.textMessageData?.textMessage || data.extendedTextMessageData?.text || "";
  return text.trim().toLowerCase().startsWith(prefix.toLowerCase());
}

// -------------------------------------------------------------------- helpers

function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

function html(payload) {
  return new Response(payload, {
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
  });
}

function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let index = 0; index < a.length; index++) diff |= a.charCodeAt(index) ^ b.charCodeAt(index);
  return diff === 0;
}

const PAGE = `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light"><title>Summarise WhatsApp images</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--text:#e6edf3;--muted:#8b949e;--go:#238636;--go2:#2ea043;--bad:#da3633;--warn:#9e6a03}
@media(prefers-color-scheme:light){:root{--bg:#fff;--panel:#f6f8fa;--line:#d0d7de;--text:#1f2328;--muted:#59636e}}
*{box-sizing:border-box}
body{margin:0;padding:28px 16px;font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;background:var(--bg);color:var(--text);display:flex;justify-content:center}
main{width:100%;max-width:440px}
h1{font-size:21px;margin:0 0 6px}.sub{color:var(--muted);font-size:14px;margin-bottom:22px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:14px}
label{display:block;font-size:13px;color:var(--muted);margin-bottom:6px}
input{width:100%;padding:12px;font-size:16px;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:8px}
button{font:inherit;font-weight:600;border-radius:8px;border:1px solid var(--line);background:var(--panel);color:var(--text);cursor:pointer;padding:11px 14px}
button:disabled{opacity:.5;cursor:not-allowed}
#go{width:100%;padding:17px;font-size:17px;background:var(--go);border-color:var(--go2);color:#fff;margin-top:12px}
#go:hover:not(:disabled){background:var(--go2)}
.row{display:flex;gap:8px;margin-top:10px}.row button{flex:1;font-size:14px;font-weight:500}
.status{margin-top:16px;font-size:15px;min-height:24px}
.status a{color:#58a6ff}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:8px;vertical-align:1px}
.spin{background:var(--warn);animation:p 1.1s ease-in-out infinite}@keyframes p{0%,100%{opacity:.35}50%{opacity:1}}
.ok{background:var(--go2)}.bad{background:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:13px}th{text-align:left;color:var(--muted);font-weight:500;padding:3px 0}
td{padding:6px 0;border-top:1px solid var(--line)}
.note{font-size:12.5px;color:var(--muted);margin-top:14px}
</style></head><body><main>
<h1>Summarise WhatsApp images</h1>
<div class="sub">Reads new images from the group, summarises them, and replies in the chat.</div>

<div class="card">
  <label for="pc">Passcode</label>
  <input id="pc" type="password" inputmode="text" autocomplete="current-password" placeholder="••••••••">
  <button id="go" type="button">Run now</button>
  <div class="row">
    <button id="dry" type="button">Test only</button>
    <button id="doc" type="button">Check connection</button>
  </div>
  <div class="status" id="st"></div>
  <div class="note">A run takes about a minute. Images already answered are skipped, so running twice is safe.</div>
</div>

<div class="card">
  <table><thead><tr><th>Recent</th><th>Result</th><th>When</th></tr></thead>
  <tbody id="rt"><tr><td colspan="3" style="border:0;color:var(--muted)">—</td></tr></tbody></table>
</div>

<script>
const el=i=>document.getElementById(i), pc=()=>el('pc').value.trim();
const KEY='wa-passcode';
if(localStorage.getItem(KEY)) el('pc').value=localStorage.getItem(KEY);
function st(msg,kind){el('st').innerHTML=(kind?'<span class="dot '+kind+'"></span>':'')+msg}
function busy(b){['go','dry','doc'].forEach(i=>el(i).disabled=b)}
function ago(iso){const m=Math.round((Date.now()-new Date(iso))/6e4);
  if(m<1)return'just now';if(m<60)return m+'m ago';const h=Math.floor(m/60);
  return h<24?h+'h ago':Math.floor(h/24)+'d ago'}
async function runs(){
  const r=await fetch('/api/runs?passcode='+encodeURIComponent(pc()));
  if(!r.ok)return;const d=await r.json();
  el('rt').innerHTML=(d.runs||[]).map(x=>{
    const res=x.status!=='completed'?'<span class="dot spin"></span>running'
      :x.conclusion==='success'?'<span class="dot ok"></span>done'
      :'<span class="dot bad"></span>'+x.conclusion;
    return '<tr><td>#'+x.number+'</td><td>'+res+'</td><td>'+ago(x.created_at)+'</td></tr>';
  }).join('')||'<tr><td colspan="3" style="border:0;color:var(--muted)">No runs yet</td></tr>';
}
async function go(mode,label){
  if(!pc()){st('Enter the passcode.','bad');el('pc').focus();return}
  localStorage.setItem(KEY,pc());busy(true);st('Starting '+label+'…','spin');
  try{
    const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({passcode:pc(),mode})});
    const d=await r.json();
    if(!r.ok){st(d.error||'Could not start.','bad');return}
    st('Started. Waiting for the result…','spin');
    for(let i=0;i<50;i++){
      await new Promise(s=>setTimeout(s,4000));
      const q=await fetch('/api/runs?passcode='+encodeURIComponent(pc()));
      if(!q.ok)continue;
      const list=(await q.json()).runs||[];
      const cur=list.find(x=>x.id!==d.previousId&&x.event==='workflow_dispatch');
      await runs();
      if(!cur)continue;
      if(cur.status==='completed'){
        st(cur.conclusion==='success'
          ?'Finished. Check WhatsApp for the reply.'
          :'The run failed ('+cur.conclusion+'). Tell the owner.',
          cur.conclusion==='success'?'ok':'bad');
        return;
      }
      st('Running…','spin');
    }
    st('Still running. Check WhatsApp shortly.','warn');
  }catch(e){st('Network problem: '+e.message,'bad')}
  finally{busy(false)}
}
el('go').onclick=()=>go('run','the run');
el('dry').onclick=()=>go('dry','a test');
el('doc').onclick=()=>go('doctor','the check');
if(pc())runs();
</script></main></body></html>`;
