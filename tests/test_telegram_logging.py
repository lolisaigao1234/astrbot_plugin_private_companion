import asyncio
import logging
from types import SimpleNamespace
import unittest

import httpx

from companion_v2.adapters.astrbot.telegram_logging import redact_telegram_token


class TelegramLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_overlapping_sends_preserve_unrelated_structured_logs(self):
        logger = logging.getLogger("httpx")
        first, second = SimpleNamespace(token="1:synthetic"), SimpleNamespace(token="2:synthetic")
        entered, resumed = asyncio.Event(), asyncio.Event()

        async def send_first():
            with redact_telegram_token(first):
                entered.set()
                await resumed.wait()
                logger.info("sent %(token)s at %(progress)d%%", {"token": first.token, "progress": 50})

        with self.assertLogs(logger, "INFO") as logs:
            task = asyncio.create_task(send_first())
            await entered.wait()
            with redact_telegram_token(second):
                logger.info("request %s", second.token)
                logger.info("status %03d", 7)
            resumed.set()
            await task
        messages = [record.getMessage() for record in logs.records]
        self.assertEqual(messages, ["request [redacted]", "status 007", "sent [redacted] at 50%"])
        self.assertEqual([record.args for record in logs.records], [(), (7,), ()])

    async def test_httpx_request_keeps_percent_encoded_query_and_scrubs_record_args(self):
        bot = SimpleNamespace(token="123:synthetic")
        transport = httpx.MockTransport(lambda request: httpx.Response(200))
        async with httpx.AsyncClient(transport=transport) as http:
            with self.assertLogs("httpx", "INFO") as logs, redact_telegram_token(bot):
                await http.post(f"https://telegram.invalid/bot{bot.token}/sendMessage?q=a%20b")
        record, = logs.records
        self.assertIn("/bot[redacted]/sendMessage?q=a%20b", record.getMessage())
        self.assertNotIn(bot.token, repr(vars(record)))

    async def test_hidden_mapping_key_is_safe_for_json_handlers(self):
        bot = SimpleNamespace(token="9:synthetic")
        logger = logging.getLogger("astrbot")
        with self.assertLogs(logger, "INFO") as logs, redact_telegram_token(bot):
            logger.info("%(status)d", {"status": 200, "url": bot.token})
        record, = logs.records
        self.assertEqual(record.getMessage(), "200")
        self.assertEqual(record.args, ())

    async def test_custom_format_record_and_filter_cleanup_on_error(self):
        bot = SimpleNamespace(token="9:synthetic")
        logger = logging.getLogger("httpx")
        original = list(logger.filters)
        with self.assertRaises(RuntimeError), redact_telegram_token(bot):
            for arg, expected in [(bot.token, ()), ("ordinary", ("ordinary",))]:
                record = logger.makeRecord(logger.name, logging.INFO, "", 0, "custom {}", (arg,), None)
                self.assertTrue(logger.filter(record))
                self.assertEqual(record.args, expected)
            raise RuntimeError
        self.assertEqual(logger.filters, original)
