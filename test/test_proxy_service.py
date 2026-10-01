import unittest
from unittest.mock import patch

from services.proxy_service import (
    ProxySettingsStore,
    normalize_proxy_url,
)


class FakeConfig:
    def __init__(self, legacy_proxy: str = "") -> None:
        self.legacy_proxy = legacy_proxy

    def get_proxy_settings(self) -> str:
        return self.legacy_proxy


class ProxyServiceTests(unittest.TestCase):
    def test_normalize_proxy_url_strips_and_converts_socks_schemes(self) -> None:
        self.assertEqual(normalize_proxy_url("  http://proxy.example:8080  "), "http://proxy.example:8080")
        self.assertEqual(normalize_proxy_url("\thttps://proxy.example:8443\n"), "https://proxy.example:8443")
        self.assertEqual(normalize_proxy_url(" socks://proxy.example:1080 "), "socks5h://proxy.example:1080")
        self.assertEqual(normalize_proxy_url("socks5://proxy.example:1080"), "socks5h://proxy.example:1080")
        self.assertEqual(normalize_proxy_url(" socks5h://proxy.example:1080 "), "socks5h://proxy.example:1080")
        self.assertEqual(normalize_proxy_url("   "), "")

    def test_build_session_kwargs_falls_back_to_global_proxy(self) -> None:
        store = ProxySettingsStore(FakeConfig(legacy_proxy="  http://legacy.example:8080  "))

        kwargs = store.build_session_kwargs(impersonate="chrome")

        self.assertEqual(kwargs["impersonate"], "chrome")
        self.assertEqual(kwargs["proxy"], "http://legacy.example:8080")

    def test_build_session_kwargs_omits_proxy_when_nothing_configured(self) -> None:
        kwargs = ProxySettingsStore(FakeConfig()).build_session_kwargs(impersonate="chrome")

        self.assertEqual(kwargs, {"impersonate": "chrome"})

    def test_account_proxy_wins_over_explicit_and_global_proxy(self) -> None:
        store = ProxySettingsStore(FakeConfig(legacy_proxy="http://legacy.example:8080"))

        kwargs = store.build_session_kwargs(
            account={"proxy": " socks://account.example:1080 "},
            proxy="http://explicit.example:8080",
        )

        self.assertEqual(kwargs["proxy"], "socks5h://account.example:1080")

    def test_explicit_proxy_wins_over_global_proxy(self) -> None:
        store = ProxySettingsStore(FakeConfig(legacy_proxy="http://legacy.example:8080"))

        kwargs = store.build_session_kwargs(proxy=" socks5://explicit.example:1080 ")

        self.assertEqual(kwargs["proxy"], "socks5h://explicit.example:1080")

    def test_get_profile_reports_the_selected_source(self) -> None:
        store = ProxySettingsStore(FakeConfig(legacy_proxy="http://legacy.example:8080"))

        self.assertEqual(store.get_profile().proxy_source, "global")
        self.assertEqual(store.get_profile(proxy="http://explicit.example:8080").proxy_source, "explicit")
        self.assertEqual(
            store.get_profile(account={"proxy": "http://account.example:8080"}).proxy_source,
            "account",
        )
        self.assertEqual(ProxySettingsStore(FakeConfig()).get_profile().proxy_source, "direct")

    def test_proxy_test_error_redacts_proxy_credentials(self) -> None:
        class FailingSession:
            def __init__(self, **kwargs: object) -> None:
                pass

            def get(self, *args: object, **kwargs: object) -> object:
                raise RuntimeError("proxy failed for http://user:pass@proxy.example:8080")

            def close(self) -> None:
                pass

        with patch("services.proxy_service.Session", FailingSession):
            result = __import__("services.proxy_service", fromlist=["test_proxy"]).test_proxy(
                "http://user:pass@proxy.example:8080"
            )

        self.assertFalse(result["ok"])
        self.assertIn("[REDACTED]", result["error"])
        self.assertNotIn("user:pass", result["error"])


if __name__ == "__main__":
    unittest.main()
