# Security Policy

## Overview

`taildrop-gui` undergoes automated red-team and blue-team security verification in an isolated sandbox using virtual Tailscale peers, a mocked `tailscale` CLI stub, and temporary local filesystems. No security verification test ever communicates with or sends files to real connected Tailscale devices.

## Red-Team Verification Methodology

The security test suite evaluates three boundaries across the application:

- **Virtual Peer Simulation:** Exercises peer discovery and transfer dispatch against synthetic Tailnet payloads and an isolated mock CLI executable to verify strict schema, type, and target allowlist enforcement.
- **Filesystem and Archive Boundary Testing:** Exercises drag-and-drop ingestion, URI parsing, and directory archiving inside temporary directories to verify that only valid regular files and standard directories are staged or compressed.
- **UI and Subprocess Isolation:** Verifies that external CLI calls never invoke a shell and that untrusted strings from peer metadata, filenames, or CLI status output are sanitized before being rendered in GTK4 and Libadwaita widgets.

## Verified Security Invariants

- **Strict Target Allowlisting:** Peer identifiers and network addresses are validated against strict ASCII hostname and IP address rules prior to staging and transfer.
- **Safe File and Archive Handling:** Symbolic links, special filesystem objects (FIFOs, Unix sockets, device nodes), and non-local `file://` URIs are rejected during drag-and-drop staging, temporary `.zip` creation, and transfer execution.
- **Direct Subprocess Execution:** External `tailscale` commands are invoked strictly with argument arrays (`shell=False`) and resolved absolute file paths.
- **Sanitized Plain-Text UI Rendering:** Control and directional-override characters are stripped from displayed labels and notifications, and Pango markup parsing is disabled on banners and toasts.
- **Zero Runtime PyPI Dependencies:** The application relies solely on the Python standard library, system GTK4/Libadwaita bindings, and the local `tailscale` CLI.

## Running the Verification Suite

```bash
uv run pytest -v
```

All 113 automated unit and adversarial regression checks run locally and in CI on every push and pull request.

## Reporting a Vulnerability

If you discover a security issue, please report it privately through [GitHub Security Advisories](https://github.com/ThaminduMiuranda/taildrop-gui/security/advisories/new) rather than opening a public issue.
