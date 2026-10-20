"""Validate fixture unlock protocol without any real OS vault or credentials."""
from contextlib import ExitStack
import importlib.util
import os
from pathlib import Path
import socket
import stat
import struct
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

SPEC = importlib.util.spec_from_file_location(
    "native_test_vault_under_test",
    Path(__file__).resolve().parents[1] / "scripts/native_test_vault.py")
vault = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vault)


class NativeTestVaultTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="radar-vault-test-"))).resolve()
        (self.root / "control").mkdir()
        self.stack.enter_context(patch.dict(os.environ, {
            "GITHUB_ACTIONS": "true", "RUNNER_OS": "Linux",
            "RADAR_TEST_VAULT_ROOT": str(self.root)}))
        self.stack.enter_context(patch.object(vault.sys, "platform", "linux"))
        self.stack.enter_context(patch.object(vault.os, "geteuid", return_value=123, create=True))
        self.info = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=123)
        self.original_lstat = Path.lstat
        def lstat(path, *args, **kwargs):
            if path == self.root / "control/control":
                return self.info
            return self.original_lstat(path, *args, **kwargs)
        self.stack.enter_context(patch.object(Path, "lstat", lstat))
        self.transport = MagicMock()
        self.transport.__enter__.return_value = self.transport
        self.transport.recv.side_effect = [b"\0\0", b"\0\x08", b"\0", b"\0\0\0"]
        self.stack.enter_context(patch.object(socket, "AF_UNIX", 1, create=True))
        self.factory = self.stack.enter_context(patch.object(socket, "socket", return_value=self.transport))
        self.daemon = self.stack.enter_context(patch.object(vault, "_run",
            side_effect=AssertionError("Unlock must preserve the existing daemon and collection")))
        self.database = Mock()
        self.collection = Mock()
        self.collection.is_locked.return_value = False
        storage = ModuleType("secretstorage")
        storage.dbus_init = Mock(return_value=self.database)
        collection_module = ModuleType("secretstorage.collection")
        collection_module.Collection = Mock(return_value=self.collection)
        self.stack.enter_context(patch.dict(sys.modules, {
            "secretstorage": storage, "secretstorage.collection": collection_module}))

    def test_unlock_existing_fixture_accepts_fragmented_reply_without_replacement(self):
        vault.control("unlock")
        self.daemon.assert_not_called()
        self.transport.connect.assert_called_once_with(str(self.root / "control/control"))
        password = vault.PASSWORD.encode()
        self.assertEqual(b"".join(call.args[0] for call in self.transport.sendall.call_args_list),
                         b"\0" + struct.pack(">III", 12 + len(password), 1, len(password)) + password)
        self.collection.is_locked.assert_called_once_with()
        self.database.close.assert_called_once_with()
        self.assertTrue(all(0 < call.args[0] <= 20 for call in self.transport.settimeout.call_args_list))

    def test_unlock_rejects_denied_truncated_oversized_and_still_locked_replies(self):
        for reply in ([struct.pack(">I", 8), struct.pack(">I", 1)],
                      [struct.pack(">I", 8), b""],
                      [struct.pack(">I", 4096)]):
            with self.subTest(reply=reply):
                self.transport.recv.side_effect = reply
                with self.assertRaises(ValueError):
                    vault.control("unlock")
        self.transport.recv.side_effect = [struct.pack(">I", 8), struct.pack(">I", 0)]
        self.collection.is_locked.return_value = True
        with self.assertRaisesRegex(ValueError, "did not unlock"):
            vault.control("unlock")
        self.daemon.assert_not_called()
        self.database.close.assert_called_once_with()

    def test_unlock_refuses_non_socket_and_wrong_owner_before_connecting(self):
        for mode, owner in ((stat.S_IFREG, 123), (stat.S_IFLNK, 123), (stat.S_IFSOCK, 456)):
            with self.subTest(mode=mode, owner=owner):
                self.info.st_mode, self.info.st_uid = mode, owner
                with self.assertRaises(ValueError):
                    vault.control("unlock")
        self.factory.assert_not_called()
        self.daemon.assert_not_called()
