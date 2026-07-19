"""Minimal pinned-runtime home resolver used by provider discovery."""
from __future__ import annotations

import os
from pathlib import Path


def get_hermes_home() -> Path:
    return Path(os.environ["HERMES_HOME"])
