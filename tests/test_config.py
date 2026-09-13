import unittest

from bot import config as config_module
from bot.config import ConfigError


BASE_ENV = {
    "GREEN_API_INSTANCE_ID": "1101",
    "GREEN_API_TOKEN": "token",
    "GEMINI_API_KEY": "key",
}


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = config_module.from_env(dict(BASE_ENV))
        cfg.raise_for_errors()
        self.assertEqual(cfg.provider, "greenapi")
        self.assertEqual(cfg.gemini_model, "gemini-3.8-flash")
        self.assertEqual(cfg.trigger_mode, "command_or_caption")
        self.assertEqual(cfg.read_mode, "both")
        self.assertEqual(cfg.gemini_max_output_tokens, 32768)
        self.assertEqual(cfg.gemini_thinking_budget, 0, "thinking must be off or replies truncate")
        self.assertFalse(cfg.dry_run)

    def test_missing_credentials_are_all_reported(self):
        cfg = config_module.from_env({})
        with self.assertRaises(ConfigError) as ctx:
            cfg.raise_for_errors()
        message = str(ctx.exception)
        self.assertIn("GREEN_API_INSTANCE_ID", message)
        self.assertIn("GREEN_API_TOKEN", message)
        self.assertIn("GEMINI_API_KEY", message)

    def test_dry_run_does_not_require_gemini_key(self):
        env = {"GREEN_API_INSTANCE_ID": "1", "GREEN_API_TOKEN": "t", "DRY_RUN": "true"}
        cfg = config_module.from_env(env)
        cfg.raise_for_errors()
        self.assertTrue(cfg.dry_run)

    def test_meta_provider_requires_its_own_credentials(self):
        cfg = config_module.from_env({"PROVIDER": "meta", "GEMINI_API_KEY": "k"})
        with self.assertRaises(ConfigError) as ctx:
            cfg.raise_for_errors()
        self.assertIn("META_PHONE_NUMBER_ID", str(ctx.exception))
        self.assertIn("META_ACCESS_TOKEN", str(ctx.exception))

    def test_chat_ids_split_on_commas_and_newlines(self):
        env = dict(BASE_ENV, WHATSAPP_CHAT_IDS=" a@g.us, b@g.us\nc@c.us ,")
        cfg = config_module.from_env(env)
        self.assertEqual(cfg.chat_ids, ("a@g.us", "b@g.us", "c@c.us"))

    def test_thinking_budget_accepts_minus_one_for_dynamic(self):
        cfg = config_module.from_env(dict(BASE_ENV, GEMINI_THINKING_BUDGET="-1"))
        cfg.raise_for_errors()
        self.assertEqual(cfg.gemini_thinking_budget, -1)

    def test_thinking_budget_rejects_below_minus_one(self):
        cfg = config_module.from_env(dict(BASE_ENV, GEMINI_THINKING_BUDGET="-5"))
        with self.assertRaises(ConfigError):
            cfg.raise_for_errors()

    def test_invalid_enum_is_rejected(self):
        cfg = config_module.from_env(dict(BASE_ENV, TRIGGER_MODE="whenever"))
        with self.assertRaises(ConfigError) as ctx:
            cfg.raise_for_errors()
        self.assertIn("TRIGGER_MODE", str(ctx.exception))

    def test_invalid_numbers_are_rejected(self):
        cfg = config_module.from_env(dict(BASE_ENV, MAX_IMAGES_PER_REPLY="lots", HISTORY_COUNT="0"))
        with self.assertRaises(ConfigError) as ctx:
            cfg.raise_for_errors()
        message = str(ctx.exception)
        self.assertIn("MAX_IMAGES_PER_REPLY", message)
        self.assertIn("HISTORY_COUNT", message)

    def test_empty_command_prefix_rejected_for_command_modes(self):
        cfg = config_module.from_env(dict(BASE_ENV, TRIGGER_MODE="command", COMMAND_PREFIX=""))
        with self.assertRaises(ConfigError):
            cfg.raise_for_errors()

    def test_empty_command_prefix_allowed_for_any_image(self):
        cfg = config_module.from_env(dict(BASE_ENV, TRIGGER_MODE="any_image", COMMAND_PREFIX=""))
        cfg.raise_for_errors()

    def test_trailing_slash_stripped_from_bases(self):
        cfg = config_module.from_env(dict(BASE_ENV, GREEN_API_BASE="https://x.dev/"))
        self.assertEqual(cfg.green_api_base, "https://x.dev")


if __name__ == "__main__":
    unittest.main()
