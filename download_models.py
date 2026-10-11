#!/usr/bin/env python3
"""Redirect: video models are managed by ~/h3-video-editor/download_fastvideo_trim.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path.home() / "h3-video-editor" / "download_fastvideo_trim.py"


def main() -> int:
    if not SCRIPT.is_file():
        print(f"Missing {SCRIPT}", file=sys.stderr)
        return 1
    print("Use: cd ~/h3-video-editor && ./download_fastvideo_trim.py")
    return subprocess.call([sys.executable, str(SCRIPT), *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
