"""Editable identity/project files are separate from code and authorization."""

import json
from pathlib import Path

DEFAULT_IDENTITY = {
    "name": "Project Assistant",
    "creator": "Project owner",
    "purpose": "Help the project team",
    "style": "Clear, friendly, practical",
}


class ContextFiles:
    def __init__(self, directory: Path):
        self.directory = directory

    def read(self, name):
        if name not in {"identity", "project", "team"}:
            raise ValueError("Unknown context file")
        path = self.directory / (name + ".json")
        if not path.is_file():
            return DEFAULT_IDENTITY if name == "identity" else {}
        if path.stat().st_size > 24000:
            raise ValueError("Context file exceeds 24 KB")
        result = json.loads(path.read_text("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("Context must be a JSON object")
        return result

    def write(self, name, value):
        self.read(name)  # Validate the allowlisted filename.
        data = json.dumps(value, ensure_ascii=False, indent=2)
        if len(data.encode()) > 24000:
            raise ValueError("Context exceeds 24 KB")
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / (name + ".json")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(path)

    def prompt(self):
        return json.dumps(
            {name: self.read(name) for name in ("identity", "project", "team")}, ensure_ascii=False
        )
