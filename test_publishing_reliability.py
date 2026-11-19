import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import bot


class GeminiResponseTests(unittest.TestCase):
    def test_extracts_all_text_parts_from_complete_response(self):
        response = {
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": "Первая "}, {"text": "часть"}]},
            }]
        }
        self.assertEqual(bot.extract_gemini_text(response), "Первая часть")

    def test_rejects_truncated_response(self):
        response = {
            "candidates": [{
                "finishReason": "MAX_TOKENS",
                "content": {"parts": [{"text": "Оборванный текст"}]},
            }]
        }
        self.assertIsNone(bot.extract_gemini_text(response))

    def test_rejects_missing_candidates_and_empty_parts(self):
        self.assertIsNone(bot.extract_gemini_text({}))
        self.assertIsNone(bot.extract_gemini_text({
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": []},
            }]
        }))


class TelegramPublishingTests(unittest.IsolatedAsyncioTestCase):
    def test_long_text_is_split_under_telegram_limit(self):
        source = ("Абзац с текстом. " * 300)
        chunks = bot.split_telegram_text(source, max_length=4000)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 4000 for chunk in chunks))
        self.assertEqual(" ".join(chunks).replace("  ", " "), source.strip().replace("  ", " "))

    async def test_photo_failure_does_not_prevent_text_delivery(self):
        client = type("BotStub", (), {})()
        client.send_photo = AsyncMock(side_effect=RuntimeError("photo failed"))
        client.send_message = AsyncMock()
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "cover.jpg"
            image_path.write_bytes(b"not-a-real-image-but-file-exists")
            with patch.object(bot, "get_blog_post_kb", return_value=None):
                result = await bot.publish_blog_to_telegram(
                    client, 123, "Текст публикации", str(image_path)
                )
        self.assertFalse(result)
        client.send_photo.assert_awaited_once()
        client.send_message.assert_awaited_once()

    async def test_long_post_sends_text_in_chunks(self):
        client = type("BotStub", (), {})()
        client.send_photo = AsyncMock()
        client.send_message = AsyncMock()
        text = "А" * 8500
        with patch.object(bot, "get_blog_post_kb", return_value=None):
            result = await bot.publish_blog_to_telegram(client, 123, text)
        self.assertTrue(result)
        self.assertEqual(client.send_message.await_count, 3)
        self.assertTrue(all(
            len(call.kwargs["text"]) <= 4000
            for call in client.send_message.await_args_list
        ))


if __name__ == "__main__":
    unittest.main()

[executed on device: WIN-G67FBDK95CQ (1e199238-2864-4f34-94f1-59ce3e582cf2)]