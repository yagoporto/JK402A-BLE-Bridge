"""Launcher silencioso (sem janela de terminal) para o bridge JK-402A no Windows."""

from __future__ import annotations

import os
from pathlib import Path
import sys

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

os.chdir(BASE_DIR)

from src.bridge import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
