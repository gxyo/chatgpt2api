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
    _return_to_otp_signup,
    _run_signup_state_machine,
    _submit_password,
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
            "services.register.playwright_register._submit_password", AsyncMock(return_value=True)
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
            "services.register.playwright_register._submit_password", AsyncMock(return_value=True)
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

    async def test_rejected_password_flow_falls_back_to_otp_without_switching_back(self) -> None:
        page = SimpleNamespace()
        with patch(
            "services.register.playwright_register._wait_for_signup_step",
            AsyncMock(side_effect=["otp", "password", "otp", "profile", "complete"]),
        ), patch(
            "services.register.playwright_register._switch_to_password_if_offered",
            AsyncMock(return_value=True),
        ) as switch_to_password, patch(
            "services.register.playwright_register._submit_password", AsyncMock(return_value=False)
        ) as submit_password, patch(
            "services.register.playwright_register._submit_otp", AsyncMock()
        ) as submit_otp, patch(
            "services.register.playwright_register._fill_profile", AsyncMock()
        ):
            password_set = await _run_signup_state_machine(
                page, 3, "Secret123!", "Test User", "25", {"address": "test@example.com"}, []
            )

        self.assertFalse(password_set)
        switch_to_password.assert_awaited_once()
        submit_password.assert_awaited_once()
        submit_otp.assert_awaited_once()

    async def test_submit_password_detects_rejection_and_returns_to_otp(self) -> None:
        password_input = MagicMock()
        password_input.first = password_input
        password_input.wait_for = AsyncMock()
        password_input.fill = AsyncMock()
        password_input.is_visible = AsyncMock(return_value=True)
        submit_button = MagicMock()
        submit_button.first = submit_button
        submit_button.click = AsyncMock()
        rejection = MagicMock()
        rejection.first = rejection
        rejection.is_visible = AsyncMock(return_value=True)
        page = MagicMock()
        page.locator.side_effect = [password_input, submit_button, rejection]

        with patch(
            "services.register.playwright_register._return_to_otp_signup", AsyncMock()
        ) as return_to_otp:
            password_set = await _submit_password(page, 4, "Secret123456!")

        self.assertFalse(password_set)
        password_input.fill.assert_awaited_once_with("Secret123456!")
        submit_button.click.assert_awaited_once()
        return_to_otp.assert_awaited_once()

    async def test_password_rejection_clicks_one_time_code_option(self) -> None:
        option = MagicMock()
        option.first = option
        option.is_visible = AsyncMock(return_value=True)
        option.click = AsyncMock()
        code_input = MagicMock()
        code_input.first = code_input
        code_input.wait_for = AsyncMock()
        page = MagicMock()
        page.locator.side_effect = [option, code_input]

        await _return_to_otp_signup(page, 5)

        option.click.assert_awaited_once()
        page.go_back.assert_not_called()
        code_input.wait_for.assert_awaited_once_with(state="visible", timeout=15_000)

    async def test_password_rejection_uses_browser_back_when_option_is_missing(self) -> None:
        option = MagicMock()
        option.first = option
        option.is_visible = AsyncMock(return_value=False)
        code_input = MagicMock()
        code_input.first = code_input
        code_input.wait_for = AsyncMock()
        page = MagicMock()
        page.go_back = AsyncMock()
        page.locator.side_effect = [option, code_input]

        await _return_to_otp_signup(page, 6)

        page.go_back.assert_awaited_once_with(wait_until="domcontentloaded", timeout=120_000)
        code_input.wait_for.assert_awaited_once_with(state="visible", timeout=15_000)

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

    async def test_token_exchange_uses_chrome_session_with_browser_cookies(self) -> None:
        session = MagicMock()
        session.cookies.set = MagicMock()
        context = MagicMock()
        context.cookies = AsyncMock(return_value=[{
            "name": "session-cookie",
            "value": "cookie-value",
            "domain": ".openai.com",
            "path": "/",
            "secure": True,
        }])
        page = MagicMock()

        with patch(
            "services.register.playwright_register.curl_requests.Session", return_value=session
        ) as create_chrome_session, patch(
            "services.register.playwright_register.request_platform_oauth_token",
            return_value={"access_token": "access", "refresh_token": "refresh"},
        ) as exchange_token:
            tokens = await _exchange_oauth_token_in_browser(
                page, context, 5, "one-time-code", "code-verifier", "http://proxy.example:8080"
            )

        self.assertEqual(tokens["access_token"], "access")
        create_chrome_session.assert_called_once_with(
            impersonate="chrome", verify=False, proxy="http://proxy.example:8080"
        )
        session.cookies.set.assert_called_once_with(
            "session-cookie", "cookie-value", domain=".openai.com", path="/", secure=True
        )
        exchange_token.assert_called_once_with(session, "one-time-code", "code-verifier")
        context.request.post.assert_not_called()
        session.close.assert_called_once()

    async def test_token_exchange_falls_back_to_playwright_context(self) -> None:
        response = SimpleNamespace(
            status=200,
            text=AsyncMock(return_value=json.dumps({"access_token": "access", "refresh_token": "refresh"})),
            headers={"x-request-id": "req-fallback"},
        )
        session = MagicMock()
        page = MagicMock()
        context = MagicMock()
        context.cookies = AsyncMock(return_value=[])
        context.request.post = AsyncMock(return_value=response)

        with patch(
            "services.register.playwright_register.curl_requests.Session", return_value=session
        ), patch(
            "services.register.playwright_register.request_platform_oauth_token",
            side_effect=RuntimeError("primary rejected"),
        ):
            tokens = await _exchange_oauth_token_in_browser(
                page, context, 5, "one-time-code", "code-verifier", ""
            )

        self.assertEqual(tokens["access_token"], "access")
        context.request.post.assert_awaited_once()
        session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
