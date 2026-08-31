import importlib.machinery
import importlib.util
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("remote_open", str(ROOT / "remote_open.py"))
spec = importlib.util.spec_from_loader(loader.name, loader)
remote_open = importlib.util.module_from_spec(spec)
loader.exec_module(remote_open)


class ProtocolTests(unittest.TestCase):
    def test_message_round_trip(self):
        stream = io.BytesIO()
        message = {"operation": "edit", "target": "one", "paths": ["/tmp/a b", "/tmp/é"]}
        remote_open.write_message(stream, message)
        stream.seek(0)
        self.assertEqual(remote_open.read_message(stream), message)

    def test_request_operation_is_an_enum(self):
        operation, _, _ = remote_open.validate_request(
            {"operation": "edit", "target": "one", "paths": ["/tmp/file"]}
        )
        self.assertIs(operation, remote_open.Operation.EDIT)

    def test_rejects_extra_fields(self):
        with self.assertRaises(remote_open.RemoteOpenError):
            remote_open.validate_request(
                {"operation": "edit", "target": "one", "paths": ["/tmp/a"], "command": "sh"}
            )

    def test_diff_needs_two_paths(self):
        with self.assertRaises(remote_open.RemoteOpenError):
            remote_open.validate_request({"operation": "diff", "target": "one", "paths": ["/tmp/a"]})

    def test_url_quotes_special_characters(self):
        urls = remote_open.remote_urls("sftp://user@host/", ["/tmp/a b#c"])
        self.assertEqual(urls, ["sftp://user@host/tmp/a%20b%23c"])

    def test_edit_path_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new file"
            normalized = remote_open.normalized_path(str(path))
            self.assertEqual(normalized, str(path))
            self.assertFalse(path.exists())

    def test_config(self):
        value = {
            "targets": {
                "one": {"url_prefix": "sftp://user@host"},
                "two": {"url_prefix": "sftp://user@other"},
            },
            "commands": {"edit": ["kate"], "diff": ["kompare", "-c"]},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(remote_open.load_config(path), value)

    def test_target_selects_url_prefix(self):
        value = {
            "targets": {
                "one": {"url_prefix": "sftp://user@one"},
                "two": {"url_prefix": "sftp://user@two"},
            },
            "commands": {"edit": ["kate"], "diff": ["kompare", "-c"]},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with mock.patch.object(remote_open.subprocess, "Popen") as popen:
                popen.return_value.wait.return_value = 0
                remote_open.launch(
                    path,
                    {"operation": "edit", "target": "two", "paths": ["/tmp/a b"]},
                )
            popen.assert_called_once_with(
                ["kate", "sftp://user@two/tmp/a%20b"],
                start_new_session=True,
            )

    def test_bridge_exchange(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            socket_path = directory / "bridge.sock"
            config_path = directory / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "targets": {"one": {"url_prefix": "sftp://user@host"}},
                        "commands": {"edit": ["/bin/true"], "diff": ["/bin/true"]},
                    }
                ),
                encoding="utf-8",
            )
            bridge = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "remote_open.py"),
                    "bridge",
                    "--socket",
                    str(socket_path),
                    "--config",
                    str(config_path),
                ],
                preexec_fn=lambda: os.umask(0),
            )
            try:
                for _ in range(100):
                    if socket_path.exists():
                        break
                    time.sleep(0.01)
                else:
                    self.fail("bridge socket was not created")

                self.assertEqual(stat.S_IMODE(socket_path.stat().st_mode), 0o600)

                environment = os.environ.copy()
                environment["REMOTE_OPEN_SOCKET"] = str(socket_path)
                environment["REMOTE_OPEN_TARGET"] = "one"
                result = subprocess.run(
                    [sys.executable, str(ROOT / "remote_open.py"), "edit", "new file"],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            finally:
                bridge.terminate()
                bridge.wait(timeout=5)
            self.assertFalse(socket_path.exists())

    def test_bridge_uses_private_umask_while_binding(self):
        observed_umask = None

        def inspect_umask(*_args):
            nonlocal observed_umask
            observed_umask = os.umask(0o177)
            os.umask(observed_umask)
            raise PermissionError("stop after inspecting umask")

        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "bridge.sock"
            with mock.patch.object(remote_open, "BridgeServer", side_effect=inspect_umask):
                with self.assertRaises(PermissionError):
                    remote_open.bridge(socket_path, Path(directory) / "config.json")

        self.assertEqual(observed_umask, 0o177)

    def test_unknown_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "targets": {"known": {"url_prefix": "sftp://user@host"}},
                        "commands": {"edit": ["/bin/true"], "diff": ["/bin/true"]},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(remote_open.RemoteOpenError, "unknown target"):
                remote_open.launch(
                    path,
                    {"operation": "edit", "target": "unknown", "paths": ["/tmp/file"]},
                )

    def test_client_socket_has_timeout(self):
        client = mock.MagicMock()
        client.connect.side_effect = socket.timeout
        with mock.patch.object(remote_open.socket, "socket", return_value=client):
            with self.assertRaisesRegex(remote_open.RemoteOpenError, "timed out"):
                remote_open.send_request(
                    Path("/tmp/bridge.sock"),
                    "one",
                    remote_open.Operation.EDIT,
                    ["/tmp/file"],
                )
        client.settimeout.assert_called_once_with(remote_open.SOCKET_TIMEOUT)
        client.close.assert_called_once_with()

    def test_main_reports_bridge_operating_system_error(self):
        error = PermissionError("cannot bind socket")
        with mock.patch.object(remote_open, "parser") as parser:
            parser.return_value.parse_args.return_value = mock.Mock(
                operation="bridge",
                socket=Path("/tmp/bridge.sock"),
                config=Path("/tmp/config.json"),
            )
            with mock.patch.object(remote_open, "bridge", side_effect=error):
                stderr = io.StringIO()
                with mock.patch("sys.stderr", stderr):
                    self.assertEqual(remote_open.main(), 1)
        self.assertIn("remote-open: operating system error: cannot bind socket", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
