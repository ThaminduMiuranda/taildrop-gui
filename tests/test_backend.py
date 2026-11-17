"""Unit and integration tests for taildrop_gui.backend."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
import zipfile

for _dist in ("/usr/lib/python3/dist-packages", "/usr/local/lib/python3/dist-packages"):
    if _dist not in sys.path and os.path.isdir(_dist):
        sys.path.append(_dist)

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio  # noqa: E402

from taildrop_gui.backend import (  # noqa: E402
    discover_peers,
    extract_paths_from_drop,
    parse_status_json,
    prepare_sendable_file,
    send_file_to_peer,
)


def test_parse_status_json_sorts_online_mac_first() -> None:
    payload = {
        "BackendState": "Running",
        "Peer": {
            "node-linux": {
                "HostName": "ubuntu-server",
                "DNSName": "ubuntu-server.example.ts.net.",
                "OS": "linux",
                "Online": True,
                "TailscaleIPs": ["100.64.0.10"],
            },
            "node-offline-mac": {
                "HostName": "old-mac-mini",
                "DNSName": "old-mac-mini.example.ts.net.",
                "OS": "macOS",
                "Online": False,
                "TailscaleIPs": ["100.64.0.11"],
            },
            "node-mac": {
                "HostName": "My MacBook Pro",
                "DNSName": "my-macbook-pro.example.ts.net.",
                "OS": "macOS",
                "Online": True,
                "TailscaleIPs": ["100.64.0.2", "fd7a:115c:a1e0::2"],
            },
        },
    }

    peers, err = parse_status_json(json.dumps(payload))
    assert err is None
    assert len(peers) == 3
    assert peers[0].hostname == "My MacBook Pro"
    assert peers[0].target_id == "my-macbook-pro"
    assert peers[0].ipv4 == "100.64.0.2"
    assert peers[0].os_name == "macOS"
    assert peers[0].is_mac is True
    assert peers[0].online is True
    assert peers[1].hostname == "old-mac-mini"
    assert peers[2].hostname == "ubuntu-server"


def test_parse_status_json_not_running() -> None:
    peers, err = parse_status_json(json.dumps({"BackendState": "Stopped", "Peer": {}}))
    assert peers == []
    assert err is not None
    assert "Stopped" in err


def test_discover_peers_finds_mac_from_mocked_status(monkeypatch) -> None:
    payload = {
        "BackendState": "Running",
        "Peer": {
            "node-mac": {
                "HostName": "My MacBook Pro",
                "DNSName": "my-macbook-pro.example.ts.net.",
                "OS": "macOS",
                "Online": True,
                "TailscaleIPs": ["100.64.0.2"],
            }
        },
    }

    class FakeResult:
        returncode = 0
        stdout = json.dumps(payload)
        stderr = ""

    monkeypatch.setattr("taildrop_gui.backend.subprocess.run", lambda *_args, **_kwargs: FakeResult())

    peers, err = discover_peers()
    assert err is None
    assert len(peers) == 1
    assert peers[0].target_id == "my-macbook-pro"
    assert peers[0].ipv4 == "100.64.0.2"
    assert peers[0].is_mac is True


def test_extract_paths_from_drop_variants(tmp_path: Path) -> None:
    file_a = tmp_path / "hello world.txt"
    file_a.write_text("hello", encoding="utf-8")
    file_b = tmp_path / "second.bin"
    file_b.write_bytes(b"\x00\x01")
    missing = tmp_path / "does_not_exist.txt"

    # Gdk.FileList
    gfile_list = Gdk.FileList.new_from_list(
        [Gio.File.new_for_path(str(file_a)), Gio.File.new_for_path(str(file_b))]
    )
    assert extract_paths_from_drop(gfile_list) == [file_a, file_b]

    # Single Gio.File
    assert extract_paths_from_drop(Gio.File.new_for_path(str(file_a))) == [file_a]

    # file:// URIs (URL-encoded spaces) + plain path + duplicate + missing + comment
    uri_payload = (
        "# comment\r\n"
        f"{file_a.as_uri()}\r\n"
        f"{file_b}\n"
        f"{file_a}\n"
        f"{missing.as_uri()}\n"
    )
    assert extract_paths_from_drop(uri_payload) == [file_a, file_b]


def test_prepare_sendable_file_for_file_and_dir(tmp_path: Path) -> None:
    regular = tmp_path / "notes.md"
    regular.write_text("# Notes", encoding="utf-8")

    stage_dir = tmp_path / "staging"
    stage_dir.mkdir()

    assert prepare_sendable_file(regular, stage_dir) == regular

    folder = tmp_path / "my_folder"
    folder.mkdir()
    (folder / "inside.txt").write_text("content", encoding="utf-8")

    zip_path = prepare_sendable_file(folder, stage_dir)
    assert zip_path.exists()
    assert zip_path.name == "my_folder.zip"
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        assert "my_folder/inside.txt" in names


def test_send_file_to_peer_invokes_tailscale_file_cp(tmp_path: Path, monkeypatch) -> None:
    sample = tmp_path / "report.pdf"
    sample.write_bytes(b"%PDF-1.4")

    recorded_cmds: list[list[str]] = []
    stages: list[str] = []

    class FakeProc:
        def __init__(self, cmd: list[str], **_kwargs) -> None:
            recorded_cmds.append(cmd)
            self.returncode = 0

        def poll(self) -> int:
            return 0

        def communicate(self) -> tuple[str, str]:
            return ("", "")

    monkeypatch.setattr("taildrop_gui.backend.subprocess.Popen", FakeProc)

    ok, msg = send_file_to_peer(
        sample,
        "my-macbook-pro",
        on_stage=stages.append,
    )
    assert ok is True
    assert "Sent" in msg
    assert recorded_cmds == [["tailscale", "file", "cp", str(sample.resolve()), "my-macbook-pro:"]]
    assert stages == ["Sending via Taildrop…"]


def test_send_folder_to_peer_zips_and_calls_tailscale(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "assets"
    folder.mkdir()
    (folder / "logo.png").write_bytes(b"png")

    recorded_cmds: list[list[str]] = []
    stages: list[str] = []

    class FakeProc:
        def __init__(self, cmd: list[str], **_kwargs) -> None:
            recorded_cmds.append(cmd)
            # Ensure the temporary zip exists while tailscale is running
            assert Path(cmd[3]).exists()
            assert cmd[3].endswith("assets.zip")
            self.returncode = 0

        def poll(self) -> int:
            return 0

        def communicate(self) -> tuple[str, str]:
            return ("", "")

    monkeypatch.setattr("taildrop_gui.backend.subprocess.Popen", FakeProc)

    ok, msg = send_file_to_peer(
        folder,
        "my-macbook-pro",
        on_stage=stages.append,
    )
    assert ok is True
    assert msg == "Sent as assets.zip"
    assert stages == ["Zipping folder…", "Sending via Taildrop…"]
    assert len(recorded_cmds) == 1
    assert recorded_cmds[0][0:3] == ["tailscale", "file", "cp"]
    assert recorded_cmds[0][4] == "my-macbook-pro:"


def test_prepare_sendable_file_skips_symlinks_inside_folder(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("sensitive", encoding="utf-8")

    folder = tmp_path / "bundle"
    folder.mkdir()
    (folder / "readme.txt").write_text("ok", encoding="utf-8")
    (folder / "secret_link.txt").symlink_to(secret)

    linked_dir = tmp_path / "linked_dir"
    linked_dir.mkdir()
    (linked_dir / "hidden.txt").write_text("hidden", encoding="utf-8")
    (folder / "subdir_link").symlink_to(linked_dir)

    stage_dir = tmp_path / "staging"
    stage_dir.mkdir()

    zip_path = prepare_sendable_file(folder, stage_dir)
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        assert "bundle/readme.txt" in names
        assert "bundle/secret_link.txt" not in names
        assert "bundle/subdir_link/hidden.txt" not in names


def test_send_file_to_peer_rejects_invalid_target_id(tmp_path: Path) -> None:
    sample = tmp_path / "report.pdf"
    sample.write_bytes(b"%PDF-1.4")

    for bad_target in ("-flag", "--targets", "bad target", "peer:extra"):
        ok, msg = send_file_to_peer(sample, bad_target)
        assert ok is False
        assert "Invalid target" in msg


def test_send_file_to_peer_cancellation(tmp_path: Path, monkeypatch) -> None:
    sample = tmp_path / "big.iso"
    sample.write_bytes(b"data")

    terminated = {"called": False}

    class SlowProc:
        def __init__(self, _cmd: list[str], **_kwargs) -> None:
            self.returncode = None

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            terminated["called"] = True

        def wait(self, timeout: float | None = None) -> int:
            return -15

    monkeypatch.setattr("taildrop_gui.backend.subprocess.Popen", SlowProc)

    cancel = threading.Event()
    cancel.set()
    # Check immediate pre-launch cancel
    ok, msg = send_file_to_peer(sample, "my-macbook-pro", cancel_event=cancel)
    assert ok is False
    assert msg == "Cancelled"

    # Check in-flight cancel
    cancel2 = threading.Event()

    def trigger() -> None:
        cancel2.set()

    timer = threading.Timer(0.05, trigger)
    timer.start()
    ok2, msg2 = send_file_to_peer(sample, "my-macbook-pro", cancel_event=cancel2)
    timer.join()
    assert ok2 is False
    assert msg2 == "Cancelled"
    assert terminated["called"] is True


def test_window_stages_on_drop_and_sends_on_button_click(tmp_path: Path, monkeypatch) -> None:
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio as GioRepo

    from taildrop_gui.app import TaildropWindow
    from taildrop_gui.backend import PeerDevice

    sample = tmp_path / "draft.txt"
    sample.write_text("hello", encoding="utf-8")

    fake_peer = PeerDevice(
        hostname="My MacBook Pro",
        dns_name="my-macbook-pro.example.ts.net",
        short_name="my-macbook-pro",
        os_name="macOS",
        online=True,
        ipv4="100.64.0.2",
    )

    monkeypatch.setattr(TaildropWindow, "_start_worker", lambda self: None)
    monkeypatch.setattr(TaildropWindow, "refresh_peers", lambda self: None)

    app = Adw.Application(
        application_id="io.github.taildrop_gui.Test",
        flags=GioRepo.ApplicationFlags.NON_UNIQUE,
    )
    app.register(None)
    win = TaildropWindow(app)
    try:
        win._apply_peers([fake_peer], None)

        assert win.send_btn.get_sensitive() is False
        assert win.send_btn.get_label() == "Send"

        # Dropping/adding files stages them without putting them in the transfer queue
        win.enqueue_paths([sample])
        assert len(win._transfer_items) == 1
        assert win._transfer_items[0].state == "staged"
        assert win._transfer_items[0].status_label.get_text() == "Ready"
        assert win._queue.empty() is True
        assert win.send_btn.get_sensitive() is True
        assert win.send_btn.get_label() == "Send (1)"

        # Removing a staged item clears it without sending
        win._cancel_item(win._transfer_items[0])
        assert len(win._transfer_items) == 0
        assert win.send_btn.get_sensitive() is False
        assert win.send_btn.get_label() == "Send"

        # Stage again and click Send
        win.enqueue_paths([sample])
        win.send_staged_items()
        assert len(win._transfer_items) == 1
        assert win._transfer_items[0].state == "queued"
        assert win._transfer_items[0].status_label.get_text() == "Queued"
        assert win._queue.qsize() == 1
        assert win.send_btn.get_sensitive() is False
        assert win.send_btn.get_label() == "Send"
    finally:
        win.close()
