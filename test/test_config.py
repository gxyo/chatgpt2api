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


if __name__ == "__main__":
    unittest.main()
