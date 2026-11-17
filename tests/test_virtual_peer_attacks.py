"""Adversarial virtual-peer and mock-CLI security tests for taildrop_gui.

All tests simulate hostile Tailscale peers and CLI responses exclusively in
memory or via a temporary mock `tailscale` executable in `tmp_path`. No test
ever invokes a real `tailscale` binary or contacts connected devices.
"""

from __future__ import annotations

import ipaddress
import itertools
import json
import os
from pathlib import Path
import re
import sys
import time

for _dist in ("/usr/lib/python3/dist-packages", "/usr/local/lib/python3/dist-packages"):
    if _dist not in sys.path and os.path.isdir(_dist):
        sys.path.append(_dist)

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gio, GLib, Pango  # noqa: E402
import pytest  # noqa: E402

import taildrop_gui.app as app_module  # noqa: E402
from taildrop_gui.app import TaildropWindow  # noqa: E402
from taildrop_gui.backend import (  # noqa: E402
    PeerDevice,
    discover_peers,
    parse_status_json,
    parse_targets_output,
    send_file_to_peer,
)

# Conservative safe target pattern: valid Tailscale short name / DNS hostname label or IPv4.
SAFE_TARGET_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")

BIDI_AND_CONTROL_CHARS = (
    "\x00",
    "\x07",
    "\x1b",
    "\n",
    "\r",
    "\t",
    "\u200e",
    "\u200f",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
    "\u2066",
    "\u2067",
    "\u2068",
    "\u2069",
)


def _is_valid_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
        return True
    except ValueError:
        return False


def _is_safe_target_id(target: str) -> bool:
    if not target or target in (".", ".."):
        return False
    if target.startswith("-") or ".." in target:
        return False
    if any(ch in target for ch in (":", "/", "\\", "<", ">", "&", ";", "$", "`")):
        return False
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in target):
        return False
    if any(ch in target for ch in BIDI_AND_CONTROL_CHARS):
        return False
    return bool(SAFE_TARGET_RE.match(target))


def _assert_no_bidi_or_control(text: str) -> None:
    for ch in BIDI_AND_CONTROL_CHARS:
        assert ch not in text, f"Unexpected control/bidi char {ch!r} in {text!r}"
    for ch in text:
        code = ord(ch)
        assert not (0 <= code < 32 or 127 <= code <= 159), (
            f"Unexpected control char U+{code:04X} in {text!r}"
        )


def _create_mock_tailscale_cli(
    tmp_path: Path,
    *,
    status_stdout: str = "",
    status_stderr: str = "",
    status_rc: int = 0,
    targets_stdout: str = "",
    targets_stderr: str = "",
    targets_rc: int = 0,
    cp_stdout: str = "",
    cp_stderr: str = "",
    cp_rc: int = 0,
) -> tuple[Path, Path]:
    """Create an executable virtual `tailscale` stub in `tmp_path` and log all argv calls."""
    config_path = tmp_path / "stub_config.json"
    log_path = tmp_path / "stub_invocations.jsonl"
    config_path.write_text(
        json.dumps(
            {
                "status_stdout": status_stdout,
                "status_stderr": status_stderr,
                "status_rc": status_rc,
                "targets_stdout": targets_stdout,
                "targets_stderr": targets_stderr,
                "targets_rc": targets_rc,
                "cp_stdout": cp_stdout,
                "cp_stderr": cp_stderr,
                "cp_rc": cp_rc,
            }
        ),
        encoding="utf-8",
    )

    stub_path = tmp_path / "tailscale"
    stub_code = f"""#!{sys.executable}
import json
import sys
from pathlib import Path

config = json.loads(Path({str(config_path)!r}).read_text(encoding="utf-8"))
log_path = Path({str(log_path)!r})
args = sys.argv[1:]
with log_path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\\n")

if args[:2] == ["status", "--json"]:
    sys.stdout.write(config["status_stdout"])
    sys.stderr.write(config["status_stderr"])
    raise SystemExit(config["status_rc"])

if args[:3] == ["file", "cp", "--targets"]:
    sys.stdout.write(config["targets_stdout"])
    sys.stderr.write(config["targets_stderr"])
    raise SystemExit(config["targets_rc"])

if args[:2] == ["file", "cp"]:
    sys.stdout.write(config["cp_stdout"])
    sys.stderr.write(config["cp_stderr"])
    raise SystemExit(config["cp_rc"])

sys.stderr.write(f"unexpected args: {{args!r}}\\n")
raise SystemExit(2)
"""
    stub_path.write_text(stub_code, encoding="utf-8")
    stub_path.chmod(0o755)
    return stub_path, log_path


def _read_stub_invocations(log_path: Path) -> list[list[str]]:
    if not log_path.exists():
        return []
    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


_APP_COUNTER = itertools.count(1)


def _make_window(monkeypatch: pytest.MonkeyPatch) -> TaildropWindow:
    monkeypatch.setattr(TaildropWindow, "_start_worker", lambda self: None)
    monkeypatch.setattr(TaildropWindow, "refresh_peers", lambda self: None)
    app = Adw.Application(
        application_id=f"io.github.taildrop_gui.RedteamPeerTest{next(_APP_COUNTER)}",
        flags=Gio.ApplicationFlags.NON_UNIQUE,
    )
    app.register(None)
    return TaildropWindow(app)


# ---------------------------------------------------------------------------
# 1. Malformed / Hostile `tailscale status --json` Payloads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_json",
    [
        "[]",
        "[1, 2, 3]",
        "123",
        "3.14",
        "true",
        '"just a string"',
        "null",
    ],
)
def test_parse_status_json_rejects_non_dict_roots(raw_json: str) -> None:
    peers, err = parse_status_json(raw_json)
    assert peers == []
    assert isinstance(err, str) and err.strip() != ""


@pytest.mark.parametrize("raw_json", ["[]", "null", "42", '"bad"'])
def test_discover_peers_handles_non_dict_json_from_cli_stub(tmp_path: Path, raw_json: str) -> None:
    stub, log_path = _create_mock_tailscale_cli(tmp_path, status_stdout=raw_json)
    peers, err = discover_peers(tailscale_bin=str(stub))
    assert peers == []
    assert isinstance(err, str) and err.strip() != ""
    assert _read_stub_invocations(log_path) == [["status", "--json"]]


def test_background_refresh_peers_recovers_from_malformed_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub, _ = _create_mock_tailscale_cli(tmp_path, status_stdout="[]")
    monkeypatch.setattr(
        app_module,
        "discover_peers",
        lambda: discover_peers(tailscale_bin=str(stub)),
    )
    monkeypatch.setattr(TaildropWindow, "_start_worker", lambda self: None)

    app = Adw.Application(
        application_id=f"io.github.taildrop_gui.RedteamPeerBgTest{next(_APP_COUNTER)}",
        flags=Gio.ApplicationFlags.NON_UNIQUE,
    )
    app.register(None)
    win = TaildropWindow(app)
    try:
        ctx = GLib.MainContext.default()
        deadline = time.monotonic() + 2.0
        while not win._peers_loaded and time.monotonic() < deadline:
            while ctx.pending():
                ctx.iteration(False)
            time.sleep(0.01)

        assert win._peers_loaded is True
        assert win.refresh_btn.get_sensitive() is True
        assert win.peers == []
        assert win.banner.get_revealed() is True
    finally:
        win.close()


@pytest.mark.parametrize(
    "bad_ips",
    [
        "100.64.0.2",
        "fd7a::1",
        ".",
        123,
        True,
        {"ipv4": "100.64.0.2"},
        {"100.64.0.2": True},
        [123, "100.64.0.2"],
        [None],
        [{"ip": "100.64.0.2"}],
        ["fd7a:115c:a1e0::2"],
        ["--flag.evil"],
        ["../etc"],
        ["-flag"],
        ["100.64.0.2\x00evil"],
        ["100.64.0.2\u202e"],
    ],
)
def test_parse_status_json_handles_malformed_tailscale_ips(bad_ips: object) -> None:
    payload_no_dns = {
        "BackendState": "Running",
        "Peer": {
            "node-malformed": {
                "HostName": "Weird Peer",
                "DNSName": "",
                "OS": "linux",
                "Online": True,
                "TailscaleIPs": bad_ips,
            }
        },
    }
    peers_no_dns, err_no_dns = parse_status_json(json.dumps(payload_no_dns))
    assert err_no_dns is None
    for peer in peers_no_dns:
        assert peer.ipv4 == "" or _is_valid_ipv4(peer.ipv4)
        assert _is_safe_target_id(peer.target_id)

    # Without valid DNSName and without a valid IPv4 address (except mixed list [123, "100.64.0.2"]),
    # no peer should be emitted with bogus ipv4 such as ".", "f", "-flag", or IPv6.
    if not (isinstance(bad_ips, list) and "100.64.0.2" in bad_ips):
        assert peers_no_dns == []

    payload_with_dns = {
        "BackendState": "Running",
        "Peer": {
            "node-with-dns": {
                "HostName": "My MacBook",
                "DNSName": "my-macbook.example.ts.net.",
                "OS": "macOS",
                "Online": True,
                "TailscaleIPs": bad_ips,
            }
        },
    }
    peers_with_dns, err_with_dns = parse_status_json(json.dumps(payload_with_dns))
    assert err_with_dns is None
    assert len(peers_with_dns) == 1
    assert peers_with_dns[0].ipv4 == "" or _is_valid_ipv4(peers_with_dns[0].ipv4)
    assert peers_with_dns[0].ipv4 not in (".", "f", "-flag", "--flag.evil", "../etc")


def test_parse_status_json_handles_missing_and_non_string_peer_fields() -> None:
    payload = {
        "BackendState": "Running",
        "Peer": {
            "node-empty": {},
            "node-not-dict": "corrupted",
            "node-null": None,
            "node-list": ["bad"],
            "node-non-strings": {
                "HostName": {"evil": 1},
                "DNSName": ["spoofed.example.ts.net."],
                "OS": 12345,
                "Online": "false",
                "TailscaleIPs": ["100.64.0.50"],
            },
            "node-int-fields": {
                "HostName": 999,
                "DNSName": True,
                "OS": ["macOS"],
                "Online": [0],
                "TailscaleIPs": ["100.64.0.51"],
            },
            "node-good": {
                "HostName": "Good Mac",
                "DNSName": "good-mac.example.ts.net.",
                "OS": "macOS",
                "Online": True,
                "TailscaleIPs": ["100.64.0.2"],
            },
        },
    }
    peers, err = parse_status_json(json.dumps(payload))
    assert err is None
    assert any(p.target_id == "good-mac" for p in peers)

    for peer in peers:
        assert "{" not in peer.hostname and "[" not in peer.hostname
        assert "{" not in peer.dns_name and "[" not in peer.dns_name
        assert "{" not in peer.os_name and "[" not in peer.os_name
        assert peer.hostname not in ("999", "True", "{'evil': 1}")
        assert peer.os_name not in ("12345", "['macOS']")
        if peer.ipv4 in ("100.64.0.50", "100.64.0.51"):
            assert peer.short_name == ""
            assert peer.dns_name == ""
            assert peer.os_name == ""
            assert peer.online is False


def test_parse_status_json_handles_non_string_backend_state() -> None:
    for bad_state in (False, 0, 123, ["Stopped"], {"state": "Stopped"}):
        payload = {"BackendState": bad_state, "Peer": {}}
        peers, err = parse_status_json(json.dumps(payload))
        assert peers == []
        # Non-string BackendState should not leak Python reprs or bypass stopped state silently.
        if err is not None:
            assert "{" not in err and "[" not in err


# ---------------------------------------------------------------------------
# 2. Hostile Virtual Peer Identifiers (`parse_status_json`, `parse_targets_output`,
#    `PeerDevice.target_id`, and `send_file_to_peer`)
# ---------------------------------------------------------------------------

HOSTILE_TARGET_IDS = [
    "-flag",
    "--targets",
    "--help",
    "-rf",
    ".",
    "..",
    "peer:8080",
    "100.64.0.2:8080",
    "fd7a:115c:a1e0::2",
    "evil/path",
    "evil/../../etc",
    "evil\\path",
    "bad target",
    "bad\ttarget",
    "bad\ntarget",
    "bad\rtarget",
    "bad\x00target",
    "mac\u202eevil",
    "\u202eonline-mac",
    "mac\x1b[31m",
    "<script>",
    "AT&T",
    "<span color='red'>x</span>",
    "peer;id",
    "$(whoami)",
]


@pytest.mark.parametrize("hostile", HOSTILE_TARGET_IDS)
def test_peer_device_target_id_rejects_hostile_fallback_and_fields(hostile: str) -> None:
    # Empty short_name and ipv4 -> currently falls back to self.hostname verbatim.
    fallback_peer = PeerDevice(
        hostname=hostile,
        dns_name="",
        short_name="",
        os_name="macOS",
        online=True,
        ipv4="",
    )
    assert fallback_peer.target_id == ""

    # Hostile short_name with empty ipv4 must also yield empty target_id.
    short_peer = PeerDevice(
        hostname="Friendly Name",
        dns_name=f"{hostile}.example.ts.net",
        short_name=hostile,
        os_name="macOS",
        online=True,
        ipv4="",
    )
    assert short_peer.target_id == ""

    # Hostile ipv4 with empty short_name must not leak into target_id.
    ipv4_peer = PeerDevice(
        hostname="Friendly Name",
        dns_name="",
        short_name="",
        os_name="macOS",
        online=True,
        ipv4=hostile,
    )
    assert ipv4_peer.target_id == ""


def test_parse_status_json_blocks_hostile_peer_identifiers(tmp_path: Path) -> None:
    hostile_dns_entries = {
        "node-dash": "-targets.example.ts.net.",
        "node-colon": "evil:8080.example.ts.net.",
        "node-slash": "evil/../../etc.example.ts.net.",
        "node-backslash": "evil\\path.example.ts.net.",
        "node-space": "evil peer.example.ts.net.",
        "node-newline": "evil\n--help.example.ts.net.",
        "node-null": "evil\x00peer.example.ts.net.",
        "node-rlo": "evil\u202epeer.example.ts.net.",
        "node-markup": "<script>&<span>.example.ts.net.",
        "node-dots": "...example.ts.net.",
    }
    peer_map: dict[str, object] = {
        key: {
            "HostName": f"Host {key}",
            "DNSName": dns,
            "OS": "linux",
            "Online": True,
            "TailscaleIPs": [],
        }
        for key, dns in hostile_dns_entries.items()
    }
    # Hostile DNSName paired with valid IPv4: must never expose hostile short_name as target_id.
    peer_map["node-dash-with-ip"] = {
        "HostName": "Sneaky Dash",
        "DNSName": "-targets.example.ts.net.",
        "OS": "linux",
        "Online": True,
        "TailscaleIPs": ["100.64.0.99"],
    }
    # Legitimate peer must still work.
    peer_map["node-good"] = {
        "HostName": "My MacBook Pro",
        "DNSName": "my-macbook-pro.example.ts.net.",
        "OS": "macOS",
        "Online": True,
        "TailscaleIPs": ["100.64.0.2"],
    }

    raw = json.dumps({"BackendState": "Running", "Peer": peer_map})
    stub, _ = _create_mock_tailscale_cli(tmp_path, status_stdout=raw)

    peers, err = discover_peers(tailscale_bin=str(stub))
    assert err is None
    assert any(p.target_id == "my-macbook-pro" for p in peers)

    for peer in peers:
        assert _is_safe_target_id(peer.target_id), f"Unsafe target_id leaked: {peer.target_id!r}"
        assert peer.target_id != "-targets"


def test_parse_targets_output_blocks_hostile_identifiers(tmp_path: Path) -> None:
    targets_text = "\n".join(
        [
            "100.64.0.5\t--targets",
            "100.64.0.6\tevil:8080",
            "100.64.0.7\tevil/../../etc",
            "100.64.0.8\tevil peer",
            "100.64.0.9\tevil\x00peer",
            "100.64.0.10\tevil\u202epeer",
            "100.64.0.11\t<script>&<span>",
            "--help",
            "not-an-ip\tbad-ip-peer",
            "100.64.0.2\tmy-macbook-pro",
        ]
    )

    direct_peers = parse_targets_output(targets_text)
    assert any(p.target_id == "my-macbook-pro" for p in direct_peers)
    for peer in direct_peers:
        assert _is_safe_target_id(peer.target_id), f"Unsafe fallback target_id: {peer.target_id!r}"
        assert peer.ipv4 == "" or _is_valid_ipv4(peer.ipv4)
        assert peer.ipv4 != "not-an-ip"

    # Also verify end-to-end via discover_peers fallback to `file cp --targets`.
    empty_status = json.dumps({"BackendState": "Running", "Peer": {}})
    stub, log_path = _create_mock_tailscale_cli(
        tmp_path,
        status_stdout=empty_status,
        targets_stdout=targets_text,
    )
    cli_peers, err = discover_peers(tailscale_bin=str(stub))
    assert err is None
    assert any(p.target_id == "my-macbook-pro" for p in cli_peers)
    for peer in cli_peers:
        assert _is_safe_target_id(peer.target_id), f"Unsafe CLI fallback target_id: {peer.target_id!r}"
    assert _read_stub_invocations(log_path) == [
        ["status", "--json"],
        ["file", "cp", "--targets"],
    ]


@pytest.mark.parametrize("bad_target", ["", *HOSTILE_TARGET_IDS])
def test_send_file_to_peer_blocks_hostile_targets_without_executing_cli(
    tmp_path: Path, bad_target: str
) -> None:
    sample = tmp_path / "notes.txt"
    sample.write_text("safe payload", encoding="utf-8")

    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "item.txt").write_text("inside", encoding="utf-8")

    stub, log_path = _create_mock_tailscale_cli(tmp_path)

    for target_path in (sample, folder):
        ok, msg = send_file_to_peer(target_path, bad_target, tailscale_bin=str(stub))
        assert ok is False
        assert "Invalid target" in msg or "No target" in msg
        assert "embedded null byte" not in msg

    assert _read_stub_invocations(log_path) == []


def test_send_file_to_peer_accepts_valid_targets_with_mock_cli(tmp_path: Path) -> None:
    sample = tmp_path / "report.pdf"
    sample.write_bytes(b"%PDF-1.4")

    stub, log_path = _create_mock_tailscale_cli(tmp_path)

    ok1, msg1 = send_file_to_peer(sample, "my-macbook-pro", tailscale_bin=str(stub))
    ok2, msg2 = send_file_to_peer(sample, "100.64.0.2", tailscale_bin=str(stub))
    assert ok1 is True and "Sent" in msg1
    assert ok2 is True and "Sent" in msg2
    assert _read_stub_invocations(log_path) == [
        ["file", "cp", str(sample.resolve()), "my-macbook-pro:"],
        ["file", "cp", str(sample.resolve()), "100.64.0.2:"],
    ]


# ---------------------------------------------------------------------------
# 3. Display Spoofing (RLO `\u202e`, Control Chars, Null Truncation) & Pango Markup
# ---------------------------------------------------------------------------


def test_hostile_display_strings_strip_bidi_control_and_null_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spoofed_hostname = "My Mac\u202eevil\x00hidden\nFake\x1b[31m"
    spoofed_os = "macOS\u202e\x00\n"
    payload = {
        "BackendState": "Running",
        "Peer": {
            "node-spoof": {
                "HostName": spoofed_hostname,
                "DNSName": "my-mac.example.ts.net.",
                "OS": spoofed_os,
                "Online": True,
                "TailscaleIPs": ["100.64.0.2"],
            }
        },
    }
    peers, err = parse_status_json(json.dumps(payload))
    assert err is None
    assert len(peers) == 1
    peer = peers[0]

    _assert_no_bidi_or_control(peer.hostname)
    _assert_no_bidi_or_control(peer.os_name)
    _assert_no_bidi_or_control(peer.display_label)
    assert "Online" in peer.display_label

    # Also verify CLI stderr/BackendState errors are sanitized.
    stub, _ = _create_mock_tailscale_cli(
        tmp_path,
        status_rc=1,
        status_stderr="Failed\u202espoof\x00hidden\n\x1b[31m",
    )
    _, cli_err = discover_peers(tailscale_bin=str(stub))
    assert cli_err is not None
    _assert_no_bidi_or_control(cli_err)

    _, state_err = parse_status_json(
        json.dumps({"BackendState": "Stopped\u202e\x00hidden\n\x1b[31m", "Peer": {}})
    )
    assert state_err is not None
    _assert_no_bidi_or_control(state_err)

    # Render into TaildropWindow and confirm GTK/PyGObject does not truncate at \x00 or render RLO.
    sample = tmp_path / "draft.txt"
    sample.write_text("hi", encoding="utf-8")

    win = _make_window(monkeypatch)
    recorded_toasts: list[Adw.Toast] = []
    monkeypatch.setattr(win.toast_overlay, "add_toast", recorded_toasts.append)
    try:
        win._apply_peers([peer], None)
        dropdown_text = win.peer_model.get_string(0)
        assert dropdown_text is not None
        _assert_no_bidi_or_control(dropdown_text)
        assert "Online" in dropdown_text

        target_sub = win.target_row.get_subtitle() or ""
        _assert_no_bidi_or_control(target_sub)
        assert "Online" in target_sub

        win.enqueue_paths([sample])
        item = win._transfer_items[0]
        _assert_no_bidi_or_control(item.row.get_subtitle() or "")

        win._finish_item(item, False, "cli err\u202espoof\x00hidden\n\x1b[31m")
        _assert_no_bidi_or_control(item.row.get_subtitle() or "")

        win._apply_peers([], cli_err)
        _assert_no_bidi_or_control(win.banner.get_title() or "")

        assert len(recorded_toasts) >= 1
        for toast in recorded_toasts:
            _assert_no_bidi_or_control(toast.get_title() or "")
            assert toast.get_use_markup() is False
    finally:
        win.close()


def test_pango_markup_payloads_safely_escaped_in_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    markup_hostname = "<script>alert(1)</script> & <span foreground='red'>evil</span>"
    markup_os = "<b>macOS</b> & Linux"
    payload = {
        "BackendState": "Running",
        "Peer": {
            "node-markup": {
                "HostName": markup_hostname,
                "DNSName": "markup-mac.example.ts.net.",
                "OS": markup_os,
                "Online": True,
                "TailscaleIPs": ["100.64.0.2"],
            }
        },
    }
    peers, err = parse_status_json(json.dumps(payload))
    assert err is None
    assert len(peers) == 1
    peer = peers[0]

    sample = tmp_path / "draft.txt"
    sample.write_text("hi", encoding="utf-8")

    win = _make_window(monkeypatch)
    recorded_toasts: list[Adw.Toast] = []
    monkeypatch.setattr(win.toast_overlay, "add_toast", recorded_toasts.append)
    try:
        win._apply_peers([peer], None)

        target_sub = win.target_row.get_subtitle() or ""
        ok, attrs, _plain, _ = Pango.parse_markup(target_sub, -1, "\x00")
        assert ok is True
        assert attrs.get_attributes() == []

        win.enqueue_paths([sample])
        item = win._transfer_items[0]
        staged_sub = item.row.get_subtitle() or ""
        ok2, attrs2, _plain2, _ = Pango.parse_markup(staged_sub, -1, "\x00")
        assert ok2 is True
        assert attrs2.get_attributes() == []

        win._finish_item(item, False, "<script>&<span color='red'>boom</span>")
        failed_sub = item.row.get_subtitle() or ""
        ok3, attrs3, _plain3, _ = Pango.parse_markup(failed_sub, -1, "\x00")
        assert ok3 is True
        assert attrs3.get_attributes() == []

        win._apply_peers([], "Error <script>&<span color='red'>bad</span>")
        assert win.banner.get_use_markup() is False
        assert all(t.get_use_markup() is False for t in recorded_toasts)
    finally:
        win.close()
