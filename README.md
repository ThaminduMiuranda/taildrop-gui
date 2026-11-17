<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/banner-dark.svg">
  <img alt="Taildrop to Mac: Drop files and folders on Ubuntu, receive them on your Mac over Tailscale." src="docs/assets/banner-light.svg" width="100%">
</picture>

**A native Ubuntu GTK4 + Libadwaita app for sending files and folders to your Mac over Tailscale Taildrop.**

[![Release](https://img.shields.io/github/v/release/ThaminduMiuranda/taildrop-gui?style=flat-square&color=3584e4)](https://github.com/ThaminduMiuranda/taildrop-gui/releases) [![License: MIT](https://img.shields.io/github/license/ThaminduMiuranda/taildrop-gui?style=flat-square&color=2ec27e)](LICENSE) [![UI: GTK4 + Libadwaita](https://img.shields.io/badge/UI-GTK4%20%2B%20Libadwaita-334155?style=flat-square)](src/taildrop_gui/app.py) [![Tailscale](https://img.shields.io/badge/Tailscale-Taildrop-334155?style=flat-square)](https://tailscale.com/kb/1106/taildrop)

[Features](#features) · [Requirements](#requirements) · [Install](#install) · [Usage](#usage) · [Security](#security) · [License](#license)

</div>

## Features

- **Drag-and-drop staging:** Drop one or more files or folders into the window, review or remove items, and click **Send** when ready.
- **Automatic folder zipping:** Folders are automatically archived to a temporary `.zip` before sending (since `tailscale file cp` only sends individual files) and cleaned up afterward.
- **Automatic Mac discovery:** Finds devices on your Tailnet and prioritizes online macOS peers at the top of the list, while still allowing you to send to any connected device.
- **Transfer queue:** Shows live status for queued, zipping, sending, sent, failed, and cancelled transfers, with per-item cancellation.
- **Desktop and CLI support:** Open it from your Ubuntu application menu, drop files onto the window or launcher icon, or pass file paths from the terminal.

## Requirements

- **Tailscale** installed, running, and signed in to your Tailnet with Taildrop enabled.
- **Ubuntu / Debian** with GTK4 and Libadwaita Python bindings (installed automatically by the `.deb` package):

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
```

## Install

### Option 1: Ubuntu / Debian `.deb` package (recommended)

Download `taildrop-gui_0.1.0_all.deb` from the [latest release](https://github.com/ThaminduMiuranda/taildrop-gui/releases/latest) and install it:

```bash
sudo apt install ./taildrop-gui_0.1.0_all.deb
```

This installs the `taildrop-gui` command and adds **Taildrop to Mac** to your application launcher.

### Option 2: Standalone executable

Download `taildrop-gui` from the [latest release](https://github.com/ThaminduMiuranda/taildrop-gui/releases/latest), make it executable, and run it:

```bash
chmod +x taildrop-gui
./taildrop-gui
```

### Option 3: Run from source

```bash
git clone https://github.com/ThaminduMiuranda/taildrop-gui.git
cd taildrop-gui
uv run taildrop-gui
```

## Usage

1. Open **Taildrop to Mac** from your application menu (or run `taildrop-gui`).
2. Check the **Destination** dropdown — your online Mac is selected automatically if available.
3. Drag and drop files or folders onto the drop zone, or click **Select Files...** / **Select Folder...**.
4. Click **Send** to transfer the staged items.

You can also pre-stage files or folders directly from the terminal:

```bash
taildrop-gui /path/to/document.pdf /path/to/folder
```

## Security

`taildrop-gui` is verified with an automated red-team test suite running against simulated virtual peers and isolated temporary filesystems (113 checks covering target allowlisting, symlink and special-file rejection, subprocess argument safety, and UI text sanitization), with zero external PyPI runtime dependencies. See [`SECURITY.md`](SECURITY.md) for the security policy and reporting instructions.

## License

[MIT](LICENSE) © Thamindu Miuranda
