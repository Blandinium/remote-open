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
        message = {"operation": "edit", "target": "one", "paths": ["/tmp/a b", "/tmp/é"], "wait": False}
        remote_open.write_message(stream, message)
        stream.seek(0)
        self.assertEqual(remote_open.read_message(stream), message)

    def test_request_operation_is_an_enum(self):
        operation, _, _, _, _ = remote_open.validate_request(
            {"operation": "edit", "target": "one", "paths": ["/tmp/file"], "wait": False}
        )
        self.assertIs(operation, remote_open.Operation.EDIT)

    def test_rejects_extra_fields(self):
        with self.assertRaises(remote_open.RemoteOpenError):
            remote_open.validate_request(
                {"operation": "edit", "target": "one", "paths": ["/tmp/a"], "wait": False, "command": "sh"}
            )

    def test_diff_needs_two_paths(self):
        with self.assertRaises(remote_open.RemoteOpenError):
            remote_open.validate_request(
                {"operation": "diff", "target": "one", "paths": ["/tmp/a"], "wait": False}
            )

    def test_diff_parser_accepts_git_external_diff_arguments(self):
        values = ["file.txt", "/tmp/old", "abc", "100644", "/tmp/new", "def", "100644"]
        arguments = remote_open.parser().parse_args(["diff", "--wait", *values])
        self.assertEqual(arguments.paths, values)
        self.assertTrue(arguments.wait)

    def test_diff_rejects_other_argument_counts(self):
        with self.assertRaisesRegex(remote_open.RemoteOpenError, "Git's seven arguments"):
            remote_open.send_request(
                Path("/tmp/bridge.sock"),
                "one",
                remote_open.Operation.DIFF,
                ["one", "two", "three"],
            )

    def test_git_external_diff_uses_old_and_new_files(self):
        with tempfile.TemporaryDirectory() as directory:
            old = Path(directory) / "old"
            new = Path(directory) / "new"
            old.touch()
            new.touch()
            values = ["file.txt", str(old), "abc", "100644", str(new), "def", "100644"]
            client = mock.MagicMock()
            client.makefile.return_value = io.BytesIO()
            with mock.patch.object(remote_open.socket, "socket", return_value=client):
                with mock.patch.object(remote_open, "write_message") as write:
                    with mock.patch.object(remote_open, "read_message", return_value={"ok": True}):
                        remote_open.send_request(
                            Path("/tmp/bridge.sock"), "one", remote_open.Operation.DIFF, values
                        )
        write.assert_called_once_with(
            client.makefile.return_value,
            {
                "operation": remote_open.Operation.DIFF,
                "target": "one",
                "paths": [str(old), str(new)],
                "wait": False,
            },
        )

    def test_git_external_diff_wait_sends_wait_and_disables_socket_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            old = Path(directory) / "old"
            new = Path(directory) / "new"
            old.touch()
            new.touch()
            values = ["file.txt", str(old), "abc", "100644", str(new), "def", "100644"]
            arguments = mock.Mock(operation="diff", paths=values, wait=True)
            client = mock.MagicMock()
            client.makefile.return_value = io.BytesIO()
            with mock.patch.object(remote_open, "parser") as parser:
                parser.return_value.parse_args.return_value = arguments
                with mock.patch.dict(
                    os.environ,
                    {
                        "REMOTE_OPEN_SOCKET": "/tmp/bridge.sock",
                        "REMOTE_OPEN_TARGET": "one",
                    },
                ):
                    with mock.patch.object(remote_open.socket, "socket", return_value=client):
                        with mock.patch.object(remote_open, "write_message") as write:
                            with mock.patch.object(
                                remote_open, "read_message", return_value={"ok": True}
                            ):
                                self.assertEqual(remote_open.main(), 0)

        write.assert_called_once_with(
            client.makefile.return_value,
            {
                "operation": remote_open.Operation.DIFF,
                "target": "one",
                "paths": [str(old), str(new)],
                "wait": True,
            },
        )
        self.assertEqual(
            client.settimeout.call_args_list,
            [mock.call(remote_open.SOCKET_TIMEOUT), mock.call(None)],
        )

    def test_open_needs_one_path(self):
        with self.assertRaises(remote_open.RemoteOpenError):
            remote_open.validate_request(
                {
                    "operation": "open",
                    "target": "one",
                    "paths": ["/tmp/a", "/tmp/b"],
                    "mime_type": "text/plain",
                    "wait": False,
                }
            )

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
            "commands": {
                "open": ["kioclient", "exec", "{url}", "{mime_type}"],
                "edit": ["kate"],
                "edit_wait": ["kate", "--block"],
                "diff": ["kompare", "-c"],
                "diff_wait": ["kompare", "-c"],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(remote_open.load_config(path), value)

    def test_config_allows_omitting_commands(self):
        value = {
            "targets": {"one": {"url_prefix": "sftp://user@host"}},
            "commands": {"edit": ["kate"]},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(remote_open.load_config(path), value)

    def test_unconfigured_operation_is_rejected(self):
        value = {
            "targets": {"one": {"url_prefix": "sftp://user@host"}},
            "commands": {"edit": ["kate"]},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(
                remote_open.RemoteOpenError,
                "diff is not configured on the bridge",
            ):
                remote_open.launch(
                    path,
                    {
                        "operation": "diff",
                        "target": "one",
                        "paths": ["/tmp/old", "/tmp/new"],
                        "wait": False,
                    },
                )

    def test_target_selects_url_prefix(self):
        value = {
            "targets": {
                "one": {"url_prefix": "sftp://user@one"},
                "two": {"url_prefix": "sftp://user@two"},
            },
            "commands": {
                "open": ["kioclient", "exec", "{url}", "{mime_type}"],
                "edit": ["kate"],
                "edit_wait": ["kate", "--block"],
                "diff": ["kompare", "-c"],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with mock.patch.object(remote_open.subprocess, "Popen") as popen:
                popen.return_value.wait.return_value = 0
                remote_open.launch(
                    path,
                    {"operation": "edit", "target": "two", "paths": ["/tmp/a b"], "wait": False},
                )
            popen.assert_called_once_with(
                ["kate", "sftp://user@two/tmp/a%20b"],
                start_new_session=True,
            )

    def test_open_substitutes_url_and_mime_type(self):
        value = {
            "targets": {"one": {"url_prefix": "sftp://user@host"}},
            "commands": {
                "open": ["kioclient", "exec", "{url}", "{mime_type}"],
                "edit": ["kate"],
                "edit_wait": ["kate", "--block"],
                "diff": ["kompare"],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with mock.patch.object(remote_open.subprocess, "Popen") as popen:
                popen.return_value.wait.return_value = 0
                remote_open.launch(
                    path,
                    {
                        "operation": "open",
                        "target": "one",
                        "paths": ["/tmp/a b.pdf"],
                        "mime_type": "application/pdf",
                        "wait": False,
                    },
                )
            popen.assert_called_once_with(
                ["kioclient", "exec", "sftp://user@host/tmp/a%20b.pdf", "application/pdf"],
                start_new_session=True,
            )

    def test_waiting_edit_uses_blocking_command(self):
        value = {
            "targets": {"one": {"url_prefix": "sftp://user@host"}},
            "commands": {
                "open": ["kioclient", "exec", "{url}"],
                "edit": ["kate"],
                "edit_wait": ["kate", "--block"],
                "diff": ["kompare"],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with mock.patch.object(remote_open.subprocess, "Popen") as popen:
                popen.return_value.wait.return_value = 0
                remote_open.launch(
                    path,
                    {
                        "operation": "edit",
                        "target": "one",
                        "paths": ["/tmp/COMMIT_EDITMSG"],
                        "wait": True,
                    },
                )
            popen.assert_called_once_with(
                ["kate", "--block", "sftp://user@host/tmp/COMMIT_EDITMSG"],
                start_new_session=True,
            )
            popen.return_value.wait.assert_called_once_with()

    def test_waiting_diff_uses_blocking_command(self):
        value = {
            "targets": {"one": {"url_prefix": "sftp://user@host"}},
            "commands": {"diff_wait": ["kompare", "-c"]},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with mock.patch.object(remote_open.subprocess, "Popen") as popen:
                popen.return_value.wait.return_value = 0
                remote_open.launch(
                    path,
                    {
                        "operation": "diff",
                        "target": "one",
                        "paths": ["/tmp/old", "/tmp/new"],
                        "wait": True,
                    },
                )
            popen.assert_called_once_with(
                ["kompare", "-c", "sftp://user@host/tmp/old", "sftp://user@host/tmp/new"],
                start_new_session=True,
            )
            popen.return_value.wait.assert_called_once_with()

    def test_waiting_diff_reports_nonzero_exit_status(self):
        value = {
            "targets": {"one": {"url_prefix": "sftp://user@host"}},
            "commands": {"diff_wait": ["kompare", "-c"]},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with mock.patch.object(remote_open.subprocess, "Popen") as popen:
                popen.return_value.wait.return_value = 2
                with self.assertRaisesRegex(
                    remote_open.RemoteOpenError,
                    "kompare exited with status 2",
                ):
                    remote_open.launch(
                        path,
                        {
                            "operation": "diff",
                            "target": "one",
                            "paths": ["/tmp/old", "/tmp/new"],
                            "wait": True,
                        },
                    )

            popen.assert_called_once_with(
                ["kompare", "-c", "sftp://user@host/tmp/old", "sftp://user@host/tmp/new"],
                start_new_session=True,
            )
            popen.return_value.wait.assert_called_once_with()

    def test_detects_mime_type_with_file(self):
        result = subprocess.CompletedProcess([], 0, "image/png\n", "")
        with mock.patch.object(remote_open.subprocess, "run", return_value=result) as run:
            self.assertEqual(remote_open.detect_mime_type("/tmp/image"), "image/png")
        run.assert_called_once_with(
            ["file", "--brief", "--mime-type", "--", "/tmp/image"],
            capture_output=True,
            text=True,
            check=False,
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
                        "commands": {
                            "open": ["/bin/true", "{url}"],
                            "edit": ["/bin/true"],
                            "edit_wait": ["/bin/true"],
                            "diff": ["/bin/true"],
                        },
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
                        "commands": {
                            "open": ["/bin/true", "{url}"],
                            "edit": ["/bin/true"],
                            "edit_wait": ["/bin/true"],
                            "diff": ["/bin/true"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(remote_open.RemoteOpenError, "unknown target"):
                remote_open.launch(
                    path,
                    {
                        "operation": "edit",
                        "target": "unknown",
                        "paths": ["/tmp/file"],
                        "wait": False,
                    },
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

    def test_main_sends_one_open_request_per_path(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.png"
            second = Path(directory) / "second.pdf"
            first.touch()
            second.touch()
            arguments = mock.Mock(operation="open", paths=[str(first), str(second)])
            with mock.patch.object(remote_open, "parser") as parser:
                parser.return_value.parse_args.return_value = arguments
                with mock.patch.dict(
                    os.environ,
                    {
                        "REMOTE_OPEN_SOCKET": "/tmp/bridge.sock",
                        "REMOTE_OPEN_TARGET": "one",
                    },
                ):
                    with mock.patch.object(
                        remote_open,
                        "detect_mime_type",
                        side_effect=["image/png", "application/pdf"],
                    ):
                        with mock.patch.object(remote_open, "send_request", return_value=0) as send:
                            self.assertEqual(remote_open.main(), 0)
        self.assertEqual(
            send.call_args_list,
            [
                mock.call(
                    Path("/tmp/bridge.sock"),
                    "one",
                    remote_open.Operation.OPEN,
                    [str(first)],
                    "image/png",
                ),
                mock.call(
                    Path("/tmp/bridge.sock"),
                    "one",
                    remote_open.Operation.OPEN,
                    [str(second)],
                    "application/pdf",
                ),
            ],
        )

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
