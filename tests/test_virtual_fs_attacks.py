"""Adversarial virtual-filesystem, URI, and drop-parser security tests.

All tests target local temporary objects in ``tmp_path`` and mock
``subprocess.Popen`` (plus GTK window background workers) so nothing ever
communicates with or transfers files to real Tailscale devices.
"""

from __future__ import annotations

import itertools
import os
from pathlib import Path
import socket
import sys
import zipfile

for _dist in ("/usr/lib/python3/dist-packages", "/usr/local/lib/python3/dist-packages"):
    if _dist not in sys.path and os.path.isdir(_dist):
        sys.path.append(_dist)

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio  # noqa: E402

from taildrop_gui.app import TaildropWindow  # noqa: E402
from taildrop_gui.backend import (  # noqa: E402
    PeerDevice,
    _uri_or_path_to_path,
    extract_paths_from_drop,
    format_item_size,
    prepare_sendable_file,
    send_file_to_peer,
)


def _mock_popen(monkeypatch):
    """Replace subprocess.Popen with an in-memory recorder; never invokes real tailscale."""
    recorded_cmds: list[list[str]] = []

    class FakeProc:
        def __init__(self, cmd: list[str], **_kwargs) -> None:
            recorded_cmds.append(list(cmd))
            self.returncode = 0

        def poll(self) -> int:
            return 0

        def communicate(self) -> tuple[str, str]:
            return ("", "")

    monkeypatch.setattr("taildrop_gui.backend.subprocess.Popen", FakeProc)
    return recorded_cmds


_WINDOW_COUNTER = itertools.count(1)


def _make_test_window(monkeypatch) -> TaildropWindow:
    """Create an isolated TaildropWindow with background threads disabled."""
    monkeypatch.setattr(TaildropWindow, "_start_worker", lambda self: None)
    monkeypatch.setattr(TaildropWindow, "refresh_peers", lambda self: None)

    app = Adw.Application(
        application_id=f"io.github.taildrop_gui.TestFsAttacks.w{next(_WINDOW_COUNTER)}",
        flags=Gio.ApplicationFlags.NON_UNIQUE,
    )
    app.register(None)
    win = TaildropWindow(app)
    fake_peer = PeerDevice(
        hostname="Virtual Mac",
        dns_name="virtual-mac.example.ts.net",
        short_name="virtual-mac",
        os_name="macOS",
        online=True,
        ipv4="100.64.0.99",
    )
    win._apply_peers([fake_peer], None)
    return win


# ---------------------------------------------------------------------------
# 1. Top-level symlink bypass tests
# ---------------------------------------------------------------------------


def test_send_file_to_peer_rejects_top_level_file_symlink(tmp_path: Path, monkeypatch) -> None:
    """Top-level symlink to a regular file must NOT bypass prepare_sendable_file."""
    sensitive = tmp_path / "sensitive_key.txt"
    sensitive.write_text("SECRET_PRIVATE_KEY", encoding="utf-8")

    evil_link = tmp_path / "innocent.txt"
    evil_link.symlink_to(sensitive)

    recorded_cmds = _mock_popen(monkeypatch)

    ok, msg = send_file_to_peer(
        evil_link,
        "virtual-mac",
        tailscale_bin=str(tmp_path / "virtual-tailscale"),
    )
    assert ok is False
    assert "symlink" in msg.lower()
    assert recorded_cmds == []


def test_send_file_to_peer_rejects_top_level_dir_and_broken_symlinks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Top-level directory symlinks and dangling symlinks must be rejected safely."""
    target_dir = tmp_path / "secret_dir"
    target_dir.mkdir()
    (target_dir / "creds.txt").write_text("token", encoding="utf-8")

    dir_link = tmp_path / "dir_link"
    dir_link.symlink_to(target_dir)

    broken_link = tmp_path / "broken_link"
    broken_link.symlink_to(tmp_path / "does_not_exist")

    recorded_cmds = _mock_popen(monkeypatch)

    for candidate in (dir_link, broken_link):
        ok, _msg = send_file_to_peer(
            candidate,
            "virtual-mac",
            tailscale_bin=str(tmp_path / "virtual-tailscale"),
        )
        assert ok is False
    assert recorded_cmds == []


def test_extract_paths_from_drop_rejects_top_level_symlinks(tmp_path: Path) -> None:
    """Dropped symlinks (via URI, plain path, Gio.File, or Gdk.FileList) must be ignored."""
    sensitive = tmp_path / "secret.txt"
    sensitive.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(sensitive)

    assert _uri_or_path_to_path(str(link)) is None
    assert _uri_or_path_to_path(link.as_uri()) is None
    assert extract_paths_from_drop(f"{link.as_uri()}\n{link}\n") == []

    gfile = Gio.File.new_for_path(str(link))
    assert extract_paths_from_drop(gfile) == []

    gfile_list = Gdk.FileList.new_from_list([gfile])
    assert extract_paths_from_drop(gfile_list) == []


def test_window_rejects_top_level_symlink_drop_and_enqueue(tmp_path: Path, monkeypatch) -> None:
    """TaildropWindow must not stage symlinks via drop or direct enqueue_paths."""
    sensitive = tmp_path / "secret.txt"
    sensitive.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(sensitive)

    win = _make_test_window(monkeypatch)
    try:
        accepted = win._on_drop(win.drop_target, link.as_uri(), 0.0, 0.0)
        assert accepted is False
        assert win._transfer_items == []
        assert win.send_btn.get_sensitive() is False

        win.enqueue_paths([link])
        assert win._transfer_items == []
        assert win.send_btn.get_sensitive() is False
    finally:
        win.close()


def test_send_folder_skips_nested_symlinks(tmp_path: Path, monkeypatch) -> None:
    """Zipped directories sent to a virtual peer must omit nested symlinks."""
    secret = tmp_path / "outside_secret.txt"
    secret.write_text("do-not-leak", encoding="utf-8")

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "ok.txt").write_text("safe", encoding="utf-8")
    (bundle / "leak.txt").symlink_to(secret)

    seen_zip_names: list[str] = []

    class InspectZipProc:
        def __init__(self, cmd: list[str], **_kwargs) -> None:
            with zipfile.ZipFile(cmd[3], "r") as zf:
                seen_zip_names.extend(zf.namelist())
            self.returncode = 0

        def poll(self) -> int:
            return 0

        def communicate(self) -> tuple[str, str]:
            return ("", "")

    monkeypatch.setattr("taildrop_gui.backend.subprocess.Popen", InspectZipProc)

    ok, _msg = send_file_to_peer(
        bundle,
        "virtual-mac",
        tailscale_bin=str(tmp_path / "virtual-tailscale"),
    )
    assert ok is True
    assert "bundle/ok.txt" in seen_zip_names
    assert "bundle/leak.txt" not in seen_zip_names


# ---------------------------------------------------------------------------
# 2. Special file DoS tests (FIFO, Unix socket, device node)
# ---------------------------------------------------------------------------


def test_send_file_to_peer_rejects_fifo_without_invoking_popen(tmp_path: Path, monkeypatch) -> None:
    """Named pipes (FIFOs) would hang `tailscale file cp` forever and must be rejected."""
    fifo = tmp_path / "hang.fifo"
    os.mkfifo(fifo)

    recorded_cmds = _mock_popen(monkeypatch)

    ok, msg = send_file_to_peer(
        fifo,
        "virtual-mac",
        tailscale_bin=str(tmp_path / "virtual-tailscale"),
    )
    assert ok is False
    assert msg
    assert recorded_cmds == []


def test_send_file_to_peer_rejects_unix_socket_and_device(tmp_path: Path, monkeypatch) -> None:
    """Unix domain sockets and device nodes are not regular files and must be rejected."""
    sock_path = tmp_path / "ipc.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(sock_path))
    try:
        recorded_cmds = _mock_popen(monkeypatch)

        ok_sock, _ = send_file_to_peer(
            sock_path,
            "virtual-mac",
            tailscale_bin=str(tmp_path / "virtual-tailscale"),
        )
        assert ok_sock is False
        assert recorded_cmds == []

        dev_null = Path("/dev/null")
        if dev_null.exists():
            ok_dev, _ = send_file_to_peer(
                dev_null,
                "virtual-mac",
                tailscale_bin=str(tmp_path / "virtual-tailscale"),
            )
            assert ok_dev is False
            assert recorded_cmds == []
    finally:
        sock.close()


def test_extract_paths_from_drop_rejects_fifo_socket_and_device(tmp_path: Path) -> None:
    """Drop parser must reject FIFOs, sockets, and device nodes across all input types."""
    fifo = tmp_path / "drop.fifo"
    os.mkfifo(fifo)

    sock_path = tmp_path / "drop.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(sock_path))
    try:
        assert _uri_or_path_to_path(str(fifo)) is None
        assert _uri_or_path_to_path(fifo.as_uri()) is None
        assert _uri_or_path_to_path(str(sock_path)) is None
        assert extract_paths_from_drop(f"{fifo.as_uri()}\n{sock_path}\n") == []

        fifo_gfile = Gio.File.new_for_path(str(fifo))
        sock_gfile = Gio.File.new_for_path(str(sock_path))
        assert extract_paths_from_drop(fifo_gfile) == []
        assert extract_paths_from_drop(Gdk.FileList.new_from_list([fifo_gfile, sock_gfile])) == []

        if Path("/dev/null").exists():
            assert extract_paths_from_drop("/dev/null") == []
    finally:
        sock.close()


def test_window_rejects_fifo_and_socket_in_drop_and_enqueue(tmp_path: Path, monkeypatch) -> None:
    """TaildropWindow must refuse to stage FIFOs or Unix sockets."""
    fifo = tmp_path / "ui.fifo"
    os.mkfifo(fifo)

    sock_path = tmp_path / "ui.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(sock_path))
    win = _make_test_window(monkeypatch)
    try:
        assert win._on_drop(win.drop_target, str(fifo), 0.0, 0.0) is False
        assert win._transfer_items == []

        win.enqueue_paths([fifo, sock_path])
        assert win._transfer_items == []
        assert win.send_btn.get_sensitive() is False
    finally:
        win.close()
        sock.close()


def test_send_folder_skips_nested_fifo_and_socket(tmp_path: Path, monkeypatch) -> None:
    """Zipping a folder containing a FIFO/socket must skip them without blocking."""
    folder = tmp_path / "mixed_dir"
    folder.mkdir()
    (folder / "normal.txt").write_text("ok", encoding="utf-8")
    os.mkfifo(folder / "inner.fifo")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(folder / "inner.sock"))
    seen_zip_names: list[str] = []

    class InspectZipProc:
        def __init__(self, cmd: list[str], **_kwargs) -> None:
            with zipfile.ZipFile(cmd[3], "r") as zf:
                seen_zip_names.extend(zf.namelist())
            self.returncode = 0

        def poll(self) -> int:
            return 0

        def communicate(self) -> tuple[str, str]:
            return ("", "")

    monkeypatch.setattr("taildrop_gui.backend.subprocess.Popen", InspectZipProc)
    try:
        ok, _ = send_file_to_peer(
            folder,
            "virtual-mac",
            tailscale_bin=str(tmp_path / "virtual-tailscale"),
        )
        assert ok is True
        assert seen_zip_names == ["mixed_dir/normal.txt"]
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# 3. URI & drop parser attacks
# ---------------------------------------------------------------------------


def test_uri_parser_rejects_remote_file_netlocs_rfc8089(tmp_path: Path) -> None:
    """RFC 8089: non-local file:// hosts (other than '' or 'localhost') must be rejected."""
    local_target = tmp_path / "target.txt"
    local_target.write_text("local content", encoding="utf-8")

    hostile_uris = [
        f"file://evil-host{local_target}",
        f"file://192.0.2.10{local_target}",
        f"file://attacker@localhost{local_target}",
        f"file://localhost:8080{local_target}",
        f"FILE://evil-host{local_target}",
    ]
    for uri in hostile_uris:
        assert _uri_or_path_to_path(uri) is None, f"Should reject remote URI {uri}"
        assert extract_paths_from_drop(uri) == [], f"Should reject remote URI {uri}"

    # Control: valid local RFC 8089 URIs remain accepted for regular files
    assert _uri_or_path_to_path(local_target.as_uri()) == local_target
    assert _uri_or_path_to_path(f"file://localhost{local_target}") == local_target


def test_window_drop_rejects_remote_file_uri(tmp_path: Path, monkeypatch) -> None:
    """Dropping file://evil-host/path onto TaildropWindow must be rejected."""
    local_target = tmp_path / "target.txt"
    local_target.write_text("local content", encoding="utf-8")

    win = _make_test_window(monkeypatch)
    try:
        accepted = win._on_drop(
            win.drop_target,
            f"file://evil-host{local_target}",
            0.0,
            0.0,
        )
        assert accepted is False
        assert win._transfer_items == []
    finally:
        win.close()


def test_embedded_null_bytes_handled_without_value_error(tmp_path: Path, monkeypatch) -> None:
    """Embedded null bytes (%00 or \\x00) must never raise unhandled ValueError."""
    valid = tmp_path / "valid.txt"
    valid.write_text("ok", encoding="utf-8")

    assert _uri_or_path_to_path(f"file://{tmp_path}/a%00b") is None
    assert _uri_or_path_to_path(f"file://{valid}%00.txt") is None
    assert _uri_or_path_to_path(f"{tmp_path}/a\x00b") is None

    payload = f"file://{tmp_path}/a%00b\n{tmp_path}/x\x00y\n{valid.as_uri()}\n"
    assert extract_paths_from_drop(payload) == [valid]

    class FakeNullGFile:
        def get_path(self) -> str:
            return f"{tmp_path}/a\x00b"

    assert extract_paths_from_drop(FakeNullGFile()) == []

    null_path = Path(f"{tmp_path}/a\x00b")
    # format_item_size previously crashed with ValueError: stat: embedded null character
    assert format_item_size(null_path) == ""

    recorded_cmds = _mock_popen(monkeypatch)
    ok, _msg = send_file_to_peer(
        null_path,
        "virtual-mac",
        tailscale_bin=str(tmp_path / "virtual-tailscale"),
    )
    assert ok is False
    assert recorded_cmds == []

    try:
        prepare_sendable_file(null_path, tmp_path)
    except (FileNotFoundError, ValueError):
        pass

    win = _make_test_window(monkeypatch)
    try:
        win.enqueue_paths([null_path])
        assert win._transfer_items == []
    finally:
        win.close()


def test_circular_symlink_loops_do_not_crash_or_send(tmp_path: Path, monkeypatch) -> None:
    """Circular symlink loops (ELOOP) must be ignored/rejected without crashing."""
    loop_a = tmp_path / "loop_a"
    loop_b = tmp_path / "loop_b"
    loop_a.symlink_to(loop_b)
    loop_b.symlink_to(loop_a)

    self_loop = tmp_path / "self_loop"
    self_loop.symlink_to(self_loop)

    valid = tmp_path / "valid.txt"
    valid.write_text("ok", encoding="utf-8")

    assert _uri_or_path_to_path(str(loop_a)) is None
    assert _uri_or_path_to_path(loop_a.as_uri()) is None
    assert _uri_or_path_to_path(str(self_loop)) is None
    assert extract_paths_from_drop(f"{loop_a}\n{self_loop.as_uri()}\n{valid}\n") == [valid]

    gfile_loop = Gio.File.new_for_path(str(loop_a))
    assert extract_paths_from_drop(gfile_loop) == []
    assert extract_paths_from_drop(Gdk.FileList.new_from_list([gfile_loop])) == []

    assert format_item_size(loop_a) == ""

    recorded_cmds = _mock_popen(monkeypatch)
    for bad in (loop_a, self_loop):
        ok, _msg = send_file_to_peer(
            bad,
            "virtual-mac",
            tailscale_bin=str(tmp_path / "virtual-tailscale"),
        )
        assert ok is False
    assert recorded_cmds == []

    win = _make_test_window(monkeypatch)
    try:
        win.enqueue_paths([loop_a, self_loop])
        assert win._transfer_items == []
    finally:
        win.close()


def test_dash_prefixed_relative_drop_rejected_and_absolute_dash_safe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Relative `-flag` drop strings must be rejected; real absolute `-flag` files stay safe."""
    dash_file = tmp_path / "--targets"
    dash_file.write_text("payload", encoding="utf-8")
    rf_file = tmp_path / "-rf"
    rf_file.write_text("payload2", encoding="utf-8")

    monkeypatch.chdir(tmp_path)

    # Relative flag-like strings in a drop payload must not resolve against CWD
    assert _uri_or_path_to_path("--targets") is None
    assert _uri_or_path_to_path("-rf") is None
    assert extract_paths_from_drop("--targets\n-rf\n") == []

    win = _make_test_window(monkeypatch)
    try:
        assert win._on_drop(win.drop_target, "--targets", 0.0, 0.0) is False
        assert win._transfer_items == []
    finally:
        win.close()

    # An actual absolute file whose basename starts with '-' must be passed as an
    # absolute path (never bare '--targets') to tailscale file cp
    assert extract_paths_from_drop(str(dash_file)) == [dash_file]
    recorded_cmds = _mock_popen(monkeypatch)
    ok, _msg = send_file_to_peer(
        dash_file,
        "virtual-mac",
        tailscale_bin=str(tmp_path / "virtual-tailscale"),
    )
    assert ok is True
    assert len(recorded_cmds) == 1
    sent_arg = recorded_cmds[0][3]
    assert not sent_arg.startswith("-")
    assert Path(sent_arg).is_absolute()

    # Even if a relative Path("--targets") is passed directly to send_file_to_peer,
    # it must either be rejected or resolved to an absolute path not starting with '-'
    recorded_cmds.clear()
    ok_rel, _ = send_file_to_peer(
        Path("--targets"),
        "virtual-mac",
        tailscale_bin=str(tmp_path / "virtual-tailscale"),
    )
    if ok_rel:
        assert len(recorded_cmds) == 1
        assert not recorded_cmds[0][3].startswith("-")
        assert Path(recorded_cmds[0][3]).is_absolute()


def test_drop_parser_rejects_filesystem_root(tmp_path: Path, monkeypatch) -> None:
    """Dropping `/` or `file:///` has no filename and must be rejected."""
    assert _uri_or_path_to_path("/") is None
    assert _uri_or_path_to_path("file:///") is None
    assert extract_paths_from_drop("file:///\n/\n") == []

    win = _make_test_window(monkeypatch)
    try:
        assert win._on_drop(win.drop_target, "file:///", 0.0, 0.0) is False
        assert win._transfer_items == []
        win.enqueue_paths([Path("/")])
        assert win._transfer_items == []
    finally:
        win.close()
