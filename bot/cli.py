"""Command-line entrypoints: run, doctor, list-models, state-merge."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import config as config_module
from .config import Config, ConfigError
from .gemini import list_models
from .httpx import HttpError
from .pipeline import run as run_pipeline
from .providers import GreenApiProvider, build_provider
from .state import State

LOG = logging.getLogger("bot")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _load_config(overrides: dict[str, str] | None = None) -> Config:
    env = dict(os.environ)
    if overrides:
        env.update(overrides)
    cfg = config_module.from_env(env)
    _setup_logging(cfg.log_level)
    cfg.raise_for_errors()
    return cfg


def _write_step_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(markdown)
    except OSError as exc:
        LOG.warning("cannot write step summary: %s", exc)


def cmd_run(args: argparse.Namespace) -> int:
    overrides = {"DRY_RUN": "true"} if args.dry_run else {}
    cfg = _load_config(overrides)
    provider = build_provider(cfg)
    state = State.load(cfg.state_path)

    report = run_pipeline(cfg, provider, state)
    state.save(cfg.state_path)

    _write_step_summary(report.summary_markdown())
    print(json.dumps(report.to_dict(), indent=2))

    if report.failed and args.strict:
        return 1
    if report.failed:
        LOG.warning("run completed with %d error(s); exiting 0 so the schedule keeps running",
                    len(report.errors))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Verify credentials and print the chat ids needed for WHATSAPP_CHAT_IDS."""
    cfg = _load_config()
    print(f"provider          : {cfg.provider}")
    print(f"gemini model      : {cfg.gemini_model}")
    print(f"trigger mode      : {cfg.trigger_mode} (prefix {cfg.command_prefix!r})")
    print(f"read mode         : {cfg.read_mode}")
    print(f"chat allowlist    : {', '.join(cfg.chat_ids) or '(any chat)'}")
    print(f"prompt file       : {cfg.prompt_path} ({'found' if cfg.prompt_path.is_file() else 'MISSING'})")
    print(f"state file        : {cfg.state_path} ({'found' if cfg.state_path.is_file() else 'new'})")

    ok = True
    provider = build_provider(cfg)

    if isinstance(provider, GreenApiProvider):
        try:
            state_info = provider.state_instance()
            status = state_info.get("stateInstance", "unknown")
            print(f"instance state    : {status}")
            if status != "authorized":
                ok = False
                print("  -> link the number by scanning the QR in the Green API console", file=sys.stderr)
        except HttpError as exc:
            ok = False
            print(f"instance state    : FAILED ({exc})", file=sys.stderr)
        try:
            settings = provider.settings()
            hook = settings.get("webhookUrl") or ""
            incoming = settings.get("incomingWebhook", "")
            print(f"webhookUrl        : {hook or '(empty - queue mode, correct for Actions)'}")
            print(f"incomingWebhook   : {incoming}")
            if hook:
                print("  -> a webhookUrl is set; notifications may go there instead of the pull queue",
                      file=sys.stderr)
            if str(incoming).lower() not in {"yes", "true", "1"}:
                ok = False
                print("  -> in the console, turn on Webhooks > 'Receive webhooks on incoming "
                      "messages and files', or the queue stays empty", file=sys.stderr)
        except HttpError as exc:
            print(f"settings          : FAILED ({exc})", file=sys.stderr)

    try:
        messages = provider.fetch_messages()
    except HttpError as exc:
        print(f"fetch_messages    : FAILED ({exc})", file=sys.stderr)
        return 1

    print(f"\nread {len(messages)} message(s)")
    chats: dict[str, str] = {}
    for message in messages:
        chats.setdefault(message.chat_id, message.chat_name or "")
        line = f"  [{message.kind:5}] {message.chat_id:34} {message.sender_name or message.sender:20} {message.text[:48]!r}"
        print(line)
    if chats:
        print("\nchat ids seen (copy the group into WHATSAPP_CHAT_IDS):")
        for chat_id, name in chats.items():
            marker = "group" if chat_id.endswith("@g.us") else "direct"
            print(f"  {chat_id}   ({marker}) {name}")
    else:
        print("\nNo messages in the queue. Post an image in the group, then run doctor again.")
        print("Note: doctor consumes the queue in queue/both read modes.")

    if args.raw and messages:
        print("\nraw payload of newest message:")
        print(json.dumps(messages[-1].raw, indent=2, ensure_ascii=False)[:4000])

    return 0 if ok else 1


def cmd_list_models(args: argparse.Namespace) -> int:
    cfg = _load_config()
    names = list_models(cfg)
    if not names:
        print("no models returned; check GEMINI_API_KEY", file=sys.stderr)
        return 1
    for name in names:
        marker = " <- configured" if name == cfg.gemini_model else ""
        print(f"{name}{marker}")
    if cfg.gemini_model not in names:
        print(f"\nWARNING: GEMINI_MODEL={cfg.gemini_model} is not available on this key", file=sys.stderr)
        return 1
    return 0


def cmd_state_merge(args: argparse.Namespace) -> int:
    """Union two state files. Used by the workflow when a push races another run."""
    mine = State.load(Path(args.ours))
    theirs = State.load(Path(args.theirs))
    merged = mine.merge(theirs)
    merged.save(Path(args.out))
    print(f"merged {len(mine.processed)} + {len(theirs.processed)} -> {len(merged.processed)} ids")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bot", description="WhatsApp images -> Gemini -> WhatsApp reply")
    subs = parser.add_subparsers(dest="command", required=True)

    run_cmd = subs.add_parser("run", help="process new images and reply")
    run_cmd.add_argument("--dry-run", action="store_true", help="skip Gemini and WhatsApp writes")
    run_cmd.add_argument("--strict", action="store_true", help="exit non-zero when any chat failed")
    run_cmd.set_defaults(func=cmd_run)

    doctor = subs.add_parser("doctor", help="check credentials and list chat ids")
    doctor.add_argument("--raw", action="store_true", help="dump the newest raw provider payload")
    doctor.set_defaults(func=cmd_doctor)

    models = subs.add_parser("list-models", help="list Gemini models available to the key")
    models.set_defaults(func=cmd_list_models)

    merge = subs.add_parser("state-merge", help="union two state files")
    merge.add_argument("--ours", required=True)
    merge.add_argument("--theirs", required=True)
    merge.add_argument("--out", required=True)
    merge.set_defaults(func=cmd_state_merge)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (HttpError, FileNotFoundError, ValueError) as exc:
        logging.getLogger("bot").error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
