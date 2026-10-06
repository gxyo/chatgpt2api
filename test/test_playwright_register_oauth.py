import json
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock, MagicMock, patch

from services.register.playwright_register import (
    OAUTH_ROUTE_PATTERN,
    OAUTH_TOKEN_PATH,
    _code_is_bound_to_our_challenge,
    _continue_identity_verification,
    _install_oauth_routes,
    _exchange_oauth_token_in_browser,
    _fill_birthdate,
    _match_verifier,
    _page_rejects_password_signup,
    _replace_pkce_params,
    _return_to_otp_signup,
    _secret_fingerprint,
    _run_signup_state_machine,
    _should_block_browser_oauth_request,
    _submit_password,
    _switch_to_password_if_offered,
    _verifier_candidates,
    _wait_for_signup_step,
    platform_oauth_client_id,
    platform_oauth_redirect_uri,
)
from utils.pkce import code_challenge_for


# 线上真实抓到的页面正文（结尾没有句号——旧选择器就是被这个标点废掉的）。
LIVE_PASSWORD_REJECTION_BODY = (
    "Create a password You’ll use this password to log in to ChatGPT and other OpenAI products "
    "Email address Edit Password Your password must contain: At least 12 characters . Complete. "
    "Failed to create account. Please try again Continue OR Sign up with a one-time code "
    "Already have an account? Log in Terms of UsePrivacy Policy"
)


def _oauth_context(**overrides) -> dict:
    """和 ``_browser_register_flow`` 里建的那份同形，免得测试用的字典少键（少键会 KeyError 而不是判定失败）。"""
    context = {
        "challenges": [],
        "client_id": "",
        "redirect_uri": "",
        "device_id": "",
        "verifier": "",
        "verifier_source": "自带",
        "issuer_fp": "",
        "rewritten_fps": set(),
    }
    context.update(overrides)
    return context


class FakePage:
    def __init__(self) -> None:
        self.routes = {}
        self.listeners = {}
        # 页面存储快照（sessionStorage/localStorage/cookie 摊平后的样子）。
        self.storage = {"values": [], "device_id": ""}

    async def route(self, pattern, handler) -> None:
        self.routes[pattern] = handler

    def on(self, event, handler) -> None:
        self.listeners[event] = handler

    async def evaluate(self, script, *args):
        return self.storage


class FakeFrame:
    """Stands in for a Playwright Frame; ``page.frames`` is what the blocker inspects."""

    def __init__(self, url: str, page=None) -> None:
        self.url = url
        self.page = page


def frame_at(url: str) -> FakeFrame:
    """A frame belonging to a page whose only frame currently sits at ``url``."""
    page = SimpleNamespace(frames=[])
    frame = FakeFrame(url, page)
    page.frames = [frame]
    return frame


class FakeRequest:
    def __init__(self, url: str, method: str = "GET", resource_type: str = "document", frame=None) -> None:
        self.url = url
        self.method = method
        self.resource_type = resource_type
        self._frame = frame

    @property
    def frame(self):
        # APIRequestContext 之类的非页面请求没有 frame，访问时会抛异常。
        if self._frame is None:
            raise Exception("frame is not available")
        return self._frame


class FakeRoute:
    def __init__(self, url: str, **request_kwargs) -> None:
        self.request = FakeRequest(url, **request_kwargs)
        self.continued_url = None
        self.fulfilled = None
        self.aborted = None
        self.fetched_urls: list[str] = []

    async def fetch(self, url=None, max_redirects=None):
        # 没人该调用它：替浏览器取响应的方案已经删掉了，
        # 重新引入会立刻让 fetched_urls 断言失败。
        self.fetched_urls.append(url or self.request.url)
        return None

    async def continue_(self, url=None) -> None:
        self.continued_url = url or self.request.url

    async def fulfill(self, **kwargs) -> None:
        self.fulfilled = kwargs

    async def abort(self, code=None) -> None:
        self.aborted = code or "aborted"


class FakeResponse:
    """What ``page.on("response")`` hands over for a redirect hop."""

    def __init__(self, status: int, url: str, location: str = "", request=None) -> None:
        self.status = status
        self.url = url
        self.headers = {"location": location} if location else {}
        self.request = request if request is not None else FakeRequest(url)


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

    async def test_single_catch_all_route_replaces_pkce_on_every_authorize_endpoint(self) -> None:
        page = FakePage()
        await _install_oauth_routes(page, 1, "our-challenge", [])

        # 授权端点会换名字，而且经常是 302 跳过来的，所以必须靠内容判断而不是 glob。
        self.assertEqual(set(page.routes), {OAUTH_ROUTE_PATTERN})
        handler = page.routes[OAUTH_ROUTE_PATTERN]
        for path in (
            "oauth/authorize",
            "api/oauth/oauth2/auth",
            "api/accounts/authorize",
            "api/accounts/oauth2/authorize",
        ):
            route = FakeRoute(
                f"https://auth.openai.com/{path}?state=a&code_challenge=browser-challenge&code_challenge_method=S256"
            )
            await handler(route)
            params = parse_qs(urlparse(route.continued_url).query)
            self.assertEqual(params["code_challenge"], ["our-challenge"])
            self.assertEqual(params["code_challenge_method"], ["S256"])
            self.assertIsNone(route.aborted)

    async def test_signup_page_authorize_load_is_left_to_the_browser(self) -> None:
        # 回归守卫：注册页加载本身就走 /oauth/authorize?screen_hint=signup&code_challenge=…。
        # 那时候页面不在资料页上，路由只能改写 PKCE 后交给浏览器自己跟跳转；
        # 一旦替它取回响应（哪怕只是取一跳），平台 SPA 就会坏在没有邮箱框的空页上。
        page = FakePage()
        captured: list[str] = []
        await _install_oauth_routes(page, 1, "our-challenge", captured, [])
        handler = page.routes[OAUTH_ROUTE_PATTERN]

        route = FakeRoute(
            "https://auth.openai.com/oauth/authorize"
            "?screen_hint=signup&client_id=app_x&code_challenge=browser-challenge&code_challenge_method=S256",
            frame=frame_at("https://auth.openai.com/oauth/authorize?screen_hint=signup"),
        )
        await handler(route)

        self.assertEqual(route.fetched_urls, [])  # 一个请求都不替浏览器发
        self.assertIsNone(route.fulfilled)
        self.assertIsNone(route.aborted)
        params = parse_qs(urlparse(route.continued_url).query)
        self.assertEqual(params["code_challenge"], ["our-challenge"])

    async def test_profile_submit_chain_is_never_fetched_by_us(self) -> None:
        # 资料页提交那条链是多跳的，中间跳要靠浏览器自己的 cookie 和头；
        # 我们替它走会拿到空响应、把页面停在 authorize 上（已经踩过一次）。
        page = FakePage()
        await _install_oauth_routes(page, 1, "our-challenge", [])
        handler = page.routes[OAUTH_ROUTE_PATTERN]

        route = FakeRoute(
            "https://auth.openai.com/api/accounts/profile",
            method="POST",
            resource_type="document",
            frame=frame_at("https://auth.openai.com/about-you"),
        )
        await handler(route)

        self.assertEqual(route.fetched_urls, [])
        self.assertEqual(route.continued_url, route.request.url)

    async def test_redirect_response_to_callback_is_captured_without_touching_requests(self) -> None:
        # 跳转目标路由不到，但响应事件看得到 location —— 纯观察，不改任何请求。
        page = FakePage()
        captured: list[str] = []
        events: list[str] = []
        await _install_oauth_routes(page, 1, "our-challenge", captured, events)

        on_response = page.listeners["response"]
        on_response(
            FakeResponse(
                302,
                "https://auth.openai.com/api/oauth/oauth2/auth?state=a",
                "https://platform.openai.com/auth/callback?code=one-time-code&state=a",
            )
        )

        self.assertEqual(captured, ["one-time-code"])
        self.assertTrue(any("响应 Location" in event for event in events))

    async def test_response_listener_resolves_a_relative_location(self) -> None:
        page = FakePage()
        captured: list[str] = []
        await _install_oauth_routes(page, 1, "our-challenge", captured, [])

        page.listeners["response"](
            FakeResponse(302, "https://auth.openai.com/api/oauth/oauth2/auth", "/auth/callback?code=relative-code")
        )

        self.assertEqual(captured, ["relative-code"])

    async def test_response_listener_ignores_non_callback_and_non_redirect_responses(self) -> None:
        page = FakePage()
        captured: list[str] = []
        await _install_oauth_routes(page, 1, "our-challenge", captured, [])
        on_response = page.listeners["response"]

        on_response(FakeResponse(302, "https://auth.openai.com/api/oauth/oauth2/auth", "/about-you"))
        on_response(FakeResponse(200, "https://platform.openai.com/auth/callback?code=too-late"))
        on_response(FakeResponse(302, "https://auth.openai.com/api/oauth/oauth2/auth"))

        self.assertEqual(captured, [])

    async def test_response_listener_does_not_recapture_after_the_code_is_in_hand(self) -> None:
        page = FakePage()
        captured: list[str] = []
        await _install_oauth_routes(page, 1, "our-challenge", captured, [])
        on_response = page.listeners["response"]

        on_response(FakeResponse(302, "https://auth.openai.com/a", "/auth/callback?code=first"))
        on_response(FakeResponse(302, "https://auth.openai.com/b", "/auth/callback?code=second"))

        self.assertEqual(captured, ["first"])

    async def test_code_issuer_challenge_is_recorded_with_the_capture(self) -> None:
        # 签发 code 的那次请求带了谁的 challenge，决定了这个 code 我们换不换得动。
        page = FakePage()
        events: list[str] = []
        await _install_oauth_routes(page, 1, "our-challenge", [], events)

        page.listeners["response"](
            FakeResponse(
                302,
                "https://auth.openai.com/api/oauth/oauth2/auth",
                "/auth/callback?code=abc",
                request=FakeRequest(
                    "https://auth.openai.com/api/oauth/oauth2/auth"
                    "?code_challenge=someone-elses&code_challenge_method=S256"
                ),
            )
        )

        issuer = [e for e in events if "签发" in e]
        self.assertEqual(len(issuer), 1)
        self.assertIn(_secret_fingerprint("someone-elses"), issuer[0])
        self.assertIn("/api/oauth/oauth2/auth", issuer[0])

    async def test_unroutable_challenge_requests_are_still_recorded(self) -> None:
        # 路由不到的跳转目标也一样会走 request 事件，所以别人发的 challenge 也能看见。
        page = FakePage()
        events: list[str] = []
        await _install_oauth_routes(page, 1, "our-challenge", [], events)
        on_request = page.listeners["request"]

        on_request(FakeRequest("https://auth.openai.com/about-you"))
        on_request(
            FakeRequest(
                "https://auth.openai.com/api/oauth/oauth2/auth?code_challenge=theirs&code_challenge_method=S256"
            )
        )
        on_request(
            FakeRequest(
                "https://auth.openai.com/api/oauth/oauth2/auth?code_challenge=theirs&code_challenge_method=S256"
            )
        )
        on_request(
            FakeRequest(
                "https://auth.openai.com/oauth/authorize?code_challenge=other&code_challenge_method=S256"
            )
        )

        challenges = [e for e in events if "带 challenge 的请求" in e]
        self.assertEqual(len(challenges), 2)
        self.assertTrue(any("/api/oauth/oauth2/auth" in e for e in challenges))
        self.assertTrue(any("/oauth/authorize" in e for e in challenges))

    async def test_callback_request_moment_is_recorded_once(self) -> None:
        page = FakePage()
        events: list[str] = []
        await _install_oauth_routes(page, 1, "our-challenge", [], events)
        on_request = page.listeners["request"]

        on_request(FakeRequest("https://auth.openai.com/about-you"))
        on_request(FakeRequest("https://platform.openai.com/auth/callback?code=abc"))
        on_request(FakeRequest("https://platform.openai.com/auth/callback?code=abc"))

        self.assertEqual(sum("浏览器已发出 callback 请求" in e for e in events), 1)

    async def test_callback_page_requests_are_blocked_so_the_code_survives(self) -> None:
        page = FakePage()
        events: list[str] = []
        await _install_oauth_routes(page, 1, "our-challenge", [], events)
        handler = page.routes[OAUTH_ROUTE_PATTERN]

        callback_frame = frame_at("https://platform.openai.com/auth/callback?code=one-time-code&state=a")
        route = FakeRoute(
            "https://platform.openai.com/backend-api/auth/session",
            method="POST",
            resource_type="fetch",
            frame=callback_frame,
        )
        await handler(route)

        self.assertIsNotNone(route.aborted)
        self.assertIsNone(route.continued_url)
        self.assertTrue(any("阻断" in event for event in events))

    async def test_token_endpoint_call_from_a_page_is_blocked(self) -> None:
        # 即使不是 callback 页面发起的，浏览器也不该自己去兑换 code（那是我们的活）。
        reason = _should_block_browser_oauth_request(
            FakeRequest(
                f"https://auth.openai.com{OAUTH_TOKEN_PATH}",
                method="POST",
                resource_type="fetch",
                frame=frame_at("https://platform.openai.com/auth/callback"),
            )
        )

        self.assertNotEqual(reason, "")

    async def test_callback_page_scripts_are_blocked_and_static_assets_allowed(self) -> None:
        page = FakePage()
        await _install_oauth_routes(page, 1, "our-challenge", [])
        handler = page.routes[OAUTH_ROUTE_PATTERN]
        callback_frame = frame_at("https://platform.openai.com/auth/callback?code=one-time-code")

        script = FakeRoute(
            "https://platform.openai.com/_next/static/chunks/main.js",
            resource_type="script",
            frame=callback_frame,
        )
        await handler(script)
        self.assertIsNotNone(script.aborted)

        stylesheet = FakeRoute(
            "https://platform.openai.com/_next/static/css/app.css",
            resource_type="stylesheet",
            frame=callback_frame,
        )
        await handler(stylesheet)
        self.assertIsNone(stylesheet.aborted)
        self.assertEqual(stylesheet.continued_url, stylesheet.request.url)

    async def test_own_navigation_away_from_callback_is_not_blocked(self) -> None:
        # 页面停在 callback 上时，注册流程自己的跳转/重试仍要放行，
        # 否则一次失败就没法恢复（曾经误伤过 page.goto）。
        page = FakePage()
        await _install_oauth_routes(page, 1, "our-challenge", [])
        handler = page.routes[OAUTH_ROUTE_PATTERN]
        callback_frame = frame_at("https://platform.openai.com/auth/callback?code=one-time-code")

        navigation = FakeRoute(
            "https://platform.openai.com/signup",
            resource_type="document",
            frame=callback_frame,
        )
        await handler(navigation)
        self.assertIsNone(navigation.aborted)

        form_post = FakeRoute(
            "https://platform.openai.com/auth/complete",
            method="POST",
            resource_type="document",
            frame=callback_frame,
        )
        await handler(form_post)
        self.assertIsNotNone(form_post.aborted)

    def test_verifier_candidates_flatten_storage_values(self) -> None:
        verifier = "v" * 60
        candidates = _verifier_candidates([
            json.dumps({"code_verifier": verifier, "state": "short"}),
            f"oai-did=abc; other={verifier}",
            "",
            None,
        ])

        self.assertIn(verifier, candidates)
        self.assertNotIn("", candidates)

    def test_verifier_matching_only_accepts_a_real_s256_preimage(self) -> None:
        verifier = "browser-verifier-" + "x" * 40
        challenge = code_challenge_for(verifier)

        # challenge 自己也在存储里（transaction 对象两个字段挨着），但它不是 verifier：
        # 用它去兑换只会换错人，所以只认能反算出 challenge 的那个明文。
        self.assertEqual(_match_verifier([challenge, verifier], [challenge]), verifier)
        self.assertEqual(_match_verifier([challenge], [challenge]), "")
        self.assertEqual(_match_verifier([verifier], []), "")

    async def test_authorize_is_rewritten_and_the_rewrite_is_recorded(self) -> None:
        page = FakePage()
        page.storage = {"values": [json.dumps({"code_verifier": "someone-elses"})], "device_id": ""}
        oauth = _oauth_context()
        events: list[str] = []
        await _install_oauth_routes(page, 1, "our-challenge", [], events, oauth)

        route = FakeRoute(
            "https://auth.openai.com/oauth/authorize"
            "?client_id=app_browser&code_challenge=browser-challenge&code_challenge_method=S256",
            frame=frame_at("https://platform.openai.com/signup"),
        )
        await page.routes[OAUTH_ROUTE_PATTERN](route)

        # 改写仍然照旧——但**不能**在这里读存储：请求正停在 handler 里，页面处于导航中，
        # page.evaluate 会一直等到超时也不返回（实测），反查只能等兑换前那次做。
        params = parse_qs(urlparse(route.continued_url).query)
        self.assertEqual(params["code_challenge"], ["our-challenge"])
        self.assertEqual(oauth["client_id"], "app_browser")
        self.assertEqual(oauth["rewritten_fps"], {_secret_fingerprint("browser-challenge")})
        self.assertEqual(oauth["verifier"], "")  # handler 里不下结论

    async def test_code_signed_by_a_rewritten_authorize_keeps_our_verifier(self) -> None:
        # 改写真的生效时（那次就是签发 code 的那次），自带 verifier 是对的，
        # 不该再去存储里换成浏览器那份。
        oauth = _oauth_context(
            issuer_fp=_secret_fingerprint("browser-challenge"),
            rewritten_fps={_secret_fingerprint("browser-challenge")},
        )

        self.assertTrue(_code_is_bound_to_our_challenge(oauth))

    async def test_code_signed_by_an_unrouted_authorize_needs_the_browser_verifier(self) -> None:
        # 跳转目标路由不到，改写的不是签发 code 的那次：签发指纹不在改写集合里。
        oauth = _oauth_context(
            issuer_fp=_secret_fingerprint("browser-challenge"),
            rewritten_fps={_secret_fingerprint("other-challenge")},
        )

        self.assertFalse(_code_is_bound_to_our_challenge(oauth))
        self.assertFalse(_code_is_bound_to_our_challenge(_oauth_context()))

    async def test_browser_verifier_is_matched_against_the_issuer_challenge_only(self) -> None:
        # 存储里可能同时躺着两条 challenge（我们改写前后的），只有签发 code 的那条算数。
        issuer_verifier = "issuer-verifier-" + "a" * 40
        other_verifier = "other-verifier-" + "b" * 40
        oauth = _oauth_context(
            challenges=[code_challenge_for(other_verifier), code_challenge_for(issuer_verifier)],
            issuer_fp=_secret_fingerprint(code_challenge_for(issuer_verifier)),
        )

        with patch(
            "services.register.playwright_register._browser_storage_snapshot",
            AsyncMock(return_value=(_verifier_candidates([issuer_verifier, other_verifier]), "")),
        ):
            from services.register.playwright_register import _resolve_browser_verifier

            await _resolve_browser_verifier(MagicMock(), oauth, lambda _: None, stage="兑换前")

        self.assertEqual(oauth["verifier"], issuer_verifier)
        self.assertEqual(oauth["verifier_source"], "浏览器")

    async def test_browser_verifier_probe_falls_back_to_our_own_when_nothing_matches(self) -> None:
        oauth = _oauth_context(
            challenges=[code_challenge_for("browser-verifier")],
            issuer_fp=_secret_fingerprint(code_challenge_for("browser-verifier")),
        )
        events: list[str] = []

        with patch(
            "services.register.playwright_register._browser_storage_snapshot",
            AsyncMock(return_value=(["unrelated-value-" + "c" * 40], "")),
        ):
            from services.register.playwright_register import _resolve_browser_verifier

            await _resolve_browser_verifier(MagicMock(), oauth, events.append, stage="兑换前")

        self.assertEqual(oauth["verifier"], "")
        self.assertEqual(oauth["verifier_source"], "自带")
        self.assertTrue(any("没有匹配 challenge 的 verifier" in event for event in events))

    async def test_issuer_fingerprint_is_taken_from_the_unrouted_request(self) -> None:
        # 跳转目标进不了 handler，只有 page.on("request") 看得到它——签发方的指纹只能从那儿拿，
        # 顺带还得把它的 client_id 抄下来（上游换过 client 的话，写死那个兑换必然 invalid_grant）。
        page = FakePage()
        verifier = "browser-verifier-" + "d" * 40
        challenge = code_challenge_for(verifier)
        oauth = _oauth_context()
        events: list[str] = []
        await _install_oauth_routes(page, 1, "our-challenge", [], events, oauth)

        page.listeners["request"](
            SimpleNamespace(
                url=f"https://auth.openai.com/oauth/authorize"
                f"?client_id=app_browser&code_challenge={challenge}&code_challenge_method=S256"
            )
        )

        # issuer_fp 是抓到 code 那一刻快照的（见 test_captured_code_remembers_the_issuing_challenge），
        # 但 challenge 与 client_id 必须当场记下——它们来自一条 handler 根本看不到的请求。
        self.assertEqual(oauth["challenges"], [challenge])
        self.assertEqual(oauth["client_id"], "app_browser")
        self.assertEqual(oauth["rewritten_fps"], set())  # 没经过 handler，不算改写

    async def test_our_own_rewritten_challenge_is_not_mistaken_for_the_browser_s(self) -> None:
        # 改写后的请求也会走 page.on("request")，但那条 challenge 是我们生成的，
        # 当成「浏览器的」会让兑换去用一份根本不存在的 verifier。
        page = FakePage()
        oauth = _oauth_context()
        await _install_oauth_routes(page, 1, "our-challenge", [], [], oauth)

        page.listeners["request"](
            SimpleNamespace(
                url="https://auth.openai.com/oauth/authorize?code_challenge=our-challenge"
            )
        )

        self.assertEqual(oauth["issuer_fp"], "")
        self.assertEqual(oauth["challenges"], [])

    async def test_captured_code_remembers_the_issuing_challenge(self) -> None:
        page = FakePage()
        verifier = "browser-verifier-" + "e" * 40
        challenge = code_challenge_for(verifier)
        oauth = _oauth_context()
        captured: list[str] = []
        await _install_oauth_routes(page, 1, "our-challenge", captured, [], oauth)
        page.listeners["request"](
            SimpleNamespace(url=f"https://auth.openai.com/x?code_challenge={challenge}")
        )

        route = FakeRoute(
            "https://platform.openai.com/auth/callback?code=one-time-code&state=s",
            frame=frame_at("https://platform.openai.com/signup"),
        )
        await page.routes[OAUTH_ROUTE_PATTERN](route)

        self.assertEqual(captured, ["one-time-code"])
        self.assertEqual(oauth["issuer_fp"], _secret_fingerprint(challenge))

    async def test_observed_authorize_params_are_used_for_the_exchange(self) -> None:
        session = MagicMock()
        page = MagicMock()
        context = MagicMock()
        context.cookies = AsyncMock(return_value=[])

        with patch(
            "services.register.playwright_register.curl_requests.Session", return_value=session
        ), patch(
            "services.register.playwright_register.request_platform_oauth_token",
            return_value={"access_token": "access"},
        ) as exchange_token:
            await _exchange_oauth_token_in_browser(
                page, context, 7, "one-time-code", "browser-verifier", "",
                client_id="app_browser", redirect_uri="https://platform.openai.com/auth/callback",
                device_id="dev-1",
            )

        exchange_token.assert_called_once_with(
            session,
            "one-time-code",
            "browser-verifier",
            client_id="app_browser",
            redirect_uri="https://platform.openai.com/auth/callback",
            device_id="dev-1",
        )

    async def test_identity_verification_page_is_detected_before_the_timeout(self) -> None:
        page = MagicMock()
        page.url = "https://auth.openai.com/verify-your-identity"

        self.assertEqual(await _wait_for_signup_step(page, []), "identity")
        page.locator.assert_not_called()

    async def test_identity_verification_is_continued_once_then_reported_as_risk_control(self) -> None:
        button = MagicMock()
        button.first = button
        button.wait_for = AsyncMock()
        button.click = AsyncMock()
        page = MagicMock()
        page.url = "https://auth.openai.com/verify-your-identity"
        page.locator.return_value = button

        await _continue_identity_verification(page, 8)
        button.click.assert_awaited_once()

        button.wait_for = AsyncMock(side_effect=Exception("no button"))
        with self.assertRaises(RuntimeError) as raised:
            await _continue_identity_verification(page, 8)
        self.assertIn("verify-your-identity", str(raised.exception))
        self.assertIn("风控", str(raised.exception))

    async def test_identity_gate_does_not_loop_forever_in_the_state_machine(self) -> None:
        page = SimpleNamespace()
        with patch(
            "services.register.playwright_register._wait_for_signup_step",
            AsyncMock(side_effect=["profile", "identity", "identity"]),
        ), patch(
            "services.register.playwright_register._fill_profile", AsyncMock()
        ), patch(
            "services.register.playwright_register._continue_identity_verification", AsyncMock()
        ) as continue_identity:
            with self.assertRaises(RuntimeError) as raised:
                await _run_signup_state_machine(
                    page, 9, "Secret123!", "Test User", "25", {"address": "test@example.com"}, []
                )

        self.assertIn("verify-your-identity", str(raised.exception))
        continue_identity.assert_awaited_once()

    async def test_signup_flow_requests_are_left_alone(self) -> None:
        page = FakePage()
        await _install_oauth_routes(page, 1, "our-challenge", [])
        handler = page.routes[OAUTH_ROUTE_PATTERN]

        route = FakeRoute(
            "https://auth.openai.com/api/accounts/email-otp/validate",
            method="POST",
            resource_type="fetch",
            frame=frame_at("https://auth.openai.com/email-verification"),
        )
        await handler(route)

        self.assertIsNone(route.aborted)
        self.assertEqual(route.continued_url, route.request.url)

    async def test_requests_without_a_frame_are_never_blocked(self) -> None:
        page = FakePage()
        await _install_oauth_routes(page, 1, "our-challenge", [])
        handler = page.routes[OAUTH_ROUTE_PATTERN]

        # APIRequestContext(page.request) 发出的请求不走 page.route；
        # 万一走到这里，也必须放行，否则我们自己的兑换请求会被自己拦掉。
        route = FakeRoute(
            f"https://auth.openai.com{OAUTH_TOKEN_PATH}",
            method="POST",
            resource_type="fetch",
        )
        await handler(route)

        self.assertIsNone(route.aborted)
        self.assertEqual(route.continued_url, route.request.url)

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
        page = MagicMock()
        page.locator.side_effect = [password_input, submit_button]
        page.inner_text = AsyncMock(return_value=LIVE_PASSWORD_REJECTION_BODY)

        with patch(
            "services.register.playwright_register._return_to_otp_signup", AsyncMock()
        ) as return_to_otp:
            password_set = await _submit_password(page, 4, "Secret123456!")

        self.assertFalse(password_set)
        password_input.fill.assert_awaited_once_with("Secret123456!")
        submit_button.click.assert_awaited_once()
        return_to_otp.assert_awaited_once()

    async def test_password_rejection_matches_ignoring_punctuation_and_case(self) -> None:
        # live 正文结尾没有句号。旧实现是 text="...again."（精确匹配），一个标点之差
        # 让兜底永远不触发，线上表现是干等 15 秒后报"提交密码后页面未继续"。
        page = MagicMock()
        page.inner_text = AsyncMock(return_value=LIVE_PASSWORD_REJECTION_BODY)
        self.assertTrue(await _page_rejects_password_signup(page))

        page.inner_text = AsyncMock(
            return_value="Failed to create account. Please try again."
        )
        self.assertTrue(await _page_rejects_password_signup(page))

    async def test_normal_page_is_not_mistaken_for_a_rejection(self) -> None:
        page = MagicMock()
        page.inner_text = AsyncMock(
            return_value=(
                "Create a password You’ll use this password to log in to ChatGPT and other "
                "OpenAI products Email address Edit Password Your password must contain: "
                "At least 12 characters . Complete. Continue"
            )
        )
        self.assertFalse(await _page_rejects_password_signup(page))

    async def test_unreadable_page_is_not_treated_as_a_rejection(self) -> None:
        page = MagicMock()
        page.inner_text = AsyncMock(side_effect=Exception("frame detached"))
        self.assertFalse(await _page_rejects_password_signup(page))

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
        exchange_token.assert_called_once_with(
            session,
            "one-time-code",
            "code-verifier",
            client_id=platform_oauth_client_id,
            redirect_uri=platform_oauth_redirect_uri,
            device_id="",
        )
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
        page.wait_for_timeout = AsyncMock()
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

    async def test_transient_exchange_failure_is_retried_on_the_same_session(self) -> None:
        session = MagicMock()
        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        context = MagicMock()
        context.cookies = AsyncMock(return_value=[])

        with patch(
            "services.register.playwright_register.curl_requests.Session", return_value=session
        ), patch(
            "services.register.playwright_register.request_platform_oauth_token",
            side_effect=[
                RuntimeError("OAuth token 交换失败: status=503, body=upstream"),
                {"access_token": "access"},
            ],
        ) as exchange_token:
            tokens = await _exchange_oauth_token_in_browser(
                page, context, 5, "one-time-code", "code-verifier", ""
            )

        self.assertEqual(tokens["access_token"], "access")
        self.assertEqual(exchange_token.call_count, 2)
        page.wait_for_timeout.assert_awaited_once()
        session.close.assert_called_once()

    async def test_invalid_grant_is_not_retried(self) -> None:
        session = MagicMock()
        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        context = MagicMock()
        context.cookies = AsyncMock(return_value=[])
        context.request.post = AsyncMock(return_value=SimpleNamespace(
            status=400,
            text=AsyncMock(return_value=json.dumps({"error": "invalid_grant"})),
            headers={},
        ))

        with patch(
            "services.register.playwright_register.curl_requests.Session", return_value=session
        ), patch(
            "services.register.playwright_register.request_platform_oauth_token",
            side_effect=RuntimeError(
                'OAuth token 交换失败: status=400, body={ "error": "invalid_grant" }'
            ),
        ) as exchange_token:
            with self.assertRaises(RuntimeError) as raised:
                await _exchange_oauth_token_in_browser(
                    page, context, 5, "one-time-code", "code-verifier", ""
                )

        # 400 是确定性的：重试没有意义，也不能把失败原因盖掉。
        self.assertEqual(exchange_token.call_count, 1)
        self.assertIn("invalid_grant", str(raised.exception))
        page.wait_for_timeout.assert_not_awaited()
        session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
