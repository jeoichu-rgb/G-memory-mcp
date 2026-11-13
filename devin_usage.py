"""Read Devin account quota from the official interactive ``/usage`` view.

Devin exposes account-level daily/weekly quota only inside its interactive
terminal UI.  ACP ``usage_update`` is session/context usage and must never be
presented as account quota.  This module keeps one idle Devin terminal alive,
asks it for ``/usage`` on demand, and normalizes the rendered values.
"""

from __future__ import annotations

import getpass
import os
import re
import select
import struct
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class DevinUsageProbeError(RuntimeError):
    """The interactive Devin usage view could not be read or parsed."""


_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_DAILY_RE = re.compile(
    r"Daily[^\r\n]*?(\d+(?:\.\d+)?)\s*%\s*used\s*[·•]?\s*resets\s+([^\r\n]+)",
    re.IGNORECASE,
)
_WEEKLY_RE = re.compile(
    r"Weekly[^\r\n]*?(\d+(?:\.\d+)?)\s*%\s*used\s*[·•]?\s*resets\s+([^\r\n]+)",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(
    r"(?:(\d+)\s*d(?:ays?)?)?\s*"
    r"(?:(\d+)\s*h(?:ours?)?)?\s*"
    r"(?:(\d+)\s*m(?:in(?:utes?)?)?)?",
    re.IGNORECASE,
)


def strip_terminal_controls(value: str) -> str:
    """Remove ANSI/OSC controls while preserving redraw text order."""
    value = _OSC_RE.sub("", value)
    value = _CSI_RE.sub("", value)
    value = value.replace("\r", "\n")
    while "\x08" in value:
        value = re.sub(r".?\x08", "", value)
    return value


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_reset_time(raw: str, captured_at: datetime) -> datetime:
    """Parse Devin's relative daily reset or absolute UTC weekly reset."""
    text = " ".join(raw.strip().split())
    if text.lower().startswith("in "):
        duration = text[3:].strip()
        match = _DURATION_RE.fullmatch(duration)
        if not match or not any(match.groups()):
            raise DevinUsageProbeError(f"unrecognized Devin reset duration: {raw!r}")
        days, hours, minutes = (int(value or 0) for value in match.groups())
        return captured_at + timedelta(days=days, hours=hours, minutes=minutes)

    utc_text = re.sub(r"\s*\(UTC\)\s*$", "", text, flags=re.IGNORECASE)
    formats = (
        "%b %d, %I:%M %p",
        "%b %d, %Y, %I:%M %p",
        "%b %d %I:%M %p",
        "%b %d %Y %I:%M %p",
    )
    for fmt in formats:
        try:
            if "%Y" in fmt:
                parsed = datetime.strptime(utc_text, fmt)
            else:
                parsed = datetime.strptime(
                    f"{captured_at.year} {utc_text}", f"%Y {fmt}"
                )
        except ValueError:
            continue
        if "%Y" not in fmt:
            if parsed.replace(tzinfo=timezone.utc) < captured_at - timedelta(days=1):
                parsed = parsed.replace(year=captured_at.year + 1)
        return parsed.replace(tzinfo=timezone.utc)
    raise DevinUsageProbeError(f"unrecognized Devin reset timestamp: {raw!r}")


def parse_devin_usage(value: str, captured_at: datetime | None = None) -> dict[str, Any]:
    """Normalize the Daily and Weekly rows rendered by Devin ``/usage``."""
    captured_at = captured_at or datetime.now(timezone.utc)
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    text = strip_terminal_controls(value)
    daily_matches = list(_DAILY_RE.finditer(text))
    weekly_matches = list(_WEEKLY_RE.finditer(text))
    if not daily_matches or not weekly_matches:
        raise DevinUsageProbeError("Devin /usage did not contain Daily and Weekly rows")

    windows = []
    for key, match in (("daily", daily_matches[-1]), ("weekly", weekly_matches[-1])):
        reset_text = match.group(2).strip()
        reset_at = parse_reset_time(reset_text, captured_at)
        windows.append({
            "key": key,
            "utilization": float(match.group(1)),
            "resets_at": _iso_z(reset_at),
            "reset_text": reset_text,
        })
    return {
        "ok": True,
        "source": "devin-cli-/usage",
        "captured_at": _iso_z(captured_at),
        "windows": windows,
    }


class DevinUsageProbe:
    """Persistent hidden terminal used only for Devin account quota reads."""

    def __init__(
        self,
        *,
        binary: str,
        model: str,
        cwd: str,
        run_as_user: str = "erik",
        home: str | None = None,
        startup_timeout: float = 20.0,
        response_timeout: float = 20.0,
    ):
        self.binary = binary
        self.model = model
        self.cwd = cwd
        self.run_as_user = run_as_user
        self.home = home or f"/home/{run_as_user}"
        self.startup_timeout = startup_timeout
        self.response_timeout = response_timeout
        self._proc: subprocess.Popen[bytes] | None = None
        self._master_fd: int | None = None
        self._lock = threading.Lock()
        self._account_info: dict[str, str] | None = None

    def _run_as_user(self, command: list[str]) -> list[str]:
        if getpass.getuser() != self.run_as_user:
            return ["sudo", "-u", self.run_as_user, "-H", *command]
        return command

    def _command(self) -> list[str]:
        return self._run_as_user([self.binary, "--model", self.model])

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update({
            "HOME": self.home,
            "USER": self.run_as_user,
            "LOGNAME": self.run_as_user,
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "COLUMNS": "160",
            "LINES": "50",
        })
        return env

    @property
    def running(self) -> bool:
        return bool(self._proc and self._proc.poll() is None and self._master_fd is not None)

    def _close_process(self) -> None:
        proc, master_fd = self._proc, self._master_fd
        self._proc = None
        self._master_fd = None
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)

    def close(self) -> None:
        with self._lock:
            if self.running:
                try:
                    os.write(self._master_fd, b"/exit\r")
                    self._proc.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self._close_process()

    def _start(self) -> None:
        import pty

        self._close_process()
        master_fd, slave_fd = pty.openpty()
        try:
            import fcntl
            import termios

            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))
            proc = subprocess.Popen(
                self._command(),
                cwd=self.cwd,
                env=self._environment(),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
        finally:
            os.close(slave_fd)
        self._proc = proc
        self._master_fd = master_fd
        self._drain_until_quiet(self.startup_timeout)
        if not self.running:
            raise DevinUsageProbeError("Devin CLI exited while starting usage probe")

    def _read_chunk(self, timeout: float) -> bytes:
        if self._master_fd is None:
            return b""
        ready, _, _ = select.select([self._master_fd], [], [], max(0.0, timeout))
        if not ready:
            return b""
        try:
            return os.read(self._master_fd, 65536)
        except OSError:
            return b""

    def _drain_until_quiet(self, timeout: float, quiet: float = 0.6) -> bytes:
        deadline = time.monotonic() + timeout
        last_data = time.monotonic()
        output = bytearray()
        while time.monotonic() < deadline:
            chunk = self._read_chunk(min(0.2, deadline - time.monotonic()))
            if chunk:
                output.extend(chunk)
                last_data = time.monotonic()
                continue
            if output and time.monotonic() - last_data >= quiet:
                break
            if self._proc and self._proc.poll() is not None:
                break
        return bytes(output)

    def _query_once(self) -> dict[str, Any]:
        if not self.running:
            self._start()
        self._drain_until_quiet(0.3, quiet=0.1)
        os.write(self._master_fd, b"/usage\r")
        deadline = time.monotonic() + self.response_timeout
        output = bytearray()
        while time.monotonic() < deadline:
            chunk = self._read_chunk(min(0.5, deadline - time.monotonic()))
            if chunk:
                output.extend(chunk)
                decoded = output.decode("utf-8", "replace")
                try:
                    return parse_devin_usage(decoded, datetime.now(timezone.utc))
                except DevinUsageProbeError:
                    pass
            if self._proc and self._proc.poll() is not None:
                break
        excerpt = strip_terminal_controls(output.decode("utf-8", "replace"))[-1000:]
        raise DevinUsageProbeError(f"timed out reading Devin /usage: {excerpt!r}")

    def _read_account_info(self) -> dict[str, str]:
        if self._account_info is not None:
            return dict(self._account_info)
        info: dict[str, str] = {}
        try:
            completed = subprocess.run(
                self._run_as_user([self.binary, "auth", "status"]),
                cwd=self.cwd,
                env=self._environment(),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            output = completed.stdout + "\n" + completed.stderr
            for key in ("Tier", "Plan"):
                match = re.search(rf"^\s*{key}:\s*(.+?)\s*$", output, re.MULTILINE)
                if match:
                    info[key.casefold()] = match.group(1).strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
        self._account_info = info
        return dict(info)

    def query(self) -> dict[str, Any]:
        with self._lock:
            try:
                data = self._query_once()
            except (OSError, DevinUsageProbeError):
                self._close_process()
                data = self._query_once()
            account = self._read_account_info()
            data["tier"] = account.get("tier", "")
            data["plan"] = account.get("plan") or account.get("tier") or "Devin"
            return data
