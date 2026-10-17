import json
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
ROOT_CONFIG_FILE = ROOT_DIR / "config.json"


class ConfigLoadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._created_root_config = False
        if not ROOT_CONFIG_FILE.exists():
            ROOT_CONFIG_FILE.write_text(json.dumps({"auth-key": "test-auth"}), encoding="utf-8")
            cls._created_root_config = True

        from services import config as config_module

        cls.config_module = config_module

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._created_root_config and ROOT_CONFIG_FILE.exists():
            ROOT_CONFIG_FILE.unlink()

    def test_load_settings_ignores_directory_config_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            data_dir = base_dir / "data"
            config_dir = base_dir / "config.json"
            os_auth_key = "env-auth"

            config_dir.mkdir()

            module = self.config_module
            old_base_dir = module.BASE_DIR
            old_data_dir = module.DATA_DIR
            old_config_file = module.CONFIG_FILE
            old_env_auth_key = module.os.environ.get("CHATGPT2API_AUTH_KEY")
            try:
                module.BASE_DIR = base_dir
                module.DATA_DIR = data_dir
                module.CONFIG_FILE = config_dir
                module.os.environ["CHATGPT2API_AUTH_KEY"] = os_auth_key

                settings = module._load_settings()

                self.assertEqual(settings.auth_key, os_auth_key)
                self.assertEqual(settings.refresh_account_interval_minute, 5)
                self.assertIsNone(settings.refresh_all_accounts_interval_minute)
            finally:
                module.BASE_DIR = old_base_dir
                module.DATA_DIR = old_data_dir
                module.CONFIG_FILE = old_config_file
                if old_env_auth_key is None:
                    module.os.environ.pop("CHATGPT2API_AUTH_KEY", None)
                else:
                    module.os.environ["CHATGPT2API_AUTH_KEY"] = old_env_auth_key

    def test_refresh_all_interval_defaults_to_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.json"
            config_file.write_text(json.dumps({"auth-key": "test-auth"}), encoding="utf-8")

            store = self.config_module.ConfigStore(config_file)

            self.assertIsNone(store.refresh_all_accounts_interval_minute)
            self.assertIsNone(store.get()["refresh_all_accounts_interval_minute"])

    def test_refresh_intervals_can_be_disabled_when_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.json"
            config_file.write_text(json.dumps({"auth-key": "test-auth"}), encoding="utf-8")
            store = self.config_module.ConfigStore(config_file)

            config = store.update({
                "refresh_account_interval_minute": "",
                "refresh_all_accounts_interval_minute": None,
            })
            saved = json.loads(config_file.read_text(encoding="utf-8"))

            self.assertIsNone(config["refresh_account_interval_minute"])
            self.assertIsNone(config["refresh_all_accounts_interval_minute"])
            self.assertIsNone(saved["refresh_account_interval_minute"])
            self.assertIsNone(saved["refresh_all_accounts_interval_minute"])

    def test_refresh_interval_zero_is_treated_as_disabled_for_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.json"
            config_file.write_text(json.dumps({"auth-key": "test-auth"}), encoding="utf-8")
            store = self.config_module.ConfigStore(config_file)

            config = store.update({
                "refresh_account_interval_minute": 0,
                "refresh_all_accounts_interval_minute": 0,
            })

            self.assertIsNone(config["refresh_account_interval_minute"])
            self.assertIsNone(config["refresh_all_accounts_interval_minute"])

    def test_positive_refresh_intervals_are_saved_as_integers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.json"
            config_file.write_text(json.dumps({"auth-key": "test-auth"}), encoding="utf-8")
            store = self.config_module.ConfigStore(config_file)

            config = store.update({
                "refresh_account_interval_minute": "60",
                "refresh_all_accounts_interval_minute": "2",
            })

            self.assertEqual(config["refresh_account_interval_minute"], 60)
            self.assertEqual(config["refresh_all_accounts_interval_minute"], 2)

    def test_image_cleanup_schedule_is_normalized_when_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.json"
            config_file.write_text(json.dumps({"auth-key": "test-auth"}), encoding="utf-8")
            store = self.config_module.ConfigStore(config_file)

            self.assertFalse(store.image_cleanup_schedule_configured)
            self.assertEqual(store.get()["image_cleanup_interval_days"], 1)
            self.assertEqual(store.get()["image_cleanup_time"], "03:00")

            config = store.update({
                "image_cleanup_interval_days": "5",
                "image_cleanup_time": "7:05",
            })

            self.assertTrue(store.image_cleanup_schedule_configured)
            self.assertEqual(config["image_cleanup_interval_days"], 5)
            self.assertEqual(config["image_cleanup_time"], "07:05")

    def test_cleanup_keep_today_flags_default_to_preserving_existing_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.json"
            config_file.write_text(json.dumps({"auth-key": "test-auth"}), encoding="utf-8")
            store = self.config_module.ConfigStore(config_file)

            # 图片清理默认删除执行日当天的数据（旧行为）；日志清理默认保留当天。
            self.assertFalse(store.image_cleanup_keep_today)
            self.assertTrue(store.log_cleanup_keep_today)

            config = store.update({
                "image_cleanup_keep_today": "yes",
                "log_cleanup_keep_today": "0",
            })

            self.assertIs(config["image_cleanup_keep_today"], True)
            self.assertIs(config["log_cleanup_keep_today"], False)
            saved = json.loads(config_file.read_text(encoding="utf-8"))
            self.assertIs(saved["image_cleanup_keep_today"], True)
            self.assertIs(saved["log_cleanup_keep_today"], False)

    def test_invalid_image_cleanup_schedule_uses_safe_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.json"
            config_file.write_text(json.dumps({"auth-key": "test-auth"}), encoding="utf-8")
            store = self.config_module.ConfigStore(config_file)

            config = store.update({
                "image_cleanup_interval_days": 2,
                "image_cleanup_time": "25:99",
            })

            self.assertEqual(config["image_cleanup_interval_days"], 1)
            self.assertEqual(config["image_cleanup_time"], "03:00")

    def test_image_cleanup_minutes_schedule_is_normalized_when_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.json"
            config_file.write_text(json.dumps({"auth-key": "test-auth"}), encoding="utf-8")
            store = self.config_module.ConfigStore(config_file)

            config = store.update({
                "image_cleanup_interval_unit": "minutes",
                "image_cleanup_interval_minutes": "15",
            })

            # 按分钟模式不依赖「每 N 天 + 固定时刻」，只认分钟数字。
            self.assertTrue(store.image_cleanup_schedule_configured)
            self.assertEqual(config["image_cleanup_interval_unit"], "minutes")
            self.assertEqual(config["image_cleanup_interval_minutes"], 15)

            self.assertEqual(store.update({"image_cleanup_interval_minutes": 0})["image_cleanup_interval_minutes"], 1)
            self.assertEqual(
                store.update({"image_cleanup_interval_minutes": 10 ** 9})["image_cleanup_interval_minutes"],
                10080,
            )
            self.assertEqual(
                store.update({"image_cleanup_interval_unit": "hourly"})["image_cleanup_interval_unit"],
                "days",
            )

    def test_upstream_model_settings_have_current_safe_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.json"
            config_file.write_text(json.dumps({"auth-key": "test-auth"}), encoding="utf-8")
            store = self.config_module.ConfigStore(config_file)

            public_config = store.get()

            self.assertEqual(store.default_upstream_model_name, "gpt-5-5")
            self.assertEqual(store.default_thinking_effort, "auto")
            self.assertEqual(public_config["default_upstream_model_name"], "gpt-5-5")
            self.assertEqual(public_config["default_thinking_effort"], "auto")

    def test_invalid_default_thinking_effort_falls_back_to_auto(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.json"
            config_file.write_text(json.dumps({
                "auth-key": "test-auth",
                "default_thinking_effort": "unsupported",
            }), encoding="utf-8")

            store = self.config_module.ConfigStore(config_file)

            self.assertEqual(store.default_thinking_effort, "auto")

    def test_model_pricing_defaults_to_empty_and_is_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.json"
            config_file.write_text(json.dumps({"auth-key": "test-auth"}), encoding="utf-8")
            store = self.config_module.ConfigStore(config_file)

            self.assertEqual(store.model_pricing, {})
            self.assertEqual(store.get()["model_pricing"], {})

    def test_model_pricing_is_normalized_when_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.json"
            config_file.write_text(json.dumps({"auth-key": "test-auth"}), encoding="utf-8")
            store = self.config_module.ConfigStore(config_file)

            config = store.update({
                "model_pricing": {
                    " gpt-image-2 ": "0.5",
                    "gpt-image-2.5": 0,      # 0 是合法的「免费」，必须保留
                    "auto": "2.3456789",     # 四舍五入到 6 位
                    "": 9,                   # 空模型名丢弃
                    "negative": -1,          # 负数丢弃
                    "non-number": "abc",     # 非法值丢弃
                    "boolean": True,         # 布尔不是价格
                    "infinite": "inf",       # 非有限值丢弃
                    "nan": "nan",
                },
            })
            saved = json.loads(config_file.read_text(encoding="utf-8"))

            expected = {"gpt-image-2": 0.5, "gpt-image-2.5": 0.0, "auto": 2.345679}
            self.assertEqual(config["model_pricing"], expected)
            self.assertEqual(saved["model_pricing"], expected)
            self.assertEqual(store.model_pricing, expected)

    def test_non_dict_model_pricing_falls_back_to_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.json"
            config_file.write_text(json.dumps({
                "auth-key": "test-auth",
                "model_pricing": ["not", "a", "dict"],
            }), encoding="utf-8")
            store = self.config_module.ConfigStore(config_file)

            self.assertEqual(store.model_pricing, {})

            config = store.update({"model_pricing": "oops"})

            self.assertEqual(config["model_pricing"], {})

    def test_hidden_pricing_models_default_to_empty_and_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.json"
            config_file.write_text(json.dumps({"auth-key": "test-auth"}), encoding="utf-8")
            store = self.config_module.ConfigStore(config_file)

            self.assertEqual(store.hidden_pricing_models, [])
            self.assertEqual(store.get()["hidden_pricing_models"], [])

            updated = store.update({
                "hidden_pricing_models": [
                    " gpt-image-2 ",
                    "",
                    "gpt-image-2",  # 去重
                    "auto",
                    5,
                ],
            })
            saved = json.loads(config_file.read_text(encoding="utf-8"))
            expected = ["gpt-image-2", "auto", "5"]
            self.assertEqual(updated["hidden_pricing_models"], expected)
            self.assertEqual(saved["hidden_pricing_models"], expected)
            self.assertEqual(store.hidden_pricing_models, expected)

            non_list = store.update({"hidden_pricing_models": "gpt-image-2"})
            self.assertEqual(non_list["hidden_pricing_models"], [])

    def test_upstream_model_name_is_trimmed_and_empty_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.json"
            config_file.write_text(json.dumps({"auth-key": "test-auth"}), encoding="utf-8")
            store = self.config_module.ConfigStore(config_file)

            updated = store.update({"default_upstream_model_name": "  gpt-5-4  "})
            self.assertEqual(updated["default_upstream_model_name"], "gpt-5-4")
            self.assertEqual(store.get()["default_upstream_model_name"], "gpt-5-4")

            blank = store.update({"default_upstream_model_name": "   "})
            self.assertEqual(blank["default_upstream_model_name"], "gpt-5-5")

    def test_default_thinking_effort_is_normalized_when_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.json"
            config_file.write_text(json.dumps({"auth-key": "test-auth"}), encoding="utf-8")
            store = self.config_module.ConfigStore(config_file)

            extended = store.update({"default_thinking_effort": " EXTENDED "})
            self.assertEqual(extended["default_thinking_effort"], "extended")
            self.assertEqual(store.get()["default_thinking_effort"], "extended")

            fallback = store.update({"default_thinking_effort": "turbo"})
            self.assertEqual(fallback["default_thinking_effort"], "auto")


if __name__ == "__main__":
    unittest.main()
