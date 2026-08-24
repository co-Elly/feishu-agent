"""Read-only access to guardian restart diagnostics."""

import json
import os

from settings import runtime_value


def guardian_status():
    path = os.path.join(runtime_value("workspace_dir"), "guardian_status.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}
