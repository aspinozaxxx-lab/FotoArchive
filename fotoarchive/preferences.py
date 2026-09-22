"""UI preferences live apart from source settings owned by the worker."""
import json


class Preferences:
    def __init__(self, data_dir):
        self.path = data_dir / "preferences.json"
        try:
            self.values = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self.values, dict):
                self.values = {}
        except (OSError, ValueError):
            self.values = {}

    @property
    def show_face_boxes(self):
        return self.values.get("show_face_boxes", True) is not False

    @property
    def face_ids(self):
        values = self.values.get("face_examples", [])
        return list(dict.fromkeys(i for i in values if isinstance(i, str))) if isinstance(values, list) else []

    @property
    def rejected_faces(self):
        values = self.values.get("rejected_faces", [])
        return {i for i in values if isinstance(i, str)} if isinstance(values, list) else set()

    def save(self, **values):
        self.values.update(values)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.values, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)
