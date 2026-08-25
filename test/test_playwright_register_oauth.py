import json
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock, MagicMock, patch

from services.register.playwright_register import (
    OAUTH_AUTHORIZE_PATTERNS,
    OAUTH_CALLBACK_PATTERN,
    _install_oauth_routes,
    _exchange_oauth_token_in_browser,
    _fill_birthdate,
    _replace_pkce_params,
    _run_signup_state_machine,
    _switch_to_password_if_offered,
    _wait_for_signup_step,
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

    async def test_legacy_password_then_otp_flow(self) -> None:
        page = SimpleNamespace()
        with patch(
            "services.register.playwright_register._wait_for_signup_step",
            AsyncMock(side_effect=["password", "otp", "profile", "complete"]),
        ), patch(
            "services.register.playwright_register._submit_password", AsyncMock()
        ) as submit_password, patch(
            "services.register.playwright_register._submit_otp", AsyncMock()
        ) as submit_otp, patch(
            "services.register.playwright_register._fill_profile", AsyncMock()
        ) as fill_profile, patch(
            "services.register.playwright_register._switch_to_password_if_offered", AsyncMock()
        ) as switch_to_password:
            password_set = await _run_signup_state_machine(
                page, 1, "Secret123!", "Test User", "25", {"address": "test@example.com"}, []
            )

        self.assertTrue(password_set)
        submit_password.assert_awaited_once()
        submit_otp.assert_awaited_once()
        fill_profile.assert_awaited_once()
        switch_to_password.assert_not_awaited()

    async def test_new_otp_page_switches_to_password_flow(self) -> None:
        page = SimpleNamespace()
        with patch(
            "services.register.playwright_register._wait_for_signup_step",
            AsyncMock(side_effect=["otp", "password", "otp", "profile", "complete"]),
        ), patch(
            "services.register.playwright_register._switch_to_password_if_offered",
            AsyncMock(return_value=True),
        ) as switch_to_password, patch(
            "services.register.playwright_register._submit_password", AsyncMock()
        ) as submit_password, patch(
            "services.register.playwright_register._submit_otp", AsyncMock()
        ) as submit_otp, patch(
            "services.register.playwright_register._fill_profile", AsyncMock()
        ) as fill_profile:
            password_set = await _run_signup_state_machine(
                page, 2, "Secret123!", "Test User", "25", {"address": "test@example.com"}, []
            )

        self.assertTrue(password_set)
        switch_to_password.assert_awaited_once()
        submit_password.assert_awaited_once()
        submit_otp.assert_awaited_once()
        fill_profile.assert_awaited_once()

    async def test_passwordless_otp_flow_does_not_save_generated_password(self) -> None:
        page = SimpleNamespace()
        with patch(
            "services.register.playwright_register._wait_for_signup_step",
            AsyncMock(side_effect=["otp", "profile", "complete"]),
        ), patch(
            "services.register.playwright_register._switch_to_password_if_offered",
            AsyncMock(return_value=False),
        ), patch(
            "services.register.playwright_register._submit_otp", AsyncMock()
        ) as submit_otp, patch(
            "services.register.playwright_register._fill_profile", AsyncMock()
        ) as fill_profile:
            password_set = await _run_signup_state_machine(
                page, 3, "Secret123!", "Test User", "25", {"address": "test@example.com"}, []
            )

        self.assertFalse(password_set)
        submit_otp.assert_awaited_once()
        fill_profile.assert_awaited_once()

    async def test_continue_with_password_control_is_clicked(self) -> None:
        option = MagicMock()
        option.first = option
        option.is_visible = AsyncMock(return_value=True)
        option.click = AsyncMock()
        password_input = MagicMock()
        password_input.first = password_input
        password_input.wait_for = AsyncMock()
        page = MagicMock()
        page.locator.side_effect = [option, password_input]

        switched = await _switch_to_password_if_offered(page, 4)

        self.assertTrue(switched)
        option.click.assert_awaited_once()
        password_input.wait_for.assert_awaited_once_with(state="visible", timeout=15_000)

    async def test_about_you_age_input_is_not_misclassified_as_otp(self) -> None:
        page = MagicMock()
        page.url = "https://auth.openai.com/about-you"

        state = await _wait_for_signup_step(page, [])

        self.assertEqual(state, "profile")
        page.locator.assert_not_called()

    async def test_segmented_birthdate_fills_visible_month_day_year_controls(self) -> None:
        native_input = MagicMock()
        native_input.first = native_input
        native_input.is_visible = AsyncMock(return_value=False)
        segments = MagicMock()
        segments.count = AsyncMock(return_value=3)
        controls = []
        for label in ("month, Date of birth", "day, Date of birth", "year, Date of birth"):
            control = MagicMock()
            control.is_visible = AsyncMock(return_value=True)
            control.get_attribute = AsyncMock(return_value=label)
            control.fill = AsyncMock()
            controls.append(control)
        segments.nth.side_effect = controls
        page = MagicMock()
        page.locator.side_effect = [native_input, segments]

        filled = await _fill_birthdate(page, "2000-01-02")

        self.assertTrue(filled)
        controls[0].fill.assert_awaited_once_with("01")
        controls[1].fill.assert_awaited_once_with("02")
        controls[2].fill.assert_awaited_once_with("2000")

    async def test_visible_birthdate_input_uses_placeholder_order(self) -> None:
        date_input = MagicMock()
        date_input.first = date_input
        date_input.is_visible = AsyncMock(return_value=True)
        date_input.get_attribute = AsyncMock(side_effect=["text", "YYYY/MM/DD"])
        date_input.fill = AsyncMock()
        page = MagicMock()
        page.locator.return_value = date_input

        filled = await _fill_birthdate(page, "2000-01-02")

        self.assertTrue(filled)
        date_input.fill.assert_awaited_once_with("2000/01/02")

    async def test_token_exchange_user_error_retries_in_browser_context(self) -> None:
        first_response = SimpleNamespace(
            status=400,
            text=AsyncMock(return_value=json.dumps({"error": {"code": "token_exchange_user_error"}})),
            headers={"x-request-id": "req-first"},
        )
        second_response = SimpleNamespace(
            status=200,
            text=AsyncMock(return_value=json.dumps({"access_token": "access", "refresh_token": "refresh"})),
            headers={"x-request-id": "req-second"},
        )
        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        context = MagicMock()
        context.request.post = AsyncMock(side_effect=[first_response, second_response])

        tokens = await _exchange_oauth_token_in_browser(
            page, context, 5, "one-time-code", "code-verifier", "http://proxy.example:8080"
        )

        self.assertEqual(tokens["access_token"], "access")
        self.assertEqual(context.request.post.await_count, 2)
        page.wait_for_timeout.assert_awaited_once_with(1000)


if __name__ == "__main__":
    unittest.main()
