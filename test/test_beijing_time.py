from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.image_storage_service import _now_iso as image_storage_now
from services.image_task_service import _now_iso as image_task_now
from services.log_service import LogService
from services.editable_file_task_service import _now_iso as editable_file_now
from utils.beijing_time import (
    BEIJING_TZ,
    TIME_FORMAT,
    beijing_from_timestamp,
    beijing_now,
    beijing_now_text,
    beijing_text_from_timestamp,
    to_beijing,
    utc_now_iso,
)


def _seconds_apart(left: datetime, right: datetime) -> float:
    return abs((left - right).total_seconds())


class BeijingTimeHelperTests(unittest.TestCase):
    def test_now_is_utc_plus_eight(self):
        expected = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)
        self.assertLess(_seconds_apart(beijing_now(), expected), 2)

    def test_now_text_uses_the_shared_format(self):
        text = beijing_now_text()
        self.assertEqual(len(text), 19)
        # 能被按项目统一格式解析回来，说明补零与分隔符都一致。
        self.assertEqual(datetime.strptime(text, TIME_FORMAT).strftime(TIME_FORMAT), text)

    def test_timestamp_is_rendered_as_beijing_wall_clock(self):
        # 2026-10-01 12:00:00 UTC == 2026-10-01 20:00:00 北京时间
        self.assertEqual(beijing_text_from_timestamp(1790856000), "2026-10-01 20:00:00")
        self.assertEqual(beijing_from_timestamp(1790856000).hour, 20)

    def test_utc_now_iso_keeps_an_explicit_offset(self):
        parsed = datetime.fromisoformat(utc_now_iso())
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset(), timedelta(0))
        self.assertLess(_seconds_apart(parsed.replace(tzinfo=None), datetime.now(timezone.utc).replace(tzinfo=None)), 2)

    def test_to_beijing_converts_aware_and_passes_naive_through(self):
        self.assertEqual(to_beijing(datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)), datetime(2026, 10, 1, 20, 0))
        naive = datetime(2026, 10, 1, 12, 0)
        self.assertIs(to_beijing(naive), naive)
        self.assertEqual(BEIJING_TZ.utcoffset(None), timedelta(hours=8))


class LogServiceBeijingTimeTests(unittest.TestCase):
    def test_log_entries_are_written_in_beijing_time(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = LogService(Path(tmp_dir) / "logs.jsonl")
            service.add("call", "调用", {"endpoint": "/v1/images/generations"})

            item = json.loads(service.path.read_text(encoding="utf-8").strip().splitlines()[0])

        logged = datetime.strptime(item["time"], TIME_FORMAT)
        self.assertLess(_seconds_apart(logged, beijing_now()), 5)


@unittest.skipUnless(hasattr(time, "tzset"), "模拟宿主机时区需要 time.tzset（POSIX）")
class HostTimezoneIndependenceTests(unittest.TestCase):
    """把宿主机时区切到非北京时间，确认各处写出/扫出的时间仍是北京时间。

    裸 ``datetime.now()`` 在 TZ=Asia/Shanghai 的容器里恰好等于北京时间，
    只有换一个时区才能暴露出来——这正是这些用例存在的意义。
    """

    def setUp(self):
        self._original_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        # 自检：宿主机确实已经被切走了，否则下面的断言会失去意义。
        offset_hours = _seconds_apart(beijing_now(), datetime.now()) / 3600
        if offset_hours < 10:
            self.skipTest("无法把宿主机时区切到 America/New_York")

    def tearDown(self):
        if self._original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._original_tz
        time.tzset()

    def test_service_timestamps_are_beijing_not_local(self):
        for producer in (image_task_now, image_storage_now, editable_file_now):
            with self.subTest(producer=producer.__module__):
                produced = datetime.strptime(producer(), TIME_FORMAT)
                self.assertLess(
                    _seconds_apart(produced, beijing_now()), 5,
                    f"{producer.__module__} 写出的不是北京时间",
                )

    def test_log_entry_time_is_beijing_not_local(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = LogService(Path(tmp_dir) / "logs.jsonl")
            service.add("call", "调用")
            item = json.loads(service.path.read_text(encoding="utf-8").strip().splitlines()[0])

        logged = datetime.strptime(item["time"], TIME_FORMAT)
        self.assertLess(_seconds_apart(logged, beijing_now()), 5)
        self.assertGreater(_seconds_apart(logged, datetime.now()), 10)


if __name__ == "__main__":
    unittest.main()
