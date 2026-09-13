# WhatsApp images → Gemini → WhatsApp reply

Images posted to a WhatsApp group are sent to the Gemini API with a prompt, and the answer
is posted back to the same group. Scheduled and executed entirely by GitHub Actions.

```
WhatsApp group ──▶ Green API queue ──▶ GitHub Actions (manual run) ──▶ Gemini
                                              │                        │
                                              └────── reply in group ◀─┘
```

Runs are **triggered on demand**, not on a schedule — see below for why.

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

With the credentials exported locally, list the groups the account belongs to:

```bash
python3 -m bot.cli list-chats --groups-only
python3 -m bot.cli list-chats --filter automation
```

Copy the `...@g.us` id of the target group. Prefer a group you created for this — the bot
replies to real people otherwise.

`doctor` also prints chat ids, but only for chats with messages waiting in the queue, and it
consumes that queue in `queue`/`both` read mode exactly as a real run does.

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
| `GEMINI_MODEL` | (`gemini-3.8-flash`) |
| `GEMINI_MODEL_FALLBACKS` | Tried in order on 503/429 (`gemini-2.5-flash`) |
| `PROMPT_PATH` | Which prompt file to use (`prompts/default.md`) |
| `GEMINI_TEMPERATURE` | (`0`) — deterministic, required for strict templates |
| `GEMINI_THINKING_BUDGET` | `0` off, `-1` model decides, or a token cap (`0`) |
| `GEMINI_MAX_OUTPUT_TOKENS` | (`32768`) — a cap, not a reservation |
| `MAX_REPLY_CHARS` | Characters per WhatsApp message (`4000`) |
| `MAX_REPLY_PARTS` | Maximum messages per answer (`12`) |
| `READ_MODE` | `queue` / `history` / `both` (`both`) |
| `INCLUDE_OUTGOING` | `true` to process images you post yourself from the linked phone (`false`) |
| `WINDOW_MINUTES` | Ignore images older than this (`1440`) |
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
exactly that. Multiple images in the same chat are answered in **one** Gemini call.

Long answers are **split across numbered WhatsApp messages** (`(1/8)`, `(2/8)`, …) rather
than truncated, because transcribing a few document pages easily produces 30,000 characters.
Only the first part quotes the original image, so the answer reads as one thread. Default
capacity is 12 messages of 4,000 characters; past that the last message says so explicitly.

Edit `prompts/default.md` to change the bot's behaviour and tone. No code change needed.

## Posting the images yourself

If the number linked to Green API is also the account that posts the images — a personal
automation group, for instance — those messages are **outgoing**, not incoming, and are
ignored by default. To handle that case:

1. In Green API, turn on **"Receive webhooks on messages sent from phone"**.
2. Set `INCLUDE_OUTGOING=true`.

This cannot cause a reply loop. Green API reports a phone-sent message as
`outgoingMessageReceived` and this bot's own replies as `outgoingAPIMessageReceived`; only
the former is ever read. In the history sweep, where the two are indistinguishable, safety
comes from the pipeline acting only on images while the bot only ever sends text.

## Triggering a run

There is deliberately **no `schedule`** on this workflow. GitHub's cron proved unusable here:
a `*/5` schedule produced two runs in six hours, and `7,37` was no better. Scheduled runs are
best-effort and get shed under load, so relying on them meant images sat unanswered for hours.

Trigger a run whichever way suits you:

```bash
scripts/trigger.sh            # dispatch, wait, print the run's counters
scripts/trigger.sh --dry-run  # match messages without calling Gemini or replying
scripts/trigger.sh --doctor   # connectivity checks only
```

Or press **Run workflow** on the Actions tab (also in the GitHub mobile app):
`Actions → whatsapp-gemini → Run workflow`, with optional `dry_run` and `doctor` checkboxes.

Or skip GitHub entirely and process locally, which is the fastest and most reliable path:

```bash
set -a && . ./.env && set +a && python3 -m bot.cli run
```

Nothing is lost between runs: the Green API queue holds messages until read, the history sweep
backfills, and `WINDOW_MINUTES=1440` still admits day-old images. Dedupe state means running
twice never double-replies.

### If you want automatic replies

Deploy `relay/cloudflare-worker.js` (free tier). A Green API webhook hits the worker, which
fires `repository_dispatch` — a trigger this workflow still listens for — and the reply lands
in 30–60s. That is event-driven and does not depend on GitHub's scheduler at all.

## Choosing a model

Model choice is measured, not assumed. `scripts/bench_models.py` runs candidates over real
images from a chat and scores template adherence — sections present, correct order, whether
missing fields are flagged, and markdown leakage:

```bash
python3 scripts/bench_models.py --chat 1203634...@g.us --prompt prompts/clinical-summary.md
```

On a three-page handwritten clinical form, `gemini-3.8-flash` beat `gemini-2.5-flash`: all
three flash models reproduced the template exactly, but 2.5-flash filed heart sounds under
CNS, left CVS empty, dropped a documented `PALLOR PRESENT`, and misread breath sounds.
`gemini-3.5-flash` was 3x slower and ignored the `[EMPTY]` rule. Pro models were not usable
on a free-tier key (429/404).

A consumer Gemini subscription does **not** grant API access or quota — AI Studio keys are
billed separately.

Re-run the benchmark on your own documents before trusting any model. And for anything
consequential, a person must check the output against the source images: these models still
garble words and can disagree on a number as important as a haemoglobin value.

## Strict output templates

`prompts/clinical-summary.md` shows the pattern for a fixed-format extraction: state the
template verbatim, forbid extra sections and markdown, require `[EMPTY]` for anything not
documented and `[ILLEGIBLE]` for unreadable values, and forbid inference. Pair it with
`GEMINI_TEMPERATURE=0` so the same images produce the same document.

Select it with the `PROMPT_PATH` variable rather than editing code.

## Thinking tokens will truncate your replies

Gemini 2.5-class models are *thinking* models, and reasoning tokens are charged against
`maxOutputTokens`. With the API default budget, a request can spend ~980 of 1024 tokens
thinking and emit a reply that stops mid-word, reported as `finishReason: MAX_TOKENS`.

This bot therefore sends `thinkingConfig.thinkingBudget: 0` by default and allows 32768
output tokens. That ceiling costs nothing when unused — billing follows tokens actually
generated — so it is set high deliberately. If you switch to a model that cannot disable thinking (`gemini-2.5-pro`), set
`GEMINI_THINKING_BUDGET=-1` and raise `GEMINI_MAX_OUTPUT_TOKENS` well above the answer you
want. A truncated response is logged as a warning and flagged on the result rather than
being sent silently.

## Cost

- **Actions:** free on public repositories. With no schedule, you only spend minutes on runs
  you ask for, so a private repo is viable too.
- **Gemini:** billed per request; images cost far more tokens than text. `command` mode is
  the cheapest because idle group chatter triggers nothing.
- **Green API:** free tier limits apply per their pricing.

## Local development

```bash
python3 -m unittest discover -s tests -v     # 125 tests, no network
python3 -m bot.cli doctor                    # credential and connectivity check
python3 -m bot.cli list-chats --groups-only  # find a group id without waiting for a message
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
  cli.py               run / doctor / list-chats / list-models / state-merge
  providers/
    base.py            InboundMessage + Provider interface
    greenapi.py        pull queue, history sweep, group sends
    meta.py            official Cloud API, 1:1 only, webhook-fed
.github/workflows/
  whatsapp-gemini.yml  manual + repository_dispatch (no cron: see Triggering a run)
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
| Model returns 503/429 | falls back to the next model in `GEMINI_MODEL_FALLBACKS` |
| Corrupt state file | starts empty; worst case is one duplicate reply |
| Two runs overlap | `concurrency` serialises them; state pushes union-merge on conflict |

A run exits 0 even with per-chat errors, so a transient outage does not turn the Actions
history red. Use `python3 -m bot.cli run --strict` to exit non-zero instead.
