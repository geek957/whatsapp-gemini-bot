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

    def test_http_failure_wrapped_as_gemini_error(self):
        http = FakeHttp([HttpError(429, "gemini", b"rate limited")])
        with self.assertRaises(GeminiError) as ctx:
            analyze(make_config(), "x", [GeminiImage("image/png", b"1")], http=http)
        self.assertIn("429", str(ctx.exception))

    def test_multipart_text_is_concatenated(self):
        payload = {"candidates": [{"content": {"parts": [{"text": "a"}, {"text": "b"}]}, "finishReason": "STOP"}]}
        http = FakeHttp([json_response(payload)])
        result = analyze(make_config(), "x", [GeminiImage("image/png", b"1")], http=http)
        self.assertEqual(result.text, "ab")


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
