from __future__ import annotations

import base64
import time
import unittest
from unittest import mock

from services.image_storage_service import IMAGE_EXTENSIONS, image_storage_service
from services.openai_backend_api import ImageRequestTimeoutError
from services.protocol import conversation
from services.protocol.conversation import ConversationRequest, format_image_result
from utils import helper

GIF_BYTES = b"GIF89a\x01\x00\x01\x00\x00\x00\x00;"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class FakeRescueBackend:
    """最小 backend：只实现取图/下载，deadline 故意设成已经过期。"""

    def __init__(self, image_bytes: bytes, urls: list[str] | None = None) -> None:
        self.deadline = time.monotonic() - 1
        self.image_bytes = image_bytes
        self.urls = urls if urls is not None else ["https://files.test/out.gif"]
        self.resolve_calls: list[tuple[str, list[str], list[str], float | None]] = []
        self.download_calls = 0

    def mark_image_phase(self, name: str) -> None:
        return None

    def resolve_conversation_image_urls(
            self,
            conversation_id: str,
            file_ids: list[str],
            sediment_ids: list[str],
            poll_timeout_secs: float | None = None,
    ) -> list[str]:
        # 救援路径必须先把 deadline 放宽，否则这里的请求全部会被判超时。
        assert self.deadline > time.monotonic()
        self.resolve_calls.append((conversation_id, list(file_ids), list(sediment_ids), poll_timeout_secs))
        return list(self.urls)

    def download_image_bytes(self, urls: list[str]) -> list[bytes]:
        self.download_calls += 1
        return [self.image_bytes for _ in urls]


def _asset_event() -> dict:
    file_id = "file_00000000918081f5b3a5dbc9a4a25671"
    return {
        "type": "conversation.event",
        "text": "",
        "conversation_id": "conv-1",
        "file_ids": [file_id],
        "sediment_ids": [file_id],
        "blocked": False,
        "tool_invoked": True,
        "turn_use_case": "image gen",
        "model_slug": "gpt-image-2",
        "app_ui_marker": False,
        "raw": {"o": "add"},
    }


def _request() -> ConversationRequest:
    return ConversationRequest(
        model="gpt-image-2",
        prompt="生成一个眨眼睛的 gif",
        images=["data:image/png;base64,AAAA"],
        response_format="b64_json",
        message_as_error=True,
    )


class DeadlineRescueTests(unittest.TestCase):
    def _stream(self, backend: FakeRescueBackend, events):
        def fake_events(_backend, **_kwargs):
            yield from events
            raise ImageRequestTimeoutError("image request deadline exceeded")

        with mock.patch.object(conversation, "conversation_events", fake_events), \
                mock.patch.object(conversation, "save_image_bytes", lambda data, base_url=None: "https://files.test/saved.gif"):
            return list(conversation.stream_image_outputs(backend, _request()))

    def test_asset_survives_deadline_and_returns_gif(self) -> None:
        backend = FakeRescueBackend(GIF_BYTES)

        outputs = self._stream(backend, [_asset_event()])

        self.assertEqual(backend.download_calls, 1)
        self.assertEqual(backend.resolve_calls[0][0], "conv-1")
        self.assertEqual(backend.resolve_calls[0][2], ["file_00000000918081f5b3a5dbc9a4a25671"])
        result = next(item for item in outputs if item.kind == "result")
        self.assertEqual(base64.b64decode(result.data[0]["b64_json"]), GIF_BYTES)
        # 取图宽限已经写回 backend 实例（但外层请求预算不受影响）。
        self.assertGreater(backend.deadline, time.monotonic())

    def test_progress_event_marks_image_asset(self) -> None:
        backend = FakeRescueBackend(GIF_BYTES)

        outputs = self._stream(backend, [_asset_event()])

        progress = next(item for item in outputs if item.kind == "progress")
        self.assertTrue(progress.has_image_asset)

    def test_no_asset_reraises_timeout_without_resolving(self) -> None:
        backend = FakeRescueBackend(GIF_BYTES)
        event = _asset_event()
        event["file_ids"] = []
        event["sediment_ids"] = []

        with self.assertRaises(ImageRequestTimeoutError):
            self._stream(backend, [event])

        self.assertEqual(backend.resolve_calls, [])

    def test_empty_resolve_reraises_original_timeout(self) -> None:
        backend = FakeRescueBackend(GIF_BYTES, urls=[])

        with self.assertRaises(ImageRequestTimeoutError) as raised:
            self._stream(backend, [_asset_event()])

        self.assertEqual(str(raised.exception), "image request deadline exceeded")

    def test_download_failure_reraises_original_timeout(self) -> None:
        backend = FakeRescueBackend(GIF_BYTES)
        backend.download_image_bytes = mock.Mock(side_effect=RuntimeError("download failed"))

        with self.assertRaises(ImageRequestTimeoutError) as raised:
            self._stream(backend, [_asset_event()])

        self.assertEqual(str(raised.exception), "image request deadline exceeded")


class GifFormatTests(unittest.TestCase):
    def _format(self, image_bytes: bytes, response_format: str = "b64_json") -> tuple[list[bytes], dict]:
        saved: list[bytes] = []

        def fake_save(data: bytes, base_url: str | None = None) -> str:
            saved.append(data)
            return "https://files.test/saved.bin"

        with mock.patch.object(conversation, "save_image_bytes", fake_save):
            result = format_image_result(
                [{"b64_json": base64.b64encode(image_bytes).decode("ascii")}],
                "prompt",
                response_format,
            )
        return saved, result

    def test_format_image_result_keeps_gif_bytes(self) -> None:
        saved, result = self._format(GIF_BYTES)

        self.assertEqual(saved, [GIF_BYTES])
        self.assertEqual(base64.b64decode(result["data"][0]["b64_json"]), GIF_BYTES)
        self.assertNotIn("_postprocess", result)

    def test_format_image_result_keeps_png_bytes(self) -> None:
        saved, result = self._format(PNG_BYTES, "url")

        self.assertEqual(saved, [PNG_BYTES])
        self.assertEqual(result["data"][0]["url"], "https://files.test/saved.bin")

    def test_storage_uses_gif_extension_by_magic(self) -> None:
        self.assertTrue(image_storage_service.make_relative_path(GIF_BYTES).endswith(".gif"))
        self.assertTrue(image_storage_service.make_relative_path(PNG_BYTES).endswith(".png"))

    def test_gif_is_a_supported_storage_extension(self) -> None:
        self.assertTrue(helper.is_gif_bytes(GIF_BYTES))
        self.assertFalse(helper.is_gif_bytes(PNG_BYTES))
        self.assertIn(".gif", IMAGE_EXTENSIONS)


class SsePayloadOrderTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, lines: list[bytes]) -> None:
            self.lines = lines

        def iter_lines(self):
            return iter(self.lines)

    def test_payload_is_delivered_before_deadline_check_raises(self) -> None:
        def on_line() -> None:
            raise ImageRequestTimeoutError("deadline")

        stream = helper.iter_sse_payloads(self.FakeResponse([b'data: {"n": 1}', b""]), on_line=on_line)

        self.assertEqual(next(stream), '{"n": 1}')
        with self.assertRaises(ImageRequestTimeoutError):
            next(stream)

    def test_heartbeat_and_blank_lines_still_checked(self) -> None:
        calls: list[int] = []
        stream = helper.iter_sse_payloads(
            self.FakeResponse([b": ping", b"", b'data: {"n": 1}']),
            on_line=lambda: calls.append(1),
        )
        self.assertEqual(list(stream), ['{"n": 1}'])
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
