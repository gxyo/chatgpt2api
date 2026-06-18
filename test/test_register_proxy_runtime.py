import unittest
from unittest.mock import patch

from services.register import openai_register


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None, url="https://auth.openai.com/test"):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.url = url

    def json(self):
        return {}


class FakeCookieJar:
    def __init__(self):
        self.items = []

    def set(self, name, value, domain=None):
        self.items.append({"name": name, "value": value, "domain": domain})


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.cookies = FakeCookieJar()
        self.proxies = {}
        self.verify = True
        self.closed = False
        self.mounts = []

    def mount(self, prefix, adapter):
        self.mounts.append((prefix, adapter))

    def close(self):
        self.closed = True


class RegisterProxyRuntimeTests(unittest.TestCase):
    def test_create_session_uses_direct_http_proxy_without_proxy_runtime(self):
        created = []

        def fake_session_factory():
            session = FakeSession()
            created.append(session)
            return session

        with patch.object(openai_register.std_requests, "Session", side_effect=fake_session_factory):
            session = openai_register.create_session("http://legacy-register.example:8080")

        self.assertIs(session, created[0])
        self.assertFalse(session.verify)
        self.assertEqual(
            session.proxies,
            {
                "http": "http://legacy-register.example:8080",
                "https": "http://legacy-register.example:8080",
            },
        )
        self.assertEqual([prefix for prefix, _adapter in session.mounts], ["http://", "https://"])

    def test_cloudflare_authorize_keeps_old_clear_register_error(self):
        cf_response = FakeResponse(
            status_code=403,
            text="<html><title>Just a moment...</title></html>",
            headers={"server": "cloudflare", "content-type": "text/html"},
            url="https://auth.openai.com/api/accounts/authorize",
        )

        with patch.object(openai_register, "create_session", return_value=FakeSession()), patch.object(
            openai_register,
            "request_with_local_retry",
            return_value=(cf_response, ""),
        ):
            registrar = openai_register.PlatformRegistrar(proxy="http://legacy-register.example:8080")
            with self.assertRaisesRegex(RuntimeError, "被 Cloudflare 拦截，请更换 IP 重试"):
                registrar._platform_authorize("user@example.com", 1)

    def test_openai_html_behind_cloudflare_is_not_treated_as_challenge(self):
        response = FakeResponse(
            status_code=200,
            text="""
            <!DOCTYPE html><html lang=\"en-US\"><head>
            <title>Create a password - OpenAI</title>
            </head><body>OpenAI account page</body></html>
            """,
            headers={"server": "cloudflare", "content-type": "text/html; charset=utf-8"},
            url="https://auth.openai.com/create-account/password",
        )

        self.assertFalse(openai_register._is_cloudflare_challenge(response))


if __name__ == "__main__":
    unittest.main()
