"""DubProject 的原子 JSON 存储。"""

import json
from pathlib import Path

from .schema import DubProject


STATE_FILE = "dub_project.json"


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


class DubProjectStore:
    def __init__(self, project_dir):
        self.root = Path(project_dir)
        self.path = self.root / STATE_FILE

    def exists(self) -> bool:
        return self.path.is_file()

    def save(self, project: DubProject) -> str:
        project.touch()
        atomic_write_json(self.path, project.to_dict())
        return str(self.path)

    def load(self) -> DubProject:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return DubProject.from_dict(data)
