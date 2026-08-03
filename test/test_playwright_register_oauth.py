import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from services.register.playwright_register import (
    OAUTH_AUTHORIZE_PATTERNS,
    OAUTH_CALLBACK_PATTERN,
    _install_oauth_routes,
    _replace_pkce_params,
)


class FakePage:
    def __init__(self) -> None:
        self.routes = {}

    async def route(self, pattern, handler) -> None:
        self.routes[pattern] = handler


class FakeRoute:
    def __init__(self, url: str) -> None:
        self.request = SimpleNamespace(url=url)
        self.continued_url = None
        self.fulfilled = None

    async def continue_(self, url=None) -> None:
        self.continued_url = url or self.request.url

    async def fulfill(self, **kwargs) -> None:
        self.fulfilled = kwargs


class PlaywrightRegisterOAuthTests(unittest.IsolatedAsyncioTestCase):
    def test_replace_pkce_params_preserves_other_query_values(self) -> None:
        url = (
            "https://auth.openai.com/api/oauth/oauth2/auth?"
            "scope=openid&scope=email&state=state-a&code_challenge=old&code_challenge_method=plain"
        )

        rewritten = _replace_pkce_params(url, "new-challenge")
        params = parse_qs(urlparse(rewritten).query)

        self.assertEqual(params["scope"], ["openid", "email"])
        self.assertEqual(params["state"], ["state-a"])
        self.assertEqual(params["code_challenge"], ["new-challenge"])
        self.assertEqual(params["code_challenge_method"], ["S256"])

    async def test_current_and_legacy_authorize_routes_replace_pkce(self) -> None:
        page = FakePage()
        captured_codes = []
        await _install_oauth_routes(page, 1, "our-challenge", captured_codes)

        self.assertEqual(set(page.routes), {*OAUTH_AUTHORIZE_PATTERNS, OAUTH_CALLBACK_PATTERN})
        for pattern, path in zip(
            OAUTH_AUTHORIZE_PATTERNS,
            ("oauth/authorize", "api/oauth/oauth2/auth", "api/accounts/authorize"),
        ):
            route = FakeRoute(
                f"https://auth.openai.com/{path}?state=a&code_challenge=browser-challenge&code_challenge_method=S256"
            )
            await page.routes[pattern](route)
            params = parse_qs(urlparse(route.continued_url).query)
            self.assertEqual(params["code_challenge"], ["our-challenge"])
            self.assertEqual(params["code_challenge_method"], ["S256"])

    async def test_platform_callback_is_fulfilled_without_reaching_platform(self) -> None:
        page = FakePage()
        captured_codes = []
        await _install_oauth_routes(page, 1, "our-challenge", captured_codes)

        route = FakeRoute("https://platform.openai.com/auth/callback?code=one-time-code&state=state-a")
        await page.routes[OAUTH_CALLBACK_PATTERN](route)

        self.assertEqual(captured_codes, ["one-time-code"])
        self.assertIsNone(route.continued_url)
        self.assertEqual(route.fulfilled["status"], 200)

    async def test_unrelated_callback_is_not_intercepted(self) -> None:
        page = FakePage()
        captured_codes = []
        await _install_oauth_routes(page, 1, "our-challenge", captured_codes)

        route = FakeRoute("https://auth.openai.com/auth/callback?code=other-code")
        await page.routes[OAUTH_CALLBACK_PATTERN](route)

        self.assertEqual(captured_codes, [])
        self.assertEqual(route.continued_url, route.request.url)
        self.assertIsNone(route.fulfilled)


if __name__ == "__main__":
    unittest.main()
