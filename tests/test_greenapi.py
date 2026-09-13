import unittest

from bot.httpx import HttpError
from bot.providers.greenapi import GreenApiProvider
from tests.helpers import FakeHttp, json_response, make_config, null_response

IMAGE_NOTIFICATION = {
    "receiptId": 7,
    "body": {
        "typeWebhook": "incomingMessageReceived",
        "idMessage": "MSG1",
        "timestamp": 1_700_000_100,
        "senderData": {
            "chatId": "123-456@g.us",
            "chatName": "Photo Group",
            "sender": "7900@c.us",
            "senderName": "Bob",
        },
        "messageData": {
            "typeMessage": "imageMessage",
            "fileMessageData": {
                "downloadUrl": "https://media.green-api.com/x.jpg",
                "caption": "/ask what is this",
                "mimeType": "image/jpeg",
                "fileName": "x.jpg",
            },
        },
    },
}

TEXT_NOTIFICATION = {
    "receiptId": 8,
    "body": {
        "typeWebhook": "incomingMessageReceived",
        "idMessage": "MSG2",
        "timestamp": 1_700_000_200,
        "senderData": {"chatId": "123-456@g.us", "chatName": "Photo Group", "sender": "7901@c.us"},
        "messageData": {"typeMessage": "textMessage", "textMessageData": {"textMessage": "/ask summarise"}},
    },
}


class GreenApiReadTests(unittest.TestCase):
    def test_queue_drain_parses_image_and_deletes_notification(self):
        http = FakeHttp([
            json_response(IMAGE_NOTIFICATION),
            json_response({"result": True}),  # deleteNotification
            null_response(),                  # queue now empty
        ])
        provider = GreenApiProvider(make_config(), http=http)

        messages = provider.fetch_messages()

        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertEqual(message.id, "MSG1")
        self.assertEqual(message.kind, "image")
        self.assertEqual(message.chat_id, "123-456@g.us")
        self.assertEqual(message.text, "/ask what is this")
        self.assertEqual(message.mime_type, "image/jpeg")
        self.assertTrue(message.is_group)

        deletes = [call for call in http.calls if call["method"] == "DELETE"]
        self.assertEqual(len(deletes), 1)
        self.assertIn("deleteNotification", deletes[0]["url"])

    def test_delete_uses_receipt_id_in_path(self):
        http = FakeHttp([json_response(IMAGE_NOTIFICATION), json_response({}), null_response()])
        GreenApiProvider(make_config(), http=http).fetch_messages()
        delete_call = [c for c in http.calls if c["method"] == "DELETE"][0]
        self.assertTrue(delete_call["url"].endswith("/7"))

    def test_text_notification_becomes_text_message(self):
        http = FakeHttp([json_response(TEXT_NOTIFICATION), json_response({}), null_response()])
        messages = GreenApiProvider(make_config(), http=http).fetch_messages()
        self.assertEqual(messages[0].kind, "text")
        self.assertEqual(messages[0].text, "/ask summarise")

    def test_empty_queue_returns_nothing(self):
        http = FakeHttp([null_response()])
        self.assertEqual(GreenApiProvider(make_config(), http=http).fetch_messages(), [])

    def test_non_message_webhooks_are_ignored(self):
        status_hook = {"receiptId": 3, "body": {"typeWebhook": "outgoingMessageStatus", "idMessage": "X"}}
        http = FakeHttp([json_response(status_hook), json_response({}), null_response()])
        self.assertEqual(GreenApiProvider(make_config(), http=http).fetch_messages(), [])

    def test_queue_error_is_swallowed_so_the_run_continues(self):
        http = FakeHttp([HttpError(503, "receiveNotification", b"down")])
        self.assertEqual(GreenApiProvider(make_config(), http=http).fetch_messages(), [])

    def test_non_image_mime_reported_as_image_is_reclassified(self):
        payload = {
            "receiptId": 9,
            "body": {
                **IMAGE_NOTIFICATION["body"],
                "idMessage": "MSG3",
                "messageData": {
                    "typeMessage": "imageMessage",
                    "fileMessageData": {"downloadUrl": "u", "mimeType": "application/pdf"},
                },
            },
        }
        http = FakeHttp([json_response(payload), json_response({}), null_response()])
        messages = GreenApiProvider(make_config(), http=http).fetch_messages()
        self.assertEqual(messages[0].kind, "other")

    def test_history_sweep_parses_incoming_rows_only(self):
        rows = [
            {"type": "outgoing", "idMessage": "OUT1", "typeMessage": "textMessage", "textMessage": "hi"},
            {
                "type": "incoming", "idMessage": "HIST1", "typeMessage": "imageMessage",
                "timestamp": 1_700_000_300, "downloadUrl": "https://m/y.jpg",
                "caption": "receipt", "mimeType": "image/png", "chatId": "123-456@g.us",
            },
        ]
        http = FakeHttp([json_response(rows)])
        cfg = make_config(read_mode="history")
        messages = GreenApiProvider(cfg, http=http).fetch_messages()
        self.assertEqual([m.id for m in messages], ["HIST1"])
        self.assertEqual(messages[0].mime_type, "image/png")
        self.assertEqual(messages[0].text, "receipt")

    def test_history_sweep_skipped_without_chat_allowlist(self):
        http = FakeHttp([])
        cfg = make_config(read_mode="history", chat_ids=())
        self.assertEqual(GreenApiProvider(cfg, http=http).fetch_messages(), [])
        self.assertEqual(http.calls, [])

    def test_both_modes_dedupe_overlapping_ids_and_sort_by_time(self):
        history_row = {
            "type": "incoming", "idMessage": "MSG1", "typeMessage": "imageMessage",
            "timestamp": 1_700_000_100, "downloadUrl": "u", "mimeType": "image/jpeg",
            "chatId": "123-456@g.us",
        }
        older_row = {
            "type": "incoming", "idMessage": "OLD", "typeMessage": "imageMessage",
            "timestamp": 1_600_000_000, "downloadUrl": "u", "mimeType": "image/jpeg",
            "chatId": "123-456@g.us",
        }
        http = FakeHttp([
            json_response(IMAGE_NOTIFICATION), json_response({}), null_response(),
            json_response([history_row, older_row]),
        ])
        messages = GreenApiProvider(make_config(read_mode="both"), http=http).fetch_messages()
        self.assertEqual([m.id for m in messages], ["OLD", "MSG1"])


class GreenApiWriteTests(unittest.TestCase):
    def test_send_text_quotes_the_triggering_message(self):
        http = FakeHttp([json_response({"idMessage": "REPLY1"})])
        provider = GreenApiProvider(make_config(reply_mode="quote"), http=http)
        reply_id = provider.send_text("123-456@g.us", "hello", reply_to="MSG1")
        self.assertEqual(reply_id, "REPLY1")
        body = http.calls[0]["json"]
        self.assertEqual(body["chatId"], "123-456@g.us")
        self.assertEqual(body["message"], "hello")
        self.assertEqual(body["quotedMessageId"], "MSG1")

    def test_plain_reply_mode_omits_quote(self):
        http = FakeHttp([json_response({"idMessage": "R"})])
        provider = GreenApiProvider(make_config(reply_mode="plain"), http=http)
        provider.send_text("c@g.us", "hi", reply_to="MSG1")
        self.assertNotIn("quotedMessageId", http.calls[0]["json"])

    def test_url_embeds_instance_and_token(self):
        http = FakeHttp([json_response({})])
        provider = GreenApiProvider(make_config(green_instance_id="1101", green_token="secret"), http=http)
        provider.send_text("c@g.us", "hi")
        self.assertIn("/waInstance1101/sendMessage/secret", http.calls[0]["url"])


class GreenApiChatListTests(unittest.TestCase):
    def test_list_chats_classifies_groups_and_direct_chats(self):
        payload = [
            {"id": "120363411021022965@g.us", "name": "Summary_automation"},
            {"id": "919700548274@c.us", "contactName": "Neighbour"},
            {"id": "", "name": "broken row"},
            "not a dict",
        ]
        http = FakeHttp([json_response(payload)])
        chats = GreenApiProvider(make_config(), http=http).list_chats()

        self.assertEqual(len(chats), 2)
        self.assertEqual(chats[0], {
            "id": "120363411021022965@g.us", "name": "Summary_automation", "kind": "group",
        })
        self.assertEqual(chats[1]["kind"], "direct")
        self.assertEqual(chats[1]["name"], "Neighbour")
        self.assertIn("getContacts", http.calls[0]["url"])

    def test_list_chats_tolerates_an_empty_response(self):
        http = FakeHttp([json_response([])])
        self.assertEqual(GreenApiProvider(make_config(), http=http).list_chats(), [])


class GreenApiDownloadTests(unittest.TestCase):
    def test_download_uses_url_from_the_message(self):
        seen = {}

        def downloader(url, **kwargs):
            seen["url"] = url
            seen["max_bytes"] = kwargs.get("max_bytes")
            return b"bytes", "image/jpeg"

        provider = GreenApiProvider(make_config(), http=FakeHttp([]), downloader=downloader)
        from tests.helpers import image_message

        data, mime = provider.download_image(image_message("MSG1", url="https://media/a.jpg"))
        self.assertEqual(data, b"bytes")
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(seen["url"], "https://media/a.jpg")
        self.assertEqual(seen["max_bytes"], make_config().max_image_bytes)

    def test_download_falls_back_to_downloadfile_endpoint(self):
        from tests.helpers import image_message

        http = FakeHttp([json_response({"downloadUrl": "https://media/resolved.jpg"})])
        captured = {}

        def downloader(url, **kwargs):
            captured["url"] = url
            return b"x", "image/png"

        provider = GreenApiProvider(make_config(), http=http, downloader=downloader)
        provider.download_image(image_message("MSG1", url=""))
        self.assertEqual(captured["url"], "https://media/resolved.jpg")
        self.assertIn("downloadFile", http.calls[0]["url"])

    def test_missing_download_url_raises(self):
        from tests.helpers import image_message

        provider = GreenApiProvider(make_config(), http=FakeHttp([json_response({})]))
        with self.assertRaises(HttpError):
            provider.download_image(image_message("MSG1", url=""))


if __name__ == "__main__":
    unittest.main()
