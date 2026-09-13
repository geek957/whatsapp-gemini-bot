import unittest

from bot.gemini import GeminiBlocked, GeminiError, GeminiResult
from bot.pipeline import run
from bot.state import State
from tests.helpers import FakeProvider, image_message, make_config, text_message

NOW = 1_700_000_400


def ok_analyzer(text="Looks like a receipt for 42.50."):
    def analyzer(cfg, prompt, images):
        analyzer.calls.append({"prompt": prompt, "images": images})
        return GeminiResult(text=text, model=cfg.gemini_model, prompt_tokens=100, output_tokens=10)
    analyzer.calls = []
    return analyzer


class TriggerTests(unittest.TestCase):
    def test_any_image_mode_answers_a_bare_image(self):
        provider = FakeProvider(inbound=[image_message("M1")])
        analyzer = ok_analyzer()
        report = run(make_config(trigger_mode="any_image"), provider, State(), now=NOW, analyzer=analyzer)

        self.assertEqual(report.replies, 1)
        self.assertEqual(len(provider.sent), 1)
        chat_id, text, reply_to = provider.sent[0]
        self.assertEqual(chat_id, "123-456@g.us")
        self.assertEqual(text, "Looks like a receipt for 42.50.")
        self.assertEqual(reply_to, "M1")

    def test_command_mode_ignores_an_uncaptioned_image(self):
        provider = FakeProvider(inbound=[image_message("M1")])
        report = run(make_config(trigger_mode="command"), provider, State(), now=NOW, analyzer=ok_analyzer())
        self.assertEqual(report.replies, 0)
        self.assertEqual(report.not_triggered, 1)
        self.assertEqual(provider.sent, [])

    def test_command_mode_fires_on_prefixed_caption_and_strips_the_prefix(self):
        provider = FakeProvider(inbound=[image_message("M1", caption="/ask how much is the total?")])
        analyzer = ok_analyzer()
        run(make_config(trigger_mode="command"), provider, State(), now=NOW, analyzer=analyzer)
        self.assertIn("how much is the total?", analyzer.calls[0]["prompt"])
        self.assertNotIn("/ask", analyzer.calls[0]["prompt"])

    def test_command_mode_fires_on_a_separate_command_message(self):
        provider = FakeProvider(inbound=[
            image_message("M1"),
            text_message("T1", "/ask compare these"),
        ])
        analyzer = ok_analyzer()
        report = run(make_config(trigger_mode="command"), provider, State(), now=NOW, analyzer=analyzer)
        self.assertEqual(report.replies, 1)
        self.assertIn("compare these", analyzer.calls[0]["prompt"])

    def test_command_prefix_matching_is_case_insensitive(self):
        provider = FakeProvider(inbound=[image_message("M1", caption="/ASK what is this")])
        report = run(make_config(trigger_mode="command"), provider, State(), now=NOW, analyzer=ok_analyzer())
        self.assertEqual(report.replies, 1)

    def test_command_or_caption_mode_accepts_any_caption(self):
        provider = FakeProvider(inbound=[image_message("M1", caption="whats the total?")])
        analyzer = ok_analyzer()
        report = run(make_config(trigger_mode="command_or_caption"), provider, State(), now=NOW, analyzer=analyzer)
        self.assertEqual(report.replies, 1)
        self.assertIn("whats the total?", analyzer.calls[0]["prompt"])

    def test_command_or_caption_mode_skips_a_bare_image(self):
        provider = FakeProvider(inbound=[image_message("M1")])
        report = run(make_config(trigger_mode="command_or_caption"), provider, State(), now=NOW, analyzer=ok_analyzer())
        self.assertEqual(report.replies, 0)

    def test_text_only_messages_never_produce_a_reply(self):
        provider = FakeProvider(inbound=[text_message("T1", "/ask anything")])
        report = run(make_config(trigger_mode="command"), provider, State(), now=NOW, analyzer=ok_analyzer())
        self.assertEqual(report.replies, 0)
        self.assertEqual(provider.sent, [])


class DedupeAndFilterTests(unittest.TestCase):
    def test_already_processed_images_are_skipped(self):
        state = State()
        state.mark("M1", NOW - 10)
        provider = FakeProvider(inbound=[image_message("M1")])
        report = run(make_config(trigger_mode="any_image"), provider, State.from_dict(state.to_dict()),
                     now=NOW, analyzer=ok_analyzer())
        self.assertEqual(report.already_processed, 1)
        self.assertEqual(provider.sent, [])

    def test_second_run_over_the_same_message_stays_silent(self):
        cfg = make_config(trigger_mode="any_image")
        state = State()
        provider = FakeProvider(inbound=[image_message("M1")])
        run(cfg, provider, state, now=NOW, analyzer=ok_analyzer())
        run(cfg, provider, state, now=NOW + 300, analyzer=ok_analyzer())
        self.assertEqual(len(provider.sent), 1)

    def test_chats_outside_the_allowlist_are_ignored(self):
        provider = FakeProvider(inbound=[image_message("M1", chat_id="999@g.us")])
        report = run(make_config(trigger_mode="any_image", chat_ids=("123-456@g.us",)),
                     provider, State(), now=NOW, analyzer=ok_analyzer())
        self.assertEqual(report.read, 1)
        self.assertEqual(report.replies, 0)
        self.assertEqual(provider.sent, [])

    def test_empty_allowlist_accepts_every_chat(self):
        provider = FakeProvider(inbound=[image_message("M1", chat_id="anything@g.us")])
        report = run(make_config(trigger_mode="any_image", chat_ids=()),
                     provider, State(), now=NOW, analyzer=ok_analyzer())
        self.assertEqual(report.replies, 1)

    def test_images_older_than_the_window_are_marked_not_answered(self):
        old = image_message("M1", timestamp=NOW - 7200)
        state = State()
        provider = FakeProvider(inbound=[old])
        report = run(make_config(trigger_mode="any_image", window_minutes=60),
                     provider, state, now=NOW, analyzer=ok_analyzer())
        self.assertEqual(report.replies, 0)
        self.assertTrue(state.seen("M1"), "stale rows must be recorded so sweeps stop reconsidering them")


class BatchingTests(unittest.TestCase):
    def test_images_from_one_chat_share_a_single_reply(self):
        provider = FakeProvider(inbound=[image_message("M1"), image_message("M2"), image_message("M3")])
        analyzer = ok_analyzer()
        report = run(make_config(trigger_mode="any_image"), provider, State(), now=NOW, analyzer=analyzer)
        self.assertEqual(len(analyzer.calls), 1)
        self.assertEqual(len(analyzer.calls[0]["images"]), 3)
        self.assertEqual(len(provider.sent), 1)
        self.assertEqual(report.outcomes[0].image_count, 3)

    def test_separate_chats_get_separate_replies(self):
        provider = FakeProvider(inbound=[
            image_message("M1", chat_id="a@g.us"),
            image_message("M2", chat_id="b@g.us"),
        ])
        report = run(make_config(trigger_mode="any_image", chat_ids=()),
                     provider, State(), now=NOW, analyzer=ok_analyzer())
        self.assertEqual(report.replies, 2)
        self.assertEqual({chat for chat, _, _ in provider.sent}, {"a@g.us", "b@g.us"})

    def test_batch_size_cap_defers_the_remainder(self):
        state = State()
        provider = FakeProvider(inbound=[image_message(f"M{i}") for i in range(5)])
        run(make_config(trigger_mode="any_image", max_images_per_reply=2),
            provider, state, now=NOW, analyzer=ok_analyzer())
        self.assertEqual(len(provider.sent), 1)
        self.assertTrue(state.seen("M0"))
        self.assertTrue(state.seen("M1"))
        self.assertFalse(state.seen("M2"), "deferred images must stay unmarked for the next run")

    def test_reply_cap_defers_extra_chats_and_reports_it(self):
        provider = FakeProvider(inbound=[image_message(f"M{i}", chat_id=f"c{i}@g.us") for i in range(4)])
        report = run(make_config(trigger_mode="any_image", chat_ids=(), max_replies_per_run=2),
                     provider, State(), now=NOW, analyzer=ok_analyzer())
        self.assertEqual(report.replies, 2)
        self.assertTrue(any("deferred" in error for error in report.errors))

    def test_multi_image_prompt_states_the_count(self):
        provider = FakeProvider(inbound=[image_message("M1"), image_message("M2")])
        analyzer = ok_analyzer()
        run(make_config(trigger_mode="any_image"), provider, State(), now=NOW, analyzer=analyzer)
        self.assertIn("2 images", analyzer.calls[0]["prompt"])


class FailureTests(unittest.TestCase):
    def test_gemini_failure_leaves_the_message_unmarked_for_retry(self):
        def failing(cfg, prompt, images):
            raise GeminiError("upstream 500")

        state = State()
        provider = FakeProvider(inbound=[image_message("M1")])
        report = run(make_config(trigger_mode="any_image"), provider, state, now=NOW, analyzer=failing)
        self.assertEqual(report.outcomes[0].status, "failed")
        self.assertFalse(state.seen("M1"))
        self.assertTrue(report.failed)

    def test_safety_block_is_recorded_and_answered_once(self):
        def blocked(cfg, prompt, images):
            raise GeminiBlocked("response blocked: SAFETY")

        state = State()
        provider = FakeProvider(inbound=[image_message("M1")])
        report = run(make_config(trigger_mode="any_image"), provider, state, now=NOW, analyzer=blocked)
        self.assertEqual(report.outcomes[0].status, "blocked")
        self.assertTrue(state.seen("M1"), "a safety block is final; do not retry every 5 minutes")
        self.assertEqual(len(provider.sent), 1)

    def test_send_failure_leaves_the_message_unmarked(self):
        state = State()
        provider = FakeProvider(inbound=[image_message("M1")], fail_send=True)
        report = run(make_config(trigger_mode="any_image"), provider, state, now=NOW, analyzer=ok_analyzer())
        self.assertEqual(report.outcomes[0].status, "failed")
        self.assertFalse(state.seen("M1"))

    def test_download_failure_of_one_image_still_answers_the_rest(self):
        provider = FakeProvider(inbound=[image_message("M1"), image_message("M2")], fail_download={"M1"})
        analyzer = ok_analyzer()
        report = run(make_config(trigger_mode="any_image"), provider, State(), now=NOW, analyzer=analyzer)
        self.assertEqual(report.replies, 1)
        self.assertEqual(len(analyzer.calls[0]["images"]), 1)

    def test_all_downloads_failing_reports_failure(self):
        provider = FakeProvider(inbound=[image_message("M1")], fail_download={"M1"})
        report = run(make_config(trigger_mode="any_image"), provider, State(), now=NOW, analyzer=ok_analyzer())
        self.assertEqual(report.outcomes[0].status, "failed")
        self.assertEqual(provider.sent, [])

    def test_provider_read_failure_reports_and_returns_early(self):
        provider = FakeProvider(fail_fetch=True)
        report = run(make_config(), provider, State(), now=NOW, analyzer=ok_analyzer())
        self.assertTrue(report.failed)
        self.assertIn("fetch_messages failed", report.errors[0])

    def test_long_replies_are_truncated_for_whatsapp(self):
        provider = FakeProvider(inbound=[image_message("M1")])
        run(make_config(trigger_mode="any_image"), provider, State(), now=NOW,
            analyzer=ok_analyzer(text="x" * 9000))
        _, text, _ = provider.sent[0]
        self.assertLessEqual(len(text), 4000)
        self.assertTrue(text.endswith("[truncated]"))


class DryRunTests(unittest.TestCase):
    def test_dry_run_calls_neither_gemini_nor_whatsapp(self):
        analyzer = ok_analyzer()
        state = State()
        provider = FakeProvider(inbound=[image_message("M1")])
        report = run(make_config(trigger_mode="any_image", dry_run=True), provider, state,
                     now=NOW, analyzer=analyzer)
        self.assertEqual(report.outcomes[0].status, "dry_run")
        self.assertEqual(analyzer.calls, [])
        self.assertEqual(provider.sent, [])
        self.assertTrue(state.seen("M1"))


class ReportTests(unittest.TestCase):
    def test_run_bookkeeping_and_pruning(self):
        state = State()
        state.mark("ancient", NOW - 90 * 86400)
        provider = FakeProvider(inbound=[image_message("M1")])
        report = run(make_config(trigger_mode="any_image", state_retention_days=14),
                     provider, state, now=NOW, analyzer=ok_analyzer())
        self.assertEqual(state.last_run_ts, NOW)
        self.assertEqual(state.runs, 1)
        self.assertEqual(report.pruned, 1)
        self.assertFalse(state.seen("ancient"))

    def test_summary_markdown_mentions_counts_and_chat(self):
        provider = FakeProvider(inbound=[image_message("M1")])
        report = run(make_config(trigger_mode="any_image"), provider, State(), now=NOW, analyzer=ok_analyzer())
        markdown = report.summary_markdown()
        self.assertIn("replies sent: **1**", markdown)
        self.assertIn("Group", markdown)

    def test_report_dict_is_json_serialisable(self):
        import json

        provider = FakeProvider(inbound=[image_message("M1")])
        report = run(make_config(trigger_mode="any_image"), provider, State(), now=NOW, analyzer=ok_analyzer())
        json.dumps(report.to_dict())


if __name__ == "__main__":
    unittest.main()
