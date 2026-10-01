from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from services import image_service


class FakeImageStorage:
    def __init__(self, images_dir: Path, items: list[dict[str, object]]):
        self.images_dir = images_dir
        self.items = {str(item["path"]): dict(item) for item in items}
        self.deleted: list[str] = []

    def list_items(self, _base_url: str) -> list[dict[str, object]]:
        return list(self.items.values())

    def delete(self, rel: str) -> bool:
        path = self.images_dir.joinpath(*Path(rel).parts)
        if path.is_file():
            path.unlink()
        self.items.pop(rel, None)
        self.deleted.append(rel)
        return True


class ImageCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.images_dir = self.root / "images"
        self.thumbnails_dir = self.root / "image_thumbnails"
        self.images_dir.mkdir()
        self.thumbnails_dir.mkdir()

    def _config_patch(self):
        config = mock.Mock()
        config.images_dir = self.images_dir
        config.image_thumbnails_dir = self.thumbnails_dir
        return mock.patch.object(image_service, "config", config)

    def test_storage_stats_counts_indexed_remote_images(self):
        size = 3 * image_service.MEGABYTE
        fake_storage = FakeImageStorage(
            self.images_dir,
            [{"path": "2026/01/01/remote.png", "size": size, "created_at": "2026-01-01 00:00:00"}],
        )

        with self._config_patch(), mock.patch.object(image_service, "image_storage_service", fake_storage):
            result = image_service.storage_stats()

        self.assertEqual(result["image_count"], 1)
        self.assertEqual(result["image_size_bytes"], size)
        self.assertEqual(result["image_size_mb"], 3)

    def test_scheduled_cleanup_initializes_the_next_run_without_deleting(self):
        cleanup_config = mock.Mock()
        cleanup_config.images_dir = self.images_dir
        cleanup_config.image_cleanup_schedule_configured = True
        cleanup_config.image_cleanup_interval_days = 3
        cleanup_config.image_cleanup_time = "09:30"
        now = datetime(2026, 9, 28, 8, 15)

        with mock.patch.object(image_service, "config", cleanup_config), mock.patch.object(image_service, "delete_images") as delete_mock:
            result = image_service.run_scheduled_image_cleanup_if_due(now)

        state = json.loads((self.root / "image_cleanup_state.json").read_text(encoding="utf-8"))
        self.assertIsNone(result)
        delete_mock.assert_not_called()
        self.assertEqual(state["schedule"], "3:09:30@+08:00")
        self.assertEqual(state["next_run_at"], "2026-09-28T09:30:00")

    def test_scheduled_cleanup_deletes_all_images_through_the_run_date(self):
        cleanup_config = mock.Mock()
        cleanup_config.images_dir = self.images_dir
        cleanup_config.image_cleanup_schedule_configured = True
        cleanup_config.image_cleanup_interval_days = 3
        cleanup_config.image_cleanup_time = "09:30"
        state_file = self.root / "image_cleanup_state.json"
        state_file.write_text(json.dumps({
            "schedule": "3:09:30@+08:00",
            "next_run_at": "2026-09-28T09:30:00",
        }), encoding="utf-8")
        now = datetime(2026, 9, 28, 9, 31)

        with mock.patch.object(image_service, "config", cleanup_config), mock.patch.object(
            image_service,
            "delete_images",
            return_value={"removed": 4},
        ) as delete_mock:
            result = image_service.run_scheduled_image_cleanup_if_due(now)

        delete_mock.assert_called_once_with(end_date="2026-09-28", all_matching=True)
        self.assertEqual(result, {"removed": 4})
        state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["last_run_at"], "2026-09-28T09:31:00")
        self.assertEqual(state["next_run_at"], "2026-10-01T09:30:00")

    def test_scheduled_cleanup_uses_beijing_time_regardless_of_host_timezone(self):
        expected = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)
        beijing_now = image_service._beijing_now()
        self.assertLess(abs((beijing_now - expected).total_seconds()), 5)
        self.assertIsNone(beijing_now.tzinfo)

    def test_scheduled_cleanup_rebaselines_state_written_before_timezone_fix(self):
        cleanup_config = mock.Mock()
        cleanup_config.images_dir = self.images_dir
        cleanup_config.image_cleanup_schedule_configured = True
        cleanup_config.image_cleanup_interval_days = 3
        cleanup_config.image_cleanup_time = "09:30"
        state_file = self.root / "image_cleanup_state.json"
        state_file.write_text(json.dumps({
            "schedule": "3:09:30",
            "next_run_at": "2026-09-28T09:30:00",
        }), encoding="utf-8")

        with mock.patch.object(image_service, "config", cleanup_config), mock.patch.object(image_service, "delete_images") as delete_mock:
            result = image_service.run_scheduled_image_cleanup_if_due(datetime(2026, 9, 28, 9, 31))

        self.assertIsNone(result)
        delete_mock.assert_not_called()
        state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["schedule"], "3:09:30@+08:00")
        self.assertEqual(state["next_run_at"], "2026-10-01T09:30:00")


if __name__ == "__main__":
    unittest.main()
