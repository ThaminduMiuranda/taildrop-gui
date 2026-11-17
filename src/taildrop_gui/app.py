"""GTK4 + Libadwaita drag-and-drop GUI for sending files from Ubuntu to Mac via Taildrop."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import queue
import sys
import threading

for _dist in ("/usr/lib/python3/dist-packages", "/usr/local/lib/python3/dist-packages"):
    if _dist not in sys.path and os.path.isdir(_dist):
        sys.path.append(_dist)

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from taildrop_gui.backend import (  # noqa: E402
    PeerDevice,
    discover_peers,
    extract_paths_from_drop,
    format_item_size,
    sanitize_display_text,
    send_file_to_peer,
    validate_local_path,
)

APP_ID = "io.github.taildrop_gui.TaildropToMac"

DROPZONE_CSS = """
.drop-zone {
    border: 2px dashed alpha(currentColor, 0.28);
    border-radius: 16px;
    background-color: alpha(currentColor, 0.03);
    transition: all 160ms ease-in-out;
}
.drop-zone.drop-active {
    border-color: @accent_color;
    background-color: alpha(@accent_bg_color, 0.14);
}
.status-ok {
    color: @success_color;
    font-weight: 600;
}
.status-err {
    color: @error_color;
    font-weight: 600;
}
.status-muted {
    opacity: 0.72;
}
"""


@dataclass
class TransferItem:
    """Represents a single staged, queued, active, or completed file transfer."""

    path: Path
    row: Adw.ActionRow
    status_label: Gtk.Label
    spinner: Gtk.Spinner
    cancel_btn: Gtk.Button
    target_peer: PeerDevice | None = None
    size_text: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event)
    state: str = "staged"  # staged | queued | running | done | failed | cancelled


class TaildropWindow(Adw.ApplicationWindow):
    """Main application window with target selector, drop zone, and transfer queue."""

    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title="Taildrop to Mac")
        self.set_default_size(560, 620)
        self.set_size_request(440, 500)

        self.peers: list[PeerDevice] = []
        self._transfer_items: list[TransferItem] = []
        self._queue: queue.Queue[TransferItem] = queue.Queue()
        self._peers_loaded = False

        self._install_css()
        self._build_ui()
        self._start_worker()
        self.refresh_peers()

    def _install_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(DROPZONE_CSS.encode("utf-8"))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    def _build_ui(self) -> None:
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        toolbar_view = Adw.ToolbarView()
        self.toast_overlay.set_child(toolbar_view)

        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        self.refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.refresh_btn.set_tooltip_text("Refresh Tailscale devices")
        self.refresh_btn.connect("clicked", lambda *_: self.refresh_peers())
        header.pack_start(self.refresh_btn)

        self.open_btn = Gtk.Button.new_from_icon_name("document-open-symbolic")
        self.open_btn.set_tooltip_text("Choose files to send")
        self.open_btn.connect("clicked", lambda *_: self._open_file_picker())
        header.pack_end(self.open_btn)

        # Main scrollable body
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        toolbar_view.set_content(main_box)

        self.banner = Adw.Banner()
        self.banner.set_use_markup(False)
        self.banner.set_button_label("Retry")
        self.banner.connect("button-clicked", lambda *_: self.refresh_peers())
        main_box.append(self.banner)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        main_box.append(scrolled)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(640)
        clamp.set_margin_top(16)
        clamp.set_margin_bottom(20)
        clamp.set_margin_start(16)
        clamp.set_margin_end(16)
        scrolled.set_child(clamp)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        clamp.set_child(content_box)

        # Target selector group
        target_group = Adw.PreferencesGroup()
        target_group.set_title("Destination")
        content_box.append(target_group)

        self.target_row = Adw.ActionRow()
        self.target_row.set_title("Send To")
        self.target_row.set_subtitle("Discovering Tailscale peers…")
        target_group.add(self.target_row)

        self.peer_model = Gtk.StringList.new(["Loading devices…"])
        self.peer_dropdown = Gtk.DropDown.new(self.peer_model, None)
        self.peer_dropdown.set_valign(Gtk.Align.CENTER)
        self.peer_dropdown.set_sensitive(False)
        self.peer_dropdown.connect("notify::selected", self._on_peer_selected)
        self.target_row.add_suffix(self.peer_dropdown)

        # Drag-and-drop zone
        self.drop_frame = Gtk.Frame()
        self.drop_frame.add_css_class("drop-zone")
        self.drop_frame.set_vexpand(False)
        content_box.append(self.drop_frame)

        drop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        drop_box.set_margin_top(32)
        drop_box.set_margin_bottom(32)
        drop_box.set_margin_start(24)
        drop_box.set_margin_end(24)
        drop_box.set_valign(Gtk.Align.CENTER)
        drop_box.set_halign(Gtk.Align.CENTER)
        self.drop_frame.set_child(drop_box)

        drop_icon = Gtk.Image.new_from_icon_name("send-to-symbolic")
        drop_icon.set_pixel_size(52)
        drop_icon.add_css_class("dim-label")
        drop_box.append(drop_icon)

        drop_title = Gtk.Label(label="Drag & Drop Files or Folders Here")
        drop_title.add_css_class("title-2")
        drop_title.set_wrap(True)
        drop_title.set_justify(Gtk.Justification.CENTER)
        drop_box.append(drop_title)

        drop_subtitle = Gtk.Label(
            label="Drop any file or folder to stage it, then click Send"
        )
        drop_subtitle.add_css_class("dim-label")
        drop_subtitle.set_wrap(True)
        drop_subtitle.set_justify(Gtk.Justification.CENTER)
        drop_box.append(drop_subtitle)

        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        button_box.set_halign(Gtk.Align.CENTER)
        button_box.set_margin_top(6)
        drop_box.append(button_box)

        select_files_btn = Gtk.Button(label="Select Files…")
        select_files_btn.add_css_class("pill")
        select_files_btn.connect("clicked", lambda *_: self._open_file_picker())
        button_box.append(select_files_btn)

        select_folder_btn = Gtk.Button(label="Select Folder…")
        select_folder_btn.add_css_class("pill")
        select_folder_btn.connect("clicked", lambda *_: self._open_folder_picker())
        button_box.append(select_folder_btn)

        # Attach GTK4 DropTarget
        self.drop_target = Gtk.DropTarget.new(GObject.TYPE_NONE, Gdk.DragAction.COPY)
        self.drop_target.set_gtypes([Gdk.FileList, Gio.File, GObject.TYPE_STRING])
        self.drop_target.connect("enter", self._on_drop_enter)
        self.drop_target.connect("motion", self._on_drop_motion)
        self.drop_target.connect("leave", self._on_drop_leave)
        self.drop_target.connect("drop", self._on_drop)
        self.drop_frame.add_controller(self.drop_target)

        # Primary Send button
        self.send_btn = Gtk.Button(label="Send")
        self.send_btn.add_css_class("suggested-action")
        self.send_btn.add_css_class("pill")
        self.send_btn.set_halign(Gtk.Align.FILL)
        self.send_btn.set_sensitive(False)
        self.send_btn.connect("clicked", lambda *_: self.send_staged_items())
        content_box.append(self.send_btn)

        # Transfer queue & history group
        self.queue_group = Adw.PreferencesGroup()
        self.queue_group.set_title("Transfers")
        content_box.append(self.queue_group)

        clear_btn = Gtk.Button(label="Clear Finished")
        clear_btn.add_css_class("flat")
        clear_btn.connect("clicked", lambda *_: self._clear_finished())
        self.queue_group.set_header_suffix(clear_btn)

        self.queue_list = Gtk.ListBox()
        self.queue_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.queue_list.add_css_class("boxed-list")
        self.queue_group.add(self.queue_list)

        self.empty_row = Adw.ActionRow()
        self.empty_row.set_title("No transfers yet")
        self.empty_row.set_subtitle("Dropped files and folders will appear here")
        self.empty_row.set_sensitive(False)
        self.queue_list.append(self.empty_row)

    def _on_drop_enter(self, _target: Gtk.DropTarget, _x: float, _y: float) -> Gdk.DragAction:
        self.drop_frame.add_css_class("drop-active")
        return Gdk.DragAction.COPY

    def _on_drop_motion(self, _target: Gtk.DropTarget, _x: float, _y: float) -> Gdk.DragAction:
        return Gdk.DragAction.COPY

    def _on_drop_leave(self, _target: Gtk.DropTarget) -> None:
        self.drop_frame.remove_css_class("drop-active")

    def _on_drop(self, _target: Gtk.DropTarget, value: object, _x: float, _y: float) -> bool:
        self.drop_frame.remove_css_class("drop-active")
        if hasattr(value, "get_value"):
            value = value.get_value()
        paths = extract_paths_from_drop(value)
        if not paths:
            self._show_toast("Could not read any local files from drop")
            return False
        self.enqueue_paths(paths)
        return True

    def refresh_peers(self) -> None:
        """Discover Tailscale peers on a background thread and update UI."""
        self.refresh_btn.set_sensitive(False)

        def _worker() -> None:
            try:
                peers, err = discover_peers()
            except Exception as exc:  # pragma: no cover - defensive fallback
                peers, err = [], f"Failed to discover Tailscale peers: {sanitize_display_text(str(exc))}"
            GLib.idle_add(self._apply_peers, peers, err)

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_peers(self, peers: list[PeerDevice], error: str | None) -> bool:
        self.refresh_btn.set_sensitive(True)
        self.peers = peers
        self._peers_loaded = True

        if error or not peers:
            safe_error = sanitize_display_text(error) if error else ""
            self.banner.set_title(safe_error or "No Taildrop peers available.")
            self.banner.set_revealed(True)
            self.peer_model.splice(0, self.peer_model.get_n_items(), ["No peers found"])
            self.peer_dropdown.set_sensitive(False)
            self.target_row.set_subtitle("Unavailable")
            self._update_staged_subtitles()
            self._update_send_button()
            return GLib.SOURCE_REMOVE

        self.banner.set_revealed(False)
        labels = [sanitize_display_text(peer.display_label) for peer in peers]
        self.peer_model.splice(0, self.peer_model.get_n_items(), labels)
        self.peer_dropdown.set_selected(0)
        self.peer_dropdown.set_sensitive(True)
        self._update_target_subtitle()
        self._update_staged_subtitles()
        self._update_send_button()

        return GLib.SOURCE_REMOVE

    def _on_peer_selected(self, _dropdown: Gtk.DropDown, _pspec: object) -> None:
        self._update_target_subtitle()
        self._update_staged_subtitles()
        self._update_send_button()

    def _update_target_subtitle(self) -> None:
        peer = self.selected_peer()
        if peer is None:
            self.target_row.set_subtitle("No device selected")
            return
        parts = []
        safe_ipv4 = sanitize_display_text(peer.ipv4)
        if safe_ipv4:
            parts.append(safe_ipv4)
        safe_os = sanitize_display_text(peer.os_name)
        if safe_os:
            parts.append(safe_os)
        parts.append("Online" if peer.online else "Offline")
        self.target_row.set_subtitle(GLib.markup_escape_text(" • ".join(parts), -1))

    def selected_peer(self) -> PeerDevice | None:
        """Return the currently selected PeerDevice, or None."""
        idx = self.peer_dropdown.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self.peers):
            return None
        return self.peers[idx]

    def _open_file_picker(self) -> None:
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Select Files to Send via Taildrop")
        dialog.open_multiple(self, None, self._on_files_picked)

    def _on_files_picked(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            file_list = dialog.open_multiple_finish(result)
        except GLib.Error:
            return
        paths: list[Path] = []
        for idx in range(file_list.get_n_items()):
            gfile = file_list.get_item(idx)
            if hasattr(gfile, "get_path"):
                p = gfile.get_path()
                if p:
                    paths.append(Path(p))
        if paths:
            self.enqueue_paths(paths)

    def _open_folder_picker(self) -> None:
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Select Folder to Zip and Send via Taildrop")
        dialog.select_folder(self, None, self._on_folder_picked)

    def _on_folder_picked(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            gfile = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if gfile is not None and hasattr(gfile, "get_path"):
            p = gfile.get_path()
            if p:
                self.enqueue_paths([Path(p)])

    def _format_row_subtitle(self, size_text: str, peer: PeerDevice | None) -> str:
        safe_size = sanitize_display_text(size_text)
        if peer is not None:
            safe_host = sanitize_display_text(peer.hostname) or "Unknown Peer"
            return f"{safe_size} → {safe_host}" if safe_size else f"→ {safe_host}"
        return safe_size or "Waiting for target device…"

    def _update_staged_subtitles(self) -> None:
        peer = self.selected_peer()
        for item in self._transfer_items:
            if item.state == "staged":
                item.target_peer = peer
                subtitle = self._format_row_subtitle(item.size_text, peer)
                item.row.set_subtitle(GLib.markup_escape_text(subtitle, -1))

    def _update_send_button(self) -> None:
        staged_count = sum(1 for item in self._transfer_items if item.state == "staged")
        peer = self.selected_peer()
        has_peer = peer is not None and bool(peer.target_id)
        if staged_count > 0:
            self.send_btn.set_label(f"Send ({staged_count})")
            self.send_btn.set_sensitive(has_peer)
        else:
            self.send_btn.set_label("Send")
            self.send_btn.set_sensitive(False)

    def enqueue_paths(self, paths: list[Path]) -> None:
        """Stage dropped or picked paths so the user can review and click Send."""
        valid_items: list[tuple[Path, bool]] = []
        for path in paths:
            ok, _err, is_dir = validate_local_path(path, require_absolute=True)
            if ok:
                valid_items.append((path, is_dir))
        if not valid_items:
            self._show_toast("No valid files found to send.")
            return

        if self.empty_row.get_parent() is not None:
            self.queue_list.remove(self.empty_row)

        peer = self.selected_peer()
        for path, is_dir in valid_items:
            row = Adw.ActionRow()
            icon_name = "folder-symbolic" if is_dir else "text-x-generic-symbolic"
            icon = Gtk.Image.new_from_icon_name(icon_name)
            row.add_prefix(icon)

            safe_name = sanitize_display_text(path.name) or "File"
            row.set_title(GLib.markup_escape_text(safe_name, -1))
            size_text = format_item_size(path)
            subtitle = self._format_row_subtitle(size_text, peer)
            row.set_subtitle(GLib.markup_escape_text(subtitle, -1))

            suffix_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            suffix_box.set_valign(Gtk.Align.CENTER)

            spinner = Gtk.Spinner()
            spinner.set_visible(False)
            suffix_box.append(spinner)

            status_label = Gtk.Label(label="Ready")
            status_label.add_css_class("status-muted")
            suffix_box.append(status_label)

            cancel_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
            cancel_btn.add_css_class("flat")
            cancel_btn.set_tooltip_text("Remove item")
            suffix_box.append(cancel_btn)

            row.add_suffix(suffix_box)

            item = TransferItem(
                path=path,
                target_peer=peer,
                size_text=size_text,
                row=row,
                status_label=status_label,
                spinner=spinner,
                cancel_btn=cancel_btn,
                state="staged",
            )
            cancel_btn.connect("clicked", lambda _b, it=item: self._cancel_item(it))

            self.queue_list.prepend(row)
            self._transfer_items.append(item)

        self._update_send_button()

    def send_staged_items(self) -> None:
        """Enqueue all staged items for Taildrop transfer."""
        peer = self.selected_peer()
        if peer is None or not peer.target_id:
            self._show_toast("No Tailscale target device is available.")
            return

        staged_items = [item for item in self._transfer_items if item.state == "staged"]
        if not staged_items:
            return

        for item in staged_items:
            item.target_peer = peer
            subtitle = self._format_row_subtitle(item.size_text, peer)
            item.row.set_subtitle(GLib.markup_escape_text(subtitle, -1))
            item.state = "queued"
            item.status_label.set_text("Queued")
            item.cancel_btn.set_icon_name("process-stop-symbolic")
            item.cancel_btn.set_tooltip_text("Cancel transfer")
            self._queue.put(item)

        self._update_send_button()

    def _cancel_item(self, item: TransferItem) -> None:
        if item.state == "staged":
            if item.row.get_parent() is not None:
                self.queue_list.remove(item.row)
            if item in self._transfer_items:
                self._transfer_items.remove(item)
            if not self._transfer_items and self.empty_row.get_parent() is None:
                self.queue_list.append(self.empty_row)
            self._update_send_button()
            return

        item.cancel_event.set()
        if item.state == "queued":
            item.state = "cancelled"
            item.status_label.set_text("Cancelled")
            item.cancel_btn.set_sensitive(False)
        else:
            item.status_label.set_text("Cancelling…")
            item.cancel_btn.set_sensitive(False)
        self._update_send_button()

    def _clear_finished(self) -> None:
        remaining: list[TransferItem] = []
        for item in self._transfer_items:
            if item.state in ("done", "failed", "cancelled"):
                if item.row.get_parent() is not None:
                    self.queue_list.remove(item.row)
            else:
                remaining.append(item)
        self._transfer_items = remaining

        if not self._transfer_items and self.empty_row.get_parent() is None:
            self.queue_list.append(self.empty_row)

    def _start_worker(self) -> None:
        def _worker_loop() -> None:
            while True:
                item = self._queue.get()
                try:
                    if item.cancel_event.is_set() or item.state == "cancelled":
                        GLib.idle_add(self._finish_item, item, False, "Cancelled")
                        continue

                    GLib.idle_add(self._mark_running, item)
                    target_id = item.target_peer.target_id if item.target_peer else ""
                    try:
                        ok, msg = send_file_to_peer(
                            item.path,
                            target_id,
                            cancel_event=item.cancel_event,
                            on_stage=lambda stage, it=item: GLib.idle_add(self._update_stage, it, stage),
                        )
                    except Exception as exc:  # pragma: no cover - defensive fallback
                        ok, msg = False, f"Transfer failed: {sanitize_display_text(str(exc))}"
                    GLib.idle_add(self._finish_item, item, ok, msg)
                finally:
                    self._queue.task_done()

        threading.Thread(target=_worker_loop, daemon=True).start()

    def _mark_running(self, item: TransferItem) -> bool:
        if item.state == "cancelled":
            return GLib.SOURCE_REMOVE
        item.state = "running"
        item.spinner.set_visible(True)
        item.spinner.start()
        item.status_label.set_text("Starting…")
        return GLib.SOURCE_REMOVE

    def _update_stage(self, item: TransferItem, stage: str) -> bool:
        if item.state == "running":
            item.status_label.set_text(sanitize_display_text(stage))
        return GLib.SOURCE_REMOVE

    def _finish_item(self, item: TransferItem, ok: bool, message: str) -> bool:
        item.spinner.stop()
        item.spinner.set_visible(False)
        item.cancel_btn.set_visible(False)
        item.status_label.remove_css_class("status-muted")

        peer_name = sanitize_display_text(item.target_peer.hostname) if item.target_peer else "target"
        peer_name = peer_name or "target"
        safe_filename = sanitize_display_text(item.path.name) or "file"
        safe_message = sanitize_display_text(message)
        if item.cancel_event.is_set() or message == "Cancelled":
            item.state = "cancelled"
            item.status_label.set_text("Cancelled")
            item.status_label.add_css_class("status-muted")
        elif ok:
            item.state = "done"
            item.status_label.set_text("Sent ✓")
            item.status_label.add_css_class("status-ok")
            self._show_toast(f"Sent {safe_filename} to {peer_name}")
        else:
            item.state = "failed"
            item.status_label.set_text("Failed ✗")
            item.status_label.add_css_class("status-err")
            item.row.set_subtitle(
                GLib.markup_escape_text(f"{peer_name} • {safe_message}", -1)
            )
            self._show_toast(f"Failed to send {safe_filename}: {safe_message}")

        return GLib.SOURCE_REMOVE

    def _show_toast(self, text: str) -> None:
        toast = Adw.Toast.new(sanitize_display_text(text))
        toast.set_use_markup(False)
        toast.set_timeout(4)
        self.toast_overlay.add_toast(toast)


class TaildropApp(Adw.Application):
    """Libadwaita application wrapper supporting CLI/desktop file opening and self-test."""

    def __init__(self, self_test: bool = False) -> None:
        flags = Gio.ApplicationFlags.HANDLES_OPEN
        if self_test:
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        self.self_test = self_test
        self.window: TaildropWindow | None = None

    def _ensure_window(self) -> TaildropWindow:
        if self.window is None:
            self.window = TaildropWindow(self)
        self.window.present()
        return self.window

    def do_activate(self) -> None:  # noqa: N802 (GTK vfunc)
        win = self._ensure_window()
        if self.self_test:
            GLib.timeout_add(300, self._run_self_test, win)

    def do_open(self, files: list[Gio.File], _n_files: int, _hint: str) -> None:  # noqa: N802
        win = self._ensure_window()
        paths = [Path(f.get_path()) for f in files if hasattr(f, "get_path") and f.get_path()]
        if paths:
            win.enqueue_paths(paths)
        if self.self_test:
            GLib.timeout_add(300, self._run_self_test, win)

    def _run_self_test(self, win: TaildropWindow) -> bool:
        # Wait briefly if background peer discovery is still in flight
        if not win._peers_loaded:
            return GLib.SOURCE_CONTINUE
        selected = win.selected_peer()
        selected_id = selected.target_id if selected else "none"
        controllers = list(win.drop_frame.observe_controllers())
        has_drop_target = any(isinstance(c, Gtk.DropTarget) for c in controllers)
        has_send_btn = hasattr(win, "send_btn") and isinstance(win.send_btn, Gtk.Button)
        print(
            f"SELF_TEST_OK: peers={len(win.peers)} selected={selected_id} "
            f"drop_target={has_drop_target} send_button={has_send_btn}"
        )
        self.quit()
        return GLib.SOURCE_REMOVE


def main(argv: list[str] | None = None) -> int:
    """Entry point for `taildrop-gui`."""
    raw_args = list(sys.argv[1:] if argv is None else argv)
    self_test = "--self-test" in raw_args
    file_args = [arg for arg in raw_args if arg != "--self-test"]

    app = TaildropApp(self_test=self_test)
    run_argv = [sys.argv[0], *file_args]
    return app.run(run_argv)


if __name__ == "__main__":
    raise SystemExit(main())
