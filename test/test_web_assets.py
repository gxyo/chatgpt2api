from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from api import support
from api.app import create_app


class WebAssetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.web_dist = Path(self.tmp.name)
        self.old_web_dist = support.WEB_DIST_DIR
        support.WEB_DIST_DIR = self.web_dist
        support._WEB_ASSET_CACHE.clear()
        self.addCleanup(self._restore_web_dist)

    def _restore_web_dist(self) -> None:
        support.WEB_DIST_DIR = self.old_web_dist
        support._WEB_ASSET_CACHE.clear()

    def test_html_assets_are_served_without_client_cache(self) -> None:
        index = self.web_dist / "index.html"
        index.write_text("<!doctype html><html></html>", encoding="utf-8")

        asset = support.resolve_web_asset("")
        self.assertEqual(asset, index)

        response = support.web_asset_response(asset, "")
        self.assertEqual(response.headers["cache-control"], "no-store, max-age=0")
        self.assertIn("text/html", response.headers["content-type"])

    def test_next_static_assets_are_immutable(self) -> None:
        asset_path = self.web_dist / "_next" / "static" / "chunk.js"
        asset_path.parent.mkdir(parents=True)
        asset_path.write_text("console.log('ok')", encoding="utf-8")

        asset = support.resolve_web_asset("_next/static/chunk.js")
        self.assertEqual(asset, asset_path)

        response = support.web_asset_response(asset, "_next/static/chunk.js")
        self.assertEqual(response.headers["cache-control"], "public, max-age=31536000, immutable")
        self.assertIn("application/javascript", response.headers["content-type"])

    def test_head_requests_skip_the_body(self) -> None:
        index = self.web_dist / "index.html"
        index.write_text("<!doctype html><html></html>", encoding="utf-8")

        asset = support.resolve_web_asset("")
        response = support.web_asset_response(asset, "", include_body=False)

        self.assertEqual(response.body, b"")
        self.assertEqual(response.headers["cache-control"], "no-store, max-age=0")
        self.assertIn("text/html", response.headers["content-type"])

    def test_web_assets_route_answers_head(self) -> None:
        # Next.js 的 output: "export" 预取会先对目标路由发 HEAD 探测，
        # 非 2xx 会让它把该路由的缓存判为不可用，从而彻底跳过预取。
        index = self.web_dist / "index.html"
        index.write_text("<!doctype html><html></html>", encoding="utf-8")
        (self.web_dist / "stats").mkdir()
        (self.web_dist / "stats" / "index.html").write_text("<!doctype html><html></html>", encoding="utf-8")

        self.assertIn(
            "HEAD",
            {method for route in create_app().routes for method in (route.methods or set())},
        )

    def test_path_traversal_is_not_resolved(self) -> None:
        outside = self.web_dist.parent / "outside.txt"
        outside.write_text("secret", encoding="utf-8")

        self.assertIsNone(support.resolve_web_asset("../outside.txt"))


if __name__ == "__main__":
    unittest.main()
