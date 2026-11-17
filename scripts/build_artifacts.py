#!/usr/bin/env python3
"""Build standalone executable (zipapp) and Ubuntu .deb package into dist/."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import zipapp


def _load_project_metadata(root: Path) -> tuple[str, str, str]:
    with (root / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    project = data["project"]
    return str(project["name"]), str(project["version"]), str(project["description"])


def build_zipapp(root: Path, dist_dir: Path, binary_name: str) -> Path:
    """Create a standalone Python zipapp executable in dist/."""
    exe_path = dist_dir / binary_name
    if exe_path.exists():
        exe_path.unlink()

    zipapp.create_archive(
        root / "src",
        target=exe_path,
        interpreter="/usr/bin/env python3",
        main="taildrop_gui.app:main",
        filter=lambda p: "__pycache__" not in p.parts and p.suffix not in (".pyc", ".pyo"),
        compressed=True,
    )
    os.chmod(exe_path, 0o755)
    return exe_path


def build_deb(
    root: Path,
    dist_dir: Path,
    package_name: str,
    version: str,
    description: str,
    exe_path: Path,
) -> Path:
    """Package the standalone executable, .desktop file, and license into a .deb."""
    deb_path = dist_dir / f"{package_name}_{version}_all.deb"
    if deb_path.exists():
        deb_path.unlink()

    with tempfile.TemporaryDirectory(prefix="taildrop_deb_") as tmp:
        pkg_root = Path(tmp) / f"{package_name}_{version}_all"
        debian_dir = pkg_root / "DEBIAN"
        bin_dir = pkg_root / "usr" / "bin"
        apps_dir = pkg_root / "usr" / "share" / "applications"
        doc_dir = pkg_root / "usr" / "share" / "doc" / package_name

        for directory in (debian_dir, bin_dir, apps_dir, doc_dir):
            directory.mkdir(parents=True, exist_ok=True)

        installed_bin = bin_dir / package_name
        shutil.copy2(exe_path, installed_bin)
        os.chmod(installed_bin, 0o755)

        installed_desktop = apps_dir / "taildrop-gui.desktop"
        shutil.copy2(root / "taildrop-gui.desktop", installed_desktop)
        os.chmod(installed_desktop, 0o644)

        installed_copyright = doc_dir / "copyright"
        shutil.copy2(root / "LICENSE", installed_copyright)
        os.chmod(installed_copyright, 0o644)

        control_content = (
            f"Package: {package_name}\n"
            f"Version: {version}\n"
            "Section: net\n"
            "Priority: optional\n"
            "Architecture: all\n"
            "Maintainer: taildrop-gui contributors\n"
            "Depends: python3 (>= 3.12), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1\n"
            f"Description: {description}\n"
        )
        control_path = debian_dir / "control"
        control_path.write_text(control_content, encoding="utf-8")
        os.chmod(control_path, 0o644)

        subprocess.run(
            ["dpkg-deb", "--build", "--root-owner-group", str(pkg_root), str(deb_path)],
            check=True,
        )

    return deb_path


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    dist_dir = root / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)

    package_name, version, description = _load_project_metadata(root)
    exe_path = build_zipapp(root, dist_dir, package_name)
    deb_path = build_deb(root, dist_dir, package_name, version, description, exe_path)

    print(f"Built executable: {exe_path}")
    print(f"Built Debian package: {deb_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
