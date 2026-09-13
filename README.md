# WhatsApp images → Gemini → WhatsApp reply

Images posted to a WhatsApp group are sent to the Gemini API with a prompt, and the answer
is posted back to the same group. Scheduled and executed entirely by GitHub Actions.

```
WhatsApp group ──▶ Green API queue ──▶ GitHub Actions (*/5 min) ──▶ Gemini
                                              │                      │
                                              └───── reply in group ◀┘
```

- **No server to run.** Green API buffers incoming messages in a pull queue, so an
  ephemeral Actions runner can read them. No webhook endpoint, no persistent socket.
- **No dependencies.** Standard library only, so the workflow has no `pip install` step
  and cannot break on a registry outage or a transitive CVE.
- **No duplicate replies.** Every answered message id is recorded on a dedicated
  `bot-state` branch and consulted on the next run.

## Why Green API and not the official WhatsApp API

The official WhatsApp Cloud API **cannot read or post in groups at all** — it is limited
to 1:1 conversations — and it has no read endpoint, only webhook push. Group automation
therefore requires a provider that wraps WhatsApp Web. Green API is used because it is the
only such provider that is free-tier *and* exposes a pull queue, which is what makes the
GitHub-Actions-only design possible.

**Read this before you point it at anything that matters:** Green API is an unofficial
gateway. Automating a personal WhatsApp account violates WhatsApp's Terms of Service and
numbers do get banned. Use a number you can afford to lose. The `meta` provider is included
as the compliant alternative, at the cost of the group requirement.

## Quick start

### 1. Green API instance

1. Create an account at green-api.com and create an instance.
2. Scan the QR code with the WhatsApp account that should act as the bot.
3. In the instance settings, under **Webhooks**:
   - turn **on** "Receive webhooks on incoming messages and files" (the API calls this
     `incomingWebhook`);
   - leave **Webhook Url** empty;
   - leave every other toggle off — "messages sent from phone" and "sent messages statuses"
     only fill the queue with events the bot discards;
   - click **Save Changes**.

   An empty Webhook Url plus that toggle is what makes messages accumulate in the pull
   queue. Setting a URL sends them there instead and the queue stays empty.
4. Note the `idInstance` and `apiTokenInstance`.

### 2. Gemini key

Create an API key at aistudio.google.com. Check which models it can use:

```bash
GEMINI_API_KEY=... python3 -m bot.cli list-models
```

### 3. Find the group id

With the credentials exported locally, post an image in the target group and run:

```bash
python3 -m bot.cli doctor
```

It prints every chat it saw, marking groups. Copy the `...@g.us` id.

> `doctor` consumes the queue in `queue`/`both` read mode, exactly as a real run does.

### 4. Configure the repository

Settings → Secrets and variables → Actions.

**Secrets**

| Secret | Value |
| --- | --- |
| `GREEN_API_INSTANCE_ID` | `idInstance` from Green API |
| `GREEN_API_TOKEN` | `apiTokenInstance` from Green API |
| `GEMINI_API_KEY` | Google AI Studio key |

**Variables** (all optional; defaults in parentheses)

| Variable | Meaning |
| --- | --- |
| `WHATSAPP_CHAT_IDS` | Comma-separated chats to serve. **Set this** — empty means every chat the account is in |
| `TRIGGER_MODE` | `any_image` / `command` / `command_or_caption` (`command_or_caption`) |
| `COMMAND_PREFIX` | Command that triggers a reply (`/ask`) |
| `GEMINI_MODEL` | (`gemini-2.5-flash`) |
| `READ_MODE` | `queue` / `history` / `both` (`both`) |
| `WINDOW_MINUTES` | Ignore images older than this (`60`) |
| `MAX_IMAGES_PER_REPLY` | Images batched into one Gemini call (`6`) |
| `MAX_REPLIES_PER_RUN` | Cap on replies per run (`5`) |
| `PROVIDER` | `greenapi` / `meta` (`greenapi`) |

### 5. Verify, then let the schedule take over

Actions → **whatsapp-gemini** → Run workflow:

- tick **doctor** to check credentials and print chat ids;
- tick **dry_run** to match messages without calling Gemini or replying;
- run with neither to go live.

The `*/5 * * * *` schedule then runs on its own.

## Trigger modes

| Mode | Fires when |
| --- | --- |
| `any_image` | every image, using `prompts/default.md`. Noisy in a busy group |
| `command` | only an image captioned `/ask ...`, or an image plus a nearby `/ask ...` message |
| `command_or_caption` | as `command`, plus any image that carries a caption, which becomes the question |

The caption after the prefix is appended to the prompt, so `/ask is this itemised?` asks
exactly that. Multiple images in the same chat are answered in **one** Gemini call and
**one** reply.

Edit `prompts/default.md` to change the bot's behaviour and tone. No code change needed.

## Timing, and what "every 5 minutes" really means

5 minutes is GitHub's cron minimum, but scheduled runs sit in a best-effort queue and
routinely fire 10–20+ minutes late when Actions is busy. Nothing is lost — the Green API
queue holds messages until read, and the history sweep backfills anything the queue missed.

For replies in ~30–60s instead, deploy `relay/cloudflare-worker.js` (free tier): it turns a
provider webhook into a `repository_dispatch`, which this workflow already listens for. A
relay is needed because Actions cannot receive inbound HTTP. If you enable a webhook in
Green API, set `READ_MODE=history` so the cron sweep still works — a configured webhook can
stop the pull queue from filling.

GitHub also disables cron on repositories with no commits for 60 days; `keepalive.yml`
commits a timestamp weekly to prevent that.

## Cost

- **Actions:** free on public repositories. On a private repo, `*/5` is ~8,600 runs/month
  at roughly a minute each — that exceeds the 2,000-minute free allowance, so either make
  the repo public or widen the cron.
- **Gemini:** billed per request; images cost far more tokens than text. `command` mode is
  the cheapest because idle group chatter triggers nothing.
- **Green API:** free tier limits apply per their pricing.

## Local development

```bash
python3 -m unittest discover -s tests -v     # 97 tests, no network
python3 -m bot.cli doctor                    # credential and connectivity check
python3 -m bot.cli run --dry-run             # read and match, call nothing
python3 -m bot.cli run                       # full cycle
python3 -m bot.cli list-models               # models available to your key
```

`doctor --raw` dumps the newest raw provider payload, which is the fastest way to confirm
Green API's field names against your instance if parsing ever looks wrong.

Environment for local runs:

```bash
export GREEN_API_INSTANCE_ID=... GREEN_API_TOKEN=... GEMINI_API_KEY=...
export WHATSAPP_CHAT_IDS='123456789-987654321@g.us'
export TRIGGER_MODE=command STATE_PATH=.state/state.json
```

## Layout

```
bot/
  config.py            env parsing, all validation errors reported at once
  httpx.py             stdlib HTTP with retry/backoff and token redaction in logs
  gemini.py            generateContent with inline base64 images
  state.py             dedupe records, atomic writes, pruning, union merge
  pipeline.py          read -> select -> batch -> Gemini -> reply -> record
  cli.py               run / doctor / list-models / state-merge
  providers/
    base.py            InboundMessage + Provider interface
    greenapi.py        pull queue, history sweep, group sends
    meta.py            official Cloud API, 1:1 only, webhook-fed
.github/workflows/
  whatsapp-gemini.yml  cron + repository_dispatch + manual
  keepalive.yml        weekly commit so cron stays enabled
  ci.yml               tests on 3.11 and 3.12
scripts/state_sync.sh  read/write state on the bot-state branch via git plumbing
relay/                 optional webhook -> repository_dispatch worker
prompts/default.md     the prompt sent with every image
```

## Failure behaviour

| Situation | Result |
| --- | --- |
| Gemini or send fails | message left unrecorded, retried next run |
| Safety block | recorded and answered once, never retried |
| One image of a batch fails to download | the rest are still analysed |
| Provider unreachable | run reports the error and exits 0 so the schedule survives |
| Corrupt state file | starts empty; worst case is one duplicate reply |
| Two runs overlap | `concurrency` serialises them; state pushes union-merge on conflict |

A run exits 0 even with per-chat errors, so a transient outage does not turn the Actions
history red. Use `python3 -m bot.cli run --strict` to exit non-zero instead.
