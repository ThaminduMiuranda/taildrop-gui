"""Tailscale Taildrop backend utilities for peer discovery, drop parsing, and file transfer."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import threading
import time
from typing import Callable
from urllib.parse import unquote, urlparse
import zipfile

_VALID_TARGET_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*$"
)

_BIDI_AND_FORMAT_CHARS = frozenset(
    {
        "\u061c",
        "\u200e",
        "\u200f",
        "\u2028",
        "\u2029",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
        "\ufeff",
    }
)


def sanitize_display_text(value: object) -> str:
    """Strip control, null, and Unicode bidi-override characters from display strings."""
    if not isinstance(value, str) or not value:
        return ""
    return "".join(
        ch
        for ch in value
        if not (0 <= ord(ch) < 32 or 127 <= ord(ch) <= 159 or ch in _BIDI_AND_FORMAT_CHARS)
    )


def _is_valid_ip(
    value: object,
    addr_cls: type[ipaddress.IPv4Address] | type[ipaddress.IPv6Address],
) -> bool:
    """Return True if value is a strict ASCII IP address matching addr_cls."""
    if not isinstance(value, str) or not value or not value.isascii():
        return False
    if any(ch.isspace() for ch in value):
        return False
    try:
        addr_cls(value)
        return True
    except ValueError:
        return False


def _is_valid_ipv4(value: object) -> bool:
    """Return True if value is a strict dotted-quad IPv4 address string."""
    return _is_valid_ip(value, ipaddress.IPv4Address)


def _is_valid_ipv6(value: object) -> bool:
    """Return True if value is a strict IPv6 address string."""
    return _is_valid_ip(value, ipaddress.IPv6Address)


def _is_valid_target_id(value: object) -> bool:
    """Validate a Tailscale peer identifier (hostname/MagicDNS label or IPv4)."""
    if not isinstance(value, str) or not value or len(value) > 253 or not value.isascii():
        return False
    return bool(_VALID_TARGET_RE.match(value))


@dataclass(frozen=True)
class PeerDevice:
    """Represents a Tailscale peer device capable of receiving Taildrop files."""

    hostname: str
    dns_name: str
    short_name: str
    os_name: str
    online: bool
    ipv4: str

    @property
    def target_id(self) -> str:
        """Identifier passed to `tailscale file cp <file> <target>:`."""
        if _is_valid_target_id(self.short_name):
            return self.short_name
        if _is_valid_ipv4(self.ipv4):
            return self.ipv4
        return ""

    @property
    def is_mac(self) -> bool:
        """Return True if this peer appears to be a macOS device."""
        os_lower = sanitize_display_text(self.os_name).lower()
        host_lower = sanitize_display_text(self.hostname).lower()
        return os_lower in ("macos", "darwin") or "mac" in host_lower

    @property
    def os_icon(self) -> str:
        """Human-friendly icon prefix for dropdown display."""
        os_lower = sanitize_display_text(self.os_name).lower()
        if os_lower in ("macos", "darwin"):
            return "🍎"
        if os_lower == "linux":
            return "🐧"
        if os_lower == "windows":
            return "🪟"
        if os_lower in ("ios", "android"):
            return "📱"
        return "💻"

    @property
    def display_label(self) -> str:
        """Formatted label shown in the target selector dropdown."""
        status = "Online" if self.online else "Offline"
        ip_part = f" ({self.ipv4})" if _is_valid_ipv4(self.ipv4) else ""
        safe_host = sanitize_display_text(self.hostname) or "Unknown Peer"
        return f"{self.os_icon} {safe_host}{ip_part} • {status}"


def _pick_ipv4(ips: object) -> str:
    """Select the first IPv4 address from a list of Tailscale IPs."""
    if not isinstance(ips, list):
        return ""
    for ip in ips:
        if _is_valid_ipv4(ip):
            return ip
    return ""


def sort_peers(peers: list[PeerDevice]) -> list[PeerDevice]:
    """Sort peers so online Mac devices appear first."""
    return sorted(
        peers,
        key=lambda peer: (
            not peer.is_mac,
            not peer.online,
            sanitize_display_text(peer.hostname).lower(),
        ),
    )


def parse_status_json(raw_json: str) -> tuple[list[PeerDevice], str | None]:
    """Parse `tailscale status --json` output into sorted peers and an optional error."""
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError) as exc:
        return [], f"Failed to parse Tailscale status: {sanitize_display_text(str(exc))}"

    if not isinstance(data, dict):
        return [], "Failed to parse Tailscale status: unexpected JSON format (expected object)."

    raw_state = data.get("BackendState")
    if raw_state is not None:
        if not isinstance(raw_state, str):
            return [], "Tailscale returned an invalid backend state. Start Tailscale and refresh."
        if raw_state and raw_state != "Running":
            safe_state = sanitize_display_text(raw_state).strip() or "unknown"
            return [], f"Tailscale is not running (state: {safe_state}). Start Tailscale and refresh."

    raw_peers = data.get("Peer")
    if not isinstance(raw_peers, dict):
        return [], None

    peers: list[PeerDevice] = []
    for entry in raw_peers.values():
        if not isinstance(entry, dict):
            continue

        raw_dns = entry.get("DNSName")
        if isinstance(raw_dns, str):
            dns_name = raw_dns[:-1] if raw_dns.endswith(".") else raw_dns
        else:
            dns_name = ""
        if not _is_valid_target_id(dns_name):
            dns_name = ""
        short_name = dns_name.split(".")[0] if dns_name else ""
        if not _is_valid_target_id(short_name):
            short_name = ""
            dns_name = ""

        ipv4 = _pick_ipv4(entry.get("TailscaleIPs"))
        if not short_name and not ipv4:
            continue

        raw_hostname = entry.get("HostName")
        hostname = sanitize_display_text(raw_hostname).strip() if isinstance(raw_hostname, str) else ""
        if not hostname:
            hostname = short_name or ipv4 or "Unknown Peer"

        raw_os = entry.get("OS")
        os_name = sanitize_display_text(raw_os).strip() if isinstance(raw_os, str) else ""

        raw_online = entry.get("Online")
        online = raw_online if isinstance(raw_online, bool) else False

        peers.append(
            PeerDevice(
                hostname=hostname,
                dns_name=dns_name,
                short_name=short_name,
                os_name=os_name,
                online=online,
                ipv4=ipv4,
            )
        )

    return sort_peers(peers), None


def parse_targets_output(raw_text: str) -> list[PeerDevice]:
    """Fallback parser for `tailscale file cp --targets` output (`<ip>\\t<name>`)."""
    peers: list[PeerDevice] = []
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) == 2:
            raw_ip, raw_name = parts[0].strip(), parts[1].strip()
            if not _is_valid_target_id(raw_name):
                continue
            if _is_valid_ipv4(raw_ip):
                ipv4 = raw_ip
            elif _is_valid_ipv6(raw_ip):
                ipv4 = ""
            else:
                continue
            short_name = raw_name
        else:
            raw_name = parts[0].strip()
            if not _is_valid_target_id(raw_name):
                continue
            ipv4, short_name = "", raw_name

        peers.append(
            PeerDevice(
                hostname=short_name or ipv4,
                dns_name=short_name,
                short_name=short_name,
                os_name="",
                online=True,
                ipv4=ipv4,
            )
        )
    return sort_peers(peers)


def discover_peers(tailscale_bin: str = "tailscale") -> tuple[list[PeerDevice], str | None]:
    """Query Tailscale CLI for available Taildrop target peers."""
    try:
        status_proc = subprocess.run(
            [tailscale_bin, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        return [], f"Tailscale binary '{sanitize_display_text(tailscale_bin)}' was not found on PATH."
    except subprocess.TimeoutExpired:
        return [], "Timed out while querying Tailscale status."
    except OSError as exc:
        return [], f"Failed to run Tailscale: {sanitize_display_text(str(exc))}"

    if status_proc.returncode != 0:
        err = (
            sanitize_display_text(status_proc.stderr).strip()
            or sanitize_display_text(status_proc.stdout).strip()
            or f"exit code {status_proc.returncode}"
        )
        return [], f"Tailscale status error: {err}"

    peers, err = parse_status_json(status_proc.stdout)
    if err:
        return [], err

    if peers:
        return peers, None

    # Fallback: `tailscale file cp --targets`
    try:
        targets_proc = subprocess.run(
            [tailscale_bin, "file", "cp", "--targets"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if targets_proc.returncode == 0:
            fallback_peers = parse_targets_output(targets_proc.stdout)
            if fallback_peers:
                return fallback_peers, None
    except (OSError, subprocess.TimeoutExpired):
        pass

    return [], "No Taildrop peers found. Make sure your Mac is online on the same Tailnet."


def validate_local_path(path: Path, *, require_absolute: bool = False) -> tuple[bool, str, bool]:
    """Validate that path is an existing regular file or directory (not a symlink or special file).

    Returns ``(is_valid, error_message, is_dir)``.
    """
    path_str = str(path)
    if "\x00" in path_str:
        return False, "Invalid path: embedded null byte.", False
    if require_absolute and not path.is_absolute():
        return False, f"Path must be absolute: {sanitize_display_text(path_str)}", False
    if path.name in ("", ".", ".."):
        return False, f"Invalid path: {sanitize_display_text(path_str)}", False

    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False, f"File not found: {sanitize_display_text(path_str)}", False
    except (OSError, ValueError) as exc:
        return False, f"Cannot access path: {sanitize_display_text(str(exc))}", False

    if stat.S_ISLNK(st.st_mode):
        return False, f"Refusing to send symlink: {sanitize_display_text(path_str)}", False
    if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
        return (
            False,
            f"Unsupported file type (not a regular file or directory): {sanitize_display_text(path_str)}",
            False,
        )

    is_dir = stat.S_ISDIR(st.st_mode)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, ValueError, RuntimeError) as exc:
        return False, f"Cannot resolve path: {sanitize_display_text(str(exc))}", False

    if not resolved.name:
        return False, f"Invalid path: {sanitize_display_text(path_str)}", False

    return True, "", is_dir


def _validate_drop_path(path_str: str) -> Path | None:
    """Validate a raw absolute path string and return a Path if safe."""
    if not path_str or "\x00" in path_str:
        return None
    candidate = Path(path_str)
    ok, _err, _is_dir = validate_local_path(candidate, require_absolute=True)
    if not ok:
        return None
    return candidate


def _gfile_to_safe_path(gfile: object) -> Path | None:
    """Extract and validate a local Path from a Gio.File-like object."""
    if hasattr(gfile, "get_uri"):
        try:
            uri = gfile.get_uri()
        except Exception:
            uri = None
        if isinstance(uri, str) and uri[:5].lower() == "file:":
            parsed = urlparse(uri)
            if parsed.netloc.lower() not in ("", "localhost"):
                return None
    if not hasattr(gfile, "get_path"):
        return None
    path_str = gfile.get_path()
    if not isinstance(path_str, str):
        return None
    return _validate_drop_path(path_str)


def _uri_or_path_to_path(item: str) -> Path | None:
    """Convert a local file:// URI or absolute filesystem path string into an existing Path."""
    if not isinstance(item, str):
        return None
    text = item.strip()
    if not text or text.startswith("#") or "\x00" in text:
        return None
    if text[:5].lower() == "file:":
        parsed = urlparse(text)
        if parsed.scheme.lower() != "file":
            return None
        if parsed.netloc.lower() not in ("", "localhost"):
            return None
        if parsed.query or parsed.fragment:
            return None
        path_str = unquote(parsed.path)
        if not path_str or "\x00" in path_str:
            return None
        return _validate_drop_path(path_str)
    return _validate_drop_path(text)


def extract_paths_from_drop(value: object) -> list[Path]:
    """Extract existing, deduplicated filesystem Paths from a GTK4 drop payload."""
    raw_candidates: list[Path] = []

    if value is None:
        return []

    if hasattr(value, "get_files"):
        for gfile in value.get_files():
            candidate = _gfile_to_safe_path(gfile)
            if candidate is not None:
                raw_candidates.append(candidate)
    elif hasattr(value, "get_path"):
        candidate = _gfile_to_safe_path(value)
        if candidate is not None:
            raw_candidates.append(candidate)
    elif isinstance(value, str):
        for line in value.splitlines():
            candidate = _uri_or_path_to_path(line)
            if candidate is not None:
                raw_candidates.append(candidate)

    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in raw_candidates:
        try:
            resolved = path.resolve(strict=True)
        except (OSError, ValueError, RuntimeError):
            continue
        if resolved not in seen:
            seen.add(resolved)
            deduped.append(path)
    return deduped


def prepare_sendable_file(
    path: Path,
    temp_dir: Path,
    cancel_event: threading.Event | None = None,
) -> Path:
    """Return a regular file path, zipping directories because Taildrop only sends files."""
    ok, err, is_dir = validate_local_path(path)
    if not ok:
        if err.startswith("File not found:"):
            raise FileNotFoundError(err)
        raise ValueError(err)

    resolved = path.resolve(strict=True)
    if not is_dir:
        return resolved

    archive_path = temp_dir / f"{resolved.name}.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, dirnames, filenames in os.walk(resolved, followlinks=False):
            if cancel_event and cancel_event.is_set():
                raise InterruptedError("Cancelled")
            root_path = Path(root)
            dirnames[:] = sorted(
                d for d in dirnames if not (root_path / d).is_symlink()
            )
            rel_root = root_path.relative_to(resolved.parent)
            if not dirnames and not filenames:
                zf.write(root_path, arcname=rel_root)
            for filename in sorted(filenames):
                if cancel_event and cancel_event.is_set():
                    raise InterruptedError("Cancelled")
                file_path = root_path / filename
                try:
                    fst = os.lstat(file_path)
                except OSError:
                    continue
                if not stat.S_ISREG(fst.st_mode):
                    continue
                zf.write(file_path, arcname=file_path.relative_to(resolved.parent))
    return archive_path


def format_item_size(path: Path) -> str:
    """Format a file size or directory description for UI display."""
    if "\x00" in str(path):
        return ""
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return ""
    if stat.S_ISLNK(st.st_mode):
        return ""
    if stat.S_ISDIR(st.st_mode):
        try:
            count = sum(1 for _ in path.iterdir())
            return f"Folder ({count} item{'s' if count != 1 else ''})"
        except (OSError, ValueError):
            return "Folder"
    if not stat.S_ISREG(st.st_mode):
        return ""
    size = float(st.st_size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return ""


def send_file_to_peer(
    path: Path,
    target_id: str,
    tailscale_bin: str = "tailscale",
    cancel_event: threading.Event | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """Send a file (or zipped folder) to `<target_id>:` via `tailscale file cp`."""
    if not target_id:
        return False, "No target device selected."
    if not _is_valid_target_id(target_id):
        return False, "Invalid target device identifier."

    ok_path, path_err, is_dir = validate_local_path(path)
    if not ok_path:
        return False, path_err

    if cancel_event and cancel_event.is_set():
        return False, "Cancelled"

    temp_ctx: tempfile.TemporaryDirectory[str] | None = None
    try:
        if is_dir:
            if on_stage:
                on_stage("Zipping folder…")
            temp_ctx = tempfile.TemporaryDirectory(prefix="taildrop_gui_")
            send_path = prepare_sendable_file(
                path,
                Path(temp_ctx.name),
                cancel_event=cancel_event,
            )
        else:
            send_path = prepare_sendable_file(
                path,
                path.parent,
                cancel_event=cancel_event,
            )

        if cancel_event and cancel_event.is_set():
            return False, "Cancelled"

        resolved_send = send_path.resolve(strict=True)
        if str(resolved_send).startswith("-"):
            return False, "Invalid file path."

        if on_stage:
            on_stage("Sending via Taildrop…")

        proc = subprocess.Popen(
            [tailscale_bin, "file", "cp", str(resolved_send), f"{target_id}:"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        while proc.poll() is None:
            if cancel_event and cancel_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
                return False, "Cancelled"
            time.sleep(0.1)

        stdout, stderr = proc.communicate()
        if proc.returncode == 0:
            if is_dir:
                return True, f"Sent as {send_path.name}"
            return True, "Sent successfully"

        err_msg = (
            sanitize_display_text(stderr).strip()
            or sanitize_display_text(stdout).strip()
            or f"Exited with code {proc.returncode}"
        )
        return False, err_msg
    except InterruptedError:
        return False, "Cancelled"
    except (FileNotFoundError, ValueError) as exc:
        return False, sanitize_display_text(str(exc))
    except OSError as exc:
        return False, f"Transfer failed: {sanitize_display_text(str(exc))}"
    finally:
        if temp_ctx is not None:
            temp_ctx.cleanup()
