#!/usr/bin/env python3
"""Open remote files in workstation applications."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import threading
from enum import Enum
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote


VERSION = "0.4.0"
MAX_MESSAGE_SIZE = 1024 * 1024
SOCKET_TIMEOUT = 10.0
HEADER = struct.Struct("!I")


class RemoteOpenError(Exception):
    """A user-facing error."""


class Operation(str, Enum):
    """An operation supported by the remote-open protocol."""

    OPEN = "open"
    EDIT = "edit"
    DIFF = "diff"


def read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise RemoteOpenError("connection closed early")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_message(stream: BinaryIO) -> dict:
    size = HEADER.unpack(read_exact(stream, HEADER.size))[0]
    if size > MAX_MESSAGE_SIZE:
        raise RemoteOpenError("message is too large")
    try:
        message = json.loads(read_exact(stream, size))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RemoteOpenError("invalid JSON message") from error
    if not isinstance(message, dict):
        raise RemoteOpenError("message must be an object")
    return message


def write_message(stream: BinaryIO, message: dict) -> None:
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
    if len(data) > MAX_MESSAGE_SIZE:
        raise RemoteOpenError("message is too large")
    stream.write(HEADER.pack(len(data)))
    stream.write(data)
    stream.flush()


def normalized_path(value: str) -> str:
    path = os.path.abspath(os.path.expanduser(value))
    try:
        path.encode("utf-8")
    except UnicodeEncodeError as error:
        raise RemoteOpenError(f"path is not valid UTF-8: {value!r}") from error
    return path


def validate_request(message: dict) -> tuple[Operation, str, list[str], str | None, bool]:
    if not isinstance(message, dict):
        raise RemoteOpenError("message must be an object")
    try:
        operation = Operation(message["operation"])
    except (KeyError, TypeError, ValueError) as error:
        raise RemoteOpenError("operation must be open, edit, or diff") from error
    expected_fields = {"operation", "target", "paths", "wait"}
    if operation is Operation.OPEN:
        expected_fields.add("mime_type")
    if set(message) != expected_fields:
        fields = ", ".join(sorted(expected_fields))
        raise RemoteOpenError(f"message must contain only {fields}")
    target = message["target"]
    paths = message["paths"]
    mime_type = message.get("mime_type")
    wait = message["wait"]
    if not isinstance(wait, bool):
        raise RemoteOpenError("wait must be a boolean")
    if wait and operation is not Operation.EDIT:
        raise RemoteOpenError("wait is supported only for edit")
    if not isinstance(target, str) or not target:
        raise RemoteOpenError("target must be a non-empty string")
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise RemoteOpenError("paths must be a list of strings")
    if operation is Operation.OPEN and len(paths) != 1:
        raise RemoteOpenError("open needs one path")
    if operation is Operation.OPEN and (
        not isinstance(mime_type, str)
        or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", mime_type)
    ):
        raise RemoteOpenError("open needs a valid MIME type")
    if operation is Operation.EDIT and not paths:
        raise RemoteOpenError("edit needs at least one path")
    if operation is Operation.DIFF and len(paths) != 2:
        raise RemoteOpenError("diff needs two paths")
    if any(not path.startswith("/") for path in paths):
        raise RemoteOpenError("paths must be absolute")
    return operation, target, paths, mime_type, wait


def load_config(path: Path) -> dict:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RemoteOpenError(f"config not found: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise RemoteOpenError(f"cannot read config: {error}") from error

    if not isinstance(config, dict):
        raise RemoteOpenError("config must be an object")
    if set(config) != {"targets", "commands"}:
        raise RemoteOpenError("config must contain only targets and commands")
    targets = config["targets"]
    commands = config["commands"]
    if not isinstance(targets, dict) or not targets:
        raise RemoteOpenError("targets must be a non-empty object")
    for alias, target in targets.items():
        if not isinstance(alias, str) or not alias:
            raise RemoteOpenError("target aliases must be non-empty strings")
        if not isinstance(target, dict) or set(target) != {"url_prefix"}:
            raise RemoteOpenError(f"targets.{alias} must contain only url_prefix")
        prefix = target["url_prefix"]
        if not isinstance(prefix, str) or not prefix.startswith(("sftp://", "ssh://")):
            raise RemoteOpenError(f"targets.{alias}.url_prefix must use sftp:// or ssh://")
    command_names = {"open", "edit", "edit_wait", "diff"}
    if not isinstance(commands, dict) or not commands:
        raise RemoteOpenError("commands must be a non-empty object")
    if not set(commands).issubset(command_names):
        raise RemoteOpenError("commands may contain only open, edit, edit_wait, and diff")
    for operation, command in commands.items():
        if not isinstance(command, list) or not command:
            raise RemoteOpenError(f"commands.{operation} must be a non-empty list")
        if not all(isinstance(item, str) and item for item in command):
            raise RemoteOpenError(f"commands.{operation} has an invalid argument")
    if "open" in commands:
        open_command = commands["open"]
        if sum(item.count("{url}") for item in open_command) != 1:
            raise RemoteOpenError("commands.open must contain {url} once")
        if sum(item.count("{mime_type}") for item in open_command) > 1:
            raise RemoteOpenError("commands.open may contain {mime_type} once")
    return config


def remote_urls(prefix: str, paths: list[str]) -> list[str]:
    return [f"{prefix.rstrip('/')}{quote(path, safe='/')}" for path in paths]


def reap(process: subprocess.Popen) -> None:
    process.wait()


def launch(config_path: Path, message: dict) -> None:
    operation, target, paths, mime_type, wait = validate_request(message)
    config = load_config(config_path)
    try:
        prefix = config["targets"][target]["url_prefix"]
    except KeyError as error:
        raise RemoteOpenError(f"unknown target: {target}") from error
    urls = remote_urls(prefix, paths)
    command_name = "edit_wait" if wait else operation.value
    if command_name not in config["commands"]:
        display_name = "edit --wait" if wait else operation.value
        raise RemoteOpenError(f"{display_name} is not configured on the bridge")
    if operation is Operation.OPEN:
        assert mime_type is not None
        command = [
            item.replace("{url}", urls[0]).replace("{mime_type}", mime_type)
            for item in config["commands"][command_name]
        ]
    else:
        command = [*config["commands"][command_name], *urls]
    try:
        process = subprocess.Popen(command, start_new_session=True)
    except OSError as error:
        raise RemoteOpenError(f"cannot start {command[0]}: {error}") from error
    if wait:
        status = process.wait()
        if status != 0:
            raise RemoteOpenError(f"{command[0]} exited with status {status}")
    else:
        threading.Thread(target=reap, args=(process,), daemon=True).start()


class RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            self.connection.settimeout(SOCKET_TIMEOUT)
            message = read_message(self.rfile)
            launch(self.server.config_path, message)  # type: ignore[attr-defined]
            response = {"ok": True}
        except socket.timeout:
            response = {"ok": False, "error": "socket operation timed out"}
        except Exception as error:
            if not isinstance(error, RemoteOpenError):
                print(f"remote-open: internal error: {error}", file=sys.stderr)
            response = {"ok": False, "error": str(error)}
        try:
            write_message(self.wfile, response)
        except (OSError, RemoteOpenError):
            pass


class BridgeServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, socket_path: Path, config_path: Path):
        self.config_path = config_path
        super().__init__(str(socket_path), RequestHandler)


def remove_stale_socket(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise RemoteOpenError(f"refusing to replace non-socket: {path}")
    probe = socket.socket(socket.AF_UNIX)
    try:
        probe.connect(str(path))
    except ConnectionRefusedError:
        path.unlink()
    else:
        raise RemoteOpenError(f"bridge is already running: {path}")
    finally:
        probe.close()


def bridge(socket_path: Path, config_path: Path) -> int:
    socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    remove_stale_socket(socket_path)
    previous_umask = os.umask(0o177)
    try:
        server = BridgeServer(socket_path, config_path)
    finally:
        os.umask(previous_umask)
    os.chmod(socket_path, 0o600)
    inode = socket_path.stat().st_ino

    def stop(_signum: int, _frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            if socket_path.lstat().st_ino == inode:
                socket_path.unlink()
        except FileNotFoundError:
            pass
    return 0


def detect_mime_type(path: str) -> str:
    try:
        result = subprocess.run(
            ["file", "--brief", "--mime-type", "--", path],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise RemoteOpenError(f"cannot determine MIME type: {error}") from error
    mime_type = result.stdout.strip()
    if result.returncode != 0 or not mime_type:
        detail = result.stderr.strip() or f"file exited with status {result.returncode}"
        raise RemoteOpenError(f"cannot determine MIME type for {path}: {detail}")
    if not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", mime_type):
        raise RemoteOpenError(f"file returned an invalid MIME type for {path}: {mime_type!r}")
    return mime_type


def send_request(
    socket_path: Path,
    target: str,
    operation: Operation,
    values: list[str],
    mime_type: str | None = None,
    wait: bool = False,
) -> int:
    paths = [normalized_path(value) for value in values]
    if operation in (Operation.OPEN, Operation.DIFF):
        missing = [path for path in paths if not os.path.exists(path)]
        if missing:
            raise RemoteOpenError(f"{operation.value} path does not exist: {missing[0]}")
    message = {"operation": operation, "target": target, "paths": paths, "wait": wait}
    if operation is Operation.OPEN:
        message["mime_type"] = mime_type
    client = socket.socket(socket.AF_UNIX)
    client.settimeout(SOCKET_TIMEOUT)
    try:
        client.connect(str(socket_path))
        stream = client.makefile("rwb")
        write_message(stream, message)
        if wait:
            client.settimeout(None)
        response = read_message(stream)
    except socket.timeout as error:
        raise RemoteOpenError(f"socket operation timed out: {socket_path}") from error
    except OSError as error:
        raise RemoteOpenError(f"cannot use socket {socket_path}: {error}") from error
    finally:
        client.close()
    if response.get("ok") is not True:
        raise RemoteOpenError(str(response.get("error", "bridge rejected request")))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="remote-open")
    result.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = result.add_subparsers(dest="operation", required=True)

    open_parser = subparsers.add_parser(Operation.OPEN.value, help="open files in their default applications")
    open_parser.add_argument("paths", nargs="+", metavar="PATH")

    edit = subparsers.add_parser(Operation.EDIT.value, help="open files in the configured editor")
    edit.add_argument("--wait", action="store_true", help="wait until the editor finishes")
    edit.add_argument("paths", nargs="+")

    diff = subparsers.add_parser(Operation.DIFF.value, help="compare two existing paths")
    diff.add_argument("paths", nargs=2, metavar=("LEFT", "RIGHT"))

    bridge_parser = subparsers.add_parser("bridge", help="run the workstation bridge")
    bridge_parser.add_argument(
        "--socket",
        type=Path,
        default=Path(
            os.environ.get(
                "REMOTE_OPEN_BRIDGE_SOCKET",
                "%s/remote-open.sock" % os.environ.get("XDG_RUNTIME_DIR", "/tmp"),
            )
        ),
    )
    bridge_parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("REMOTE_OPEN_CONFIG", "~/.config/remote-open/config.json")).expanduser(),
    )
    return result


def main() -> int:
    arguments = parser().parse_args()
    try:
        if arguments.operation == "bridge":
            return bridge(arguments.socket, arguments.config)
        socket_path = Path(os.environ.get("REMOTE_OPEN_SOCKET", "~/.remote-open.socket")).expanduser()
        target = os.environ.get("REMOTE_OPEN_TARGET")
        if not target:
            raise RemoteOpenError("REMOTE_OPEN_TARGET is not set")
        operation = Operation(arguments.operation)
        if operation is Operation.OPEN:
            for value in arguments.paths:
                path = normalized_path(value)
                if not os.path.exists(path):
                    raise RemoteOpenError(f"open path does not exist: {path}")
                send_request(socket_path, target, operation, [path], detect_mime_type(path))
            return 0
        return send_request(
            socket_path,
            target,
            operation,
            arguments.paths,
            wait=getattr(arguments, "wait", False),
        )
    except RemoteOpenError as error:
        print(f"remote-open: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"remote-open: operating system error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
