import base64
import unittest

from bot.gemini import GeminiBlocked, GeminiError, GeminiImage, analyze, list_models
from bot.httpx import HttpError
from tests.helpers import FakeHttp, json_response, make_config

OK_PAYLOAD = {
    "candidates": [{
        "content": {"parts": [{"text": "A receipt for 42.50 EUR."}]},
        "finishReason": "STOP",
    }],
    "usageMetadata": {"promptTokenCount": 310, "candidatesTokenCount": 12},
}


class AnalyzeTests(unittest.TestCase):
    def test_happy_path_returns_text_and_usage(self):
        http = FakeHttp([json_response(OK_PAYLOAD)])
        result = analyze(make_config(), "describe", [GeminiImage("image/jpeg", b"rawbytes")], http=http)
        self.assertEqual(result.text, "A receipt for 42.50 EUR.")
        self.assertEqual(result.prompt_tokens, 310)
        self.assertEqual(result.output_tokens, 12)
        self.assertEqual(result.finish_reason, "STOP")

    def test_request_shape_and_auth_header(self):
        http = FakeHttp([json_response(OK_PAYLOAD)])
        cfg = make_config(gemini_model="gemini-2.5-flash", gemini_api_key="secret-key")
        analyze(cfg, "describe", [GeminiImage("image/jpeg", b"AB")], http=http)

        call = http.calls[0]
        self.assertTrue(call["url"].endswith("/models/gemini-2.5-flash:generateContent"))
        self.assertEqual(call["headers"]["x-goog-api-key"], "secret-key")
        parts = call["json"]["contents"][0]["parts"]
        self.assertEqual(parts[0]["text"], "describe")
        self.assertEqual(parts[1]["inline_data"]["mime_type"], "image/jpeg")
        self.assertEqual(parts[1]["inline_data"]["data"], base64.b64encode(b"AB").decode())

    def test_multiple_images_are_sent_in_one_call(self):
        http = FakeHttp([json_response(OK_PAYLOAD)])
        images = [GeminiImage("image/jpeg", b"1"), GeminiImage("image/png", b"2")]
        analyze(make_config(), "compare", images, http=http)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(len(http.calls[0]["json"]["contents"][0]["parts"]), 3)

    def test_jpg_alias_normalised(self):
        http = FakeHttp([json_response(OK_PAYLOAD)])
        analyze(make_config(), "x", [GeminiImage("image/jpg", b"1")], http=http)
        mime = http.calls[0]["json"]["contents"][0]["parts"][1]["inline_data"]["mime_type"]
        self.assertEqual(mime, "image/jpeg")

    def test_charset_suffix_stripped(self):
        http = FakeHttp([json_response(OK_PAYLOAD)])
        analyze(make_config(), "x", [GeminiImage("image/png; charset=binary", b"1")], http=http)
        mime = http.calls[0]["json"]["contents"][0]["parts"][1]["inline_data"]["mime_type"]
        self.assertEqual(mime, "image/png")

    def test_unsupported_types_dropped_and_error_when_none_remain(self):
        http = FakeHttp([json_response(OK_PAYLOAD)])
        with self.assertRaises(GeminiError):
            analyze(make_config(), "x", [GeminiImage("application/pdf", b"1")], http=http)
        self.assertEqual(http.calls, [])

    def test_empty_prompt_rejected(self):
        with self.assertRaises(GeminiError):
            analyze(make_config(), "   ", [GeminiImage("image/png", b"1")], http=FakeHttp([]))

    def test_prompt_block_raises_blocked(self):
        http = FakeHttp([json_response({"promptFeedback": {"blockReason": "SAFETY"}})])
        with self.assertRaises(GeminiBlocked):
            analyze(make_config(), "x", [GeminiImage("image/png", b"1")], http=http)

    def test_safety_finish_reason_raises_blocked(self):
        payload = {"candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}]}
        http = FakeHttp([json_response(payload)])
        with self.assertRaises(GeminiBlocked):
            analyze(make_config(), "x", [GeminiImage("image/png", b"1")], http=http)

    def test_max_tokens_without_text_is_actionable(self):
        payload = {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}]}
        http = FakeHttp([json_response(payload)])
        with self.assertRaises(GeminiError) as ctx:
            analyze(make_config(), "x", [GeminiImage("image/png", b"1")], http=http)
        self.assertIn("GEMINI_MAX_OUTPUT_TOKENS", str(ctx.exception))

    def test_no_candidates_raises(self):
        http = FakeHttp([json_response({})])
        with self.assertRaises(GeminiError):
            analyze(make_config(), "x", [GeminiImage("image/png", b"1")], http=http)

    def test_non_transient_http_failure_raises_without_fallback(self):
        http = FakeHttp([HttpError(400, "gemini", b"bad request")])
        cfg = make_config(gemini_model_fallbacks=("gemini-2.5-flash",))
        with self.assertRaises(GeminiError) as ctx:
            analyze(cfg, "x", [GeminiImage("image/png", b"1")], http=http)
        self.assertIn("400", str(ctx.exception))
        self.assertEqual(len(http.calls), 1, "a bad request must not be retried on another model")

    def test_multipart_text_is_concatenated(self):
        payload = {"candidates": [{"content": {"parts": [{"text": "a"}, {"text": "b"}]}, "finishReason": "STOP"}]}
        http = FakeHttp([json_response(payload)])
        result = analyze(make_config(), "x", [GeminiImage("image/png", b"1")], http=http)
        self.assertEqual(result.text, "ab")


class ThinkingBudgetTests(unittest.TestCase):
    """A 2.5-class model spends maxOutputTokens on reasoning unless told not to."""

    def test_thinking_disabled_by_default(self):
        http = FakeHttp([json_response(OK_PAYLOAD)])
        analyze(make_config(), "x", [GeminiImage("image/png", b"1")], http=http)
        cfg_sent = http.calls[0]["json"]["generationConfig"]
        self.assertEqual(cfg_sent["thinkingConfig"], {"thinkingBudget": 0})
        self.assertEqual(cfg_sent["maxOutputTokens"], 32768)

    def test_positive_budget_passed_through(self):
        http = FakeHttp([json_response(OK_PAYLOAD)])
        analyze(make_config(gemini_thinking_budget=512), "x", [GeminiImage("image/png", b"1")], http=http)
        self.assertEqual(http.calls[0]["json"]["generationConfig"]["thinkingConfig"], {"thinkingBudget": 512})

    def test_negative_budget_omits_config_so_the_model_decides(self):
        http = FakeHttp([json_response(OK_PAYLOAD)])
        analyze(make_config(gemini_thinking_budget=-1), "x", [GeminiImage("image/png", b"1")], http=http)
        self.assertNotIn("thinkingConfig", http.calls[0]["json"]["generationConfig"])

    def test_truncated_response_is_flagged_not_silently_sent(self):
        payload = {
            "candidates": [{"content": {"parts": [{"text": "half an ans"}]}, "finishReason": "MAX_TOKENS"}],
            "usageMetadata": {"promptTokenCount": 272, "candidatesTokenCount": 41, "thoughtsTokenCount": 980},
        }
        http = FakeHttp([json_response(payload)])
        result = analyze(make_config(), "x", [GeminiImage("image/png", b"1")], http=http)
        self.assertTrue(result.truncated)
        self.assertEqual(result.thinking_tokens, 980)
        self.assertEqual(result.text, "half an ans")

    def test_complete_response_is_not_flagged(self):
        http = FakeHttp([json_response(OK_PAYLOAD)])
        result = analyze(make_config(), "x", [GeminiImage("image/png", b"1")], http=http)
        self.assertFalse(result.truncated)


class ModelFallbackTests(unittest.TestCase):
    """The newest models return 503 under load; that must not drop the message."""

    def test_503_falls_back_to_the_next_model(self):
        http = FakeHttp([HttpError(503, "gemini", b"high demand"), json_response(OK_PAYLOAD)])
        cfg = make_config(gemini_model="gemini-3.8-flash", gemini_model_fallbacks=("gemini-2.5-flash",))
        result = analyze(cfg, "x", [GeminiImage("image/png", b"1")], http=http)
        self.assertEqual(result.model, "gemini-2.5-flash")
        self.assertIn("gemini-3.8-flash", http.calls[0]["url"])
        self.assertIn("gemini-2.5-flash", http.calls[1]["url"])

    def test_429_also_falls_back(self):
        http = FakeHttp([HttpError(429, "gemini", b"RESOURCE_EXHAUSTED"), json_response(OK_PAYLOAD)])
        cfg = make_config(gemini_model_fallbacks=("gemini-2.5-flash",))
        self.assertEqual(analyze(cfg, "x", [GeminiImage("image/png", b"1")], http=http).model,
                         "gemini-2.5-flash")

    def test_exhausting_every_model_raises(self):
        http = FakeHttp([HttpError(503, "a", b"high demand"), HttpError(503, "b", b"high demand")])
        cfg = make_config(gemini_model_fallbacks=("gemini-2.5-flash",))
        with self.assertRaises(GeminiError):
            analyze(cfg, "x", [GeminiImage("image/png", b"1")], http=http)
        self.assertEqual(len(http.calls), 2)

    def test_safety_block_never_falls_back(self):
        http = FakeHttp([json_response({"promptFeedback": {"blockReason": "SAFETY"}}), json_response(OK_PAYLOAD)])
        cfg = make_config(gemini_model_fallbacks=("gemini-2.5-flash",))
        with self.assertRaises(GeminiBlocked):
            analyze(cfg, "x", [GeminiImage("image/png", b"1")], http=http)
        self.assertEqual(len(http.calls), 1, "another model would block the same content")

    def test_primary_success_never_calls_a_fallback(self):
        http = FakeHttp([json_response(OK_PAYLOAD)])
        cfg = make_config(gemini_model_fallbacks=("gemini-2.5-flash",))
        analyze(cfg, "x", [GeminiImage("image/png", b"1")], http=http)
        self.assertEqual(len(http.calls), 1)


class ListModelsTests(unittest.TestCase):
    def test_only_generatecontent_models_returned_sorted(self):
        payload = {"models": [
            {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
            {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]},
        ]}
        http = FakeHttp([json_response(payload)])
        self.assertEqual(list_models(make_config(), http=http), ["gemini-2.5-flash", "gemini-2.5-pro"])


if __name__ == "__main__":
    unittest.main()
