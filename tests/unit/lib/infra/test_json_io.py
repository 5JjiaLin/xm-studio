import json
import os

from lib.infra.json_io import atomic_write_json
from tests.fakes import observe_json_replace


def test_atomic_write_json_fsyncs_temp_file_before_replace(tmp_path, monkeypatch):
    """替换前临时文件必须已落盘：断电时目标不会被换成空文件或半截文件。"""
    target = tmp_path / "project.json"
    target.write_text('{"old": true}', encoding="utf-8")
    events: list[str] = []
    real_fsync = os.fsync

    def recording_fsync(fd):
        events.append("fsync")
        real_fsync(fd)

    def recording_replace(_src, _dst):
        events.append("replace")

    monkeypatch.setattr(os, "fsync", recording_fsync)
    observe_json_replace(monkeypatch, recording_replace)

    atomic_write_json(target, {"title": "新内容"})

    assert events == ["fsync", "replace"]
    assert json.loads(target.read_text(encoding="utf-8")) == {"title": "新内容"}
    assert list(tmp_path.iterdir()) == [target]
