import unittest

from services.register import openai_register


class FakeResponse:
    def __init__(self, text="", headers=None):
        self.text = text
        self.headers = headers or {}


class RegisterCloudflareDetectionTests(unittest.TestCase):
    def test_cloudflare_server_header_alone_is_not_challenge(self):
        response = FakeResponse(
            text='{"error":{"code":"invalid_request","message":"bad request"}}',
            headers={"server": "cloudflare", "content-type": "application/json"},
        )

        self.assertFalse(openai_register._is_cloudflare_challenge(response))

    def test_cloudflare_challenge_headers_are_detected(self):
        response = FakeResponse(headers={"server": "cloudflare", "cf-mitigated": "challenge"})

        self.assertTrue(openai_register._is_cloudflare_challenge(response))

    def test_cloudflare_challenge_body_is_detected(self):
        response = FakeResponse(text="<html><title>Just a moment...</title></html>")

        self.assertTrue(openai_register._is_cloudflare_challenge(response))

    def test_proxy_scheme_detection(self):
        self.assertFalse(openai_register._is_socks_proxy(""))
        self.assertFalse(openai_register._is_socks_proxy("http://127.0.0.1:7890"))
        self.assertTrue(openai_register._is_socks_proxy("socks5://127.0.0.1:7890"))


if __name__ == "__main__":
    unittest.main()
