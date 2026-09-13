import json
import tempfile
import unittest
from pathlib import Path

from bot.providers.meta import MetaCloudProvider
from tests.helpers import FakeHttp, json_response, make_config

WEBHOOK = {
    "entry": [{
        "changes": [{
            "value": {
                "contacts": [{"wa_id": "4915112345", "profile": {"name": "Ada"}}],
                "messages": [{
                    "id": "wamid.1",
                    "from": "4915112345",
                    "timestamp": "1700000100",
                    "type": "image",
                    "image": {"id": "media-9", "mime_type": "image/jpeg", "caption": "/ask total?"},
                }],
            }
        }]
    }]
}


def meta_config(**kw):
    return make_config(provider="meta", meta_phone_number_id="pn1", meta_access_token="tok", **kw)


class MetaReadTests(unittest.TestCase):
    def test_no_dispatch_payload_reads_nothing(self):
        provider = MetaCloudProvider(meta_config(), http=FakeHttp([]), event_path="")
        self.assertEqual(provider.fetch_messages(), [])

    def test_webhook_payload_from_repository_dispatch_is_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = Path(tmp) / "event.json"
            event.write_text(json.dumps({"client_payload": {"webhook": WEBHOOK}}), encoding="utf-8")
            provider = MetaCloudProvider(meta_config(), http=FakeHttp([]), event_path=str(event))
            messages = provider.fetch_messages()

        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertEqual(message.id, "wamid.1")
        self.assertEqual(message.kind, "image")
        self.assertEqual(message.chat_id, "4915112345")
        self.assertEqual(message.sender_name, "Ada")
        self.assertEqual(message.text, "/ask total?")
        self.assertEqual(message.download_url, "media-9")
        self.assertFalse(message.is_group, "the Cloud API never yields group chats")

    def test_unreadable_event_file_is_tolerated(self):
        provider = MetaCloudProvider(meta_config(), http=FakeHttp([]), event_path="/nonexistent/event.json")
        self.assertEqual(provider.fetch_messages(), [])


class MetaWriteTests(unittest.TestCase):
    def test_send_text_shape(self):
        http = FakeHttp([json_response({"messages": [{"id": "wamid.out"}]})])
        provider = MetaCloudProvider(meta_config(), http=http)
        reply_id = provider.send_text("4915112345", "hello", reply_to="wamid.1")

        self.assertEqual(reply_id, "wamid.out")
        call = http.calls[0]
        self.assertTrue(call["url"].endswith("/pn1/messages"))
        self.assertEqual(call["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(call["json"]["messaging_product"], "whatsapp")
        self.assertEqual(call["json"]["text"]["body"], "hello")
        self.assertEqual(call["json"]["context"]["message_id"], "wamid.1")

    def test_media_download_resolves_the_short_lived_url(self):
        http = FakeHttp([json_response({"url": "https://lookaside/x", "mime_type": "image/png"})])
        captured = {}

        def downloader(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers")
            return b"png", "application/octet-stream"

        from tests.helpers import image_message

        provider = MetaCloudProvider(meta_config(), http=http, downloader=downloader)
        message = image_message("wamid.1", url="media-9", mime="")
        data, mime = provider.download_image(message)

        self.assertEqual(data, b"png")
        self.assertEqual(mime, "image/png")
        self.assertEqual(captured["url"], "https://lookaside/x")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer tok")


if __name__ == "__main__":
    unittest.main()
