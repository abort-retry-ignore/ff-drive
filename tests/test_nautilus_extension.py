"""Regression tests for the Nautilus emblem extension's URI handling.

These focus on the security-relevant boundary: turning an arbitrary,
possibly-crafted URI into a path relative to the fixed cache root, without
ever trusting a host, port, user, or path component we did not expect.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT_PATH = os.path.join(ROOT, "nautilus", "fast_fruit_drive_nautilus.py")


def load_extension():
    loader = importlib.machinery.SourceFileLoader("ffd_nautilus_under_test", EXT_PATH)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


try:
    ext = load_extension()
    SKIP_REASON = None
except ImportError as exc:  # pragma: no cover - depends on gi/Nautilus being installed
    ext = None
    SKIP_REASON = str(exc)


@unittest.skipIf(ext is None, f"gi/Nautilus not importable: {SKIP_REASON}")
class RelFromUriTests(unittest.TestCase):
    def test_plain_dav_uri(self):
        self.assertEqual(
            ext._rel_from_uri("dav://ff-drive@iCloud.localhost:8080/Documents/a.txt"),
            "Documents/a.txt",
        )

    def test_root_uri(self):
        self.assertEqual(ext._rel_from_uri("dav://ff-drive@iCloud.localhost:8080/"), "")

    def test_wrong_host_rejected(self):
        self.assertIsNone(ext._rel_from_uri("dav://attacker.example:8080/x"))

    def test_wrong_port_rejected(self):
        self.assertIsNone(ext._rel_from_uri("dav://ff-drive@iCloud.localhost:9999/x"))

    def test_wrong_user_rejected(self):
        self.assertIsNone(ext._rel_from_uri("dav://evil@iCloud.localhost:8080/x"))

    def test_dotdot_traversal_rejected(self):
        self.assertIsNone(ext._rel_from_uri("dav://ff-drive@iCloud.localhost:8080/../../etc/passwd"))

    def test_encoded_dotdot_traversal_rejected(self):
        self.assertIsNone(ext._rel_from_uri("dav://ff-drive@iCloud.localhost:8080/a/%2e%2e/b"))

    def test_gvfs_mount_path_form(self):
        uri = "file:///run/user/1000/gvfs/dav:host=icloud.localhost,port=8080,user=ff-drive/Documents/a.txt"
        self.assertEqual(ext._rel_from_uri(uri), "Documents/a.txt")

    def test_gvfs_wrong_host_rejected(self):
        uri = "file:///run/user/1000/gvfs/dav:host=attacker.example,port=8080/x"
        self.assertIsNone(ext._rel_from_uri(uri))

    def test_gvfs_wrong_port_rejected(self):
        uri = "file:///run/user/1000/gvfs/dav:host=icloud.localhost,port=9999/x"
        self.assertIsNone(ext._rel_from_uri(uri))

    def test_gvfs_wrong_user_rejected(self):
        uri = "file:///run/user/1000/gvfs/dav:host=icloud.localhost,port=8080,user=evil/x"
        self.assertIsNone(ext._rel_from_uri(uri))

    def test_unrelated_local_path_rejected(self):
        self.assertIsNone(ext._rel_from_uri("file:///home/igor/somewhere/else.txt"))

    def test_empty_uri_rejected(self):
        self.assertIsNone(ext._rel_from_uri(""))


@unittest.skipIf(ext is None, f"gi/Nautilus not importable: {SKIP_REASON}")
class ValidRelPathTests(unittest.TestCase):
    def test_rejects_traversal_components(self):
        self.assertFalse(ext._valid_rel_path("a/../b"))
        self.assertFalse(ext._valid_rel_path("../a"))
        self.assertFalse(ext._valid_rel_path("a//b"))

    def test_accepts_normal_paths(self):
        self.assertTrue(ext._valid_rel_path(""))
        self.assertTrue(ext._valid_rel_path("a"))
        self.assertTrue(ext._valid_rel_path("a/b/c"))


if __name__ == "__main__":
    unittest.main()
