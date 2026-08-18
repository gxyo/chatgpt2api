from __future__ import annotations

import os
import tempfile
import unittest
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

    def _write_image(self, rel: str, size: int, mtime: float) -> None:
        path = self.images_dir.joinpath(*Path(rel).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        os.utime(path, (mtime, mtime))

    def test_cleanup_targets_image_usage_even_when_disk_has_free_space(self):
        items = [
            {"path": "2026/01/01/old.png", "size": 2 * image_service.MEGABYTE, "created_at": "2026-01-01 00:00:00"},
            {"path": "2026/01/02/middle.jpg", "size": 2 * image_service.MEGABYTE, "created_at": "2026-01-02 00:00:00"},
            {"path": "2026/01/03/new.webp", "size": 2 * image_service.MEGABYTE, "created_at": "2026-01-03 00:00:00"},
        ]
        for index, item in enumerate(items):
            self._write_image(str(item["path"]), int(item["size"]), index + 1)
        fake_storage = FakeImageStorage(self.images_dir, items)

        with self._config_patch(), mock.patch.object(image_service, "image_storage_service", fake_storage), mock.patch.object(image_service, "remove_tags"):
            result = image_service.delete_to_target(3)

        self.assertEqual(result["removed"], 2)
        self.assertEqual(result["freed_bytes"], 4 * image_service.MEGABYTE)
        self.assertEqual(result["current_size_mb"], 2)
        self.assertTrue(result["done"])
        self.assertEqual(fake_storage.deleted, ["2026/01/01/old.png", "2026/01/02/middle.jpg"])

    def test_cleanup_dry_run_reports_candidates_without_deleting(self):
        rel = "2026/01/01/image.jpeg"
        size = 2 * image_service.MEGABYTE
        self._write_image(rel, size, 1)
        fake_storage = FakeImageStorage(self.images_dir, [{"path": rel, "size": size, "created_at": "2026-01-01 00:00:00"}])

        with self._config_patch(), mock.patch.object(image_service, "image_storage_service", fake_storage), mock.patch.object(image_service, "remove_tags"):
            result = image_service.delete_to_target(1, dry_run=True)

        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["freed_mb"], 2)
        self.assertTrue((self.images_dir / rel).is_file())
        self.assertEqual(fake_storage.deleted, [])

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


if __name__ == "__main__":
    unittest.main()
