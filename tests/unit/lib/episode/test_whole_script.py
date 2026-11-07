"""Full-source import preserves every code point and commits all episodes together."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from lib.episode.episode_management import create_episode
from lib.episode.episode_paths import episode_source_path
from lib.episode.episode_sources import source_snapshot_path
from lib.episode.whole_script import divide_script, import_script, project_basis
from lib.project.project_manager import ProjectManager

TEXT = "前言🙂e\u0301\r\n# 第一集：开始\r\n甲说：你好。\r\n第２集 后续\r\n乙说：再见。\r\n"


@pytest.fixture
def project(tmp_path):
    pm = ProjectManager(tmp_path)
    pm.create_project("demo", content_mode="drama")
    return pm


def save(pm, text=TEXT, basis=None):
    preview = divide_script(text)
    return import_script(
        pm,
        "demo",
        preview["text"],
        [{k: p[k] for k in ("start", "title", "number")} for p in preview["episodes"]],
        preview["fingerprint"],
        basis or project_basis(pm.load_project("demo")),
    )


def test_chinese_fullwidth_headings_preserve_full_normalized_text():
    result = divide_script(TEXT)
    assert [p["number"] for p in result["episodes"]] == [1, 2]
    assert result["warnings"]
    assert not result["errors"]
    assert "".join(result["text"][p["start"] : p["end"]] for p in result["episodes"]) == result["text"]
    assert "🙂é" in result["text"]


@pytest.mark.parametrize(
    ("text", "has_errors", "has_warnings"),
    [
        ("第一集\n正文\n第一集\n正文", True, False),
        ("第二集\n正文\n第一集\n正文", True, False),
        ("第一集\n正文\n第三集\n正文", False, True),
        ("第一集\n第二集\n正文", True, False),
        ("没有标题的正文", False, True),
        ("第十一集\n正文\n第十二集\n正文", False, False),
    ],
)
def test_boundaries_validate_before_write(text, has_errors, has_warnings):
    result = divide_script(text)
    assert bool(result["errors"]) == has_errors
    assert bool(result["warnings"]) == has_warnings


def test_manual_split_merge_and_invalid_offsets():
    text = "前言🙂\n正文A\n正文B"
    merged = divide_script(text, [{"start": 0, "number": 1, "title": "一集"}])
    assert merged["episodes"][0]["end"] == len(text)
    with pytest.raises(ValueError, match="行首"):
        divide_script(text, [{"start": 0, "number": 1, "title": "一"}, {"start": 2, "number": 2, "title": "二"}])


def test_import_append_idempotence_source_snapshot_and_no_media(project):
    old = create_episode(project, "demo", source_text="旧集原文")
    first = save(project)
    assert first == save(project)
    data = project.load_project("demo")
    assert [e["episode"] for e in data["episodes"]] == [old, *first["episodes"]]
    directory = project.get_project_path("demo")
    assert episode_source_path(directory, old).read_text() == "旧集原文"
    rel = data["episodes"][1]["source_range"]["source_file"]
    assert (directory / rel).read_text() == source_snapshot_path(directory, rel).read_text()
    assert (
        "".join(episode_source_path(directory, e).read_text() for e in first["episodes"]) == divide_script(TEXT)["text"]
    )
    assert not (directory / "production" / "script_batch.json").exists()
    assert not list((directory / "scripts").glob("*.json"))


def test_concurrent_double_confirmation_creates_one_import(project):
    basis = project_basis(project.load_project("demo"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: save(project, basis=basis), range(2)))
    assert results[0] == results[1]
    assert len(project.load_project("demo")["episodes"]) == 2


def test_stale_project_and_transaction_failure_leave_no_partial_import(project, monkeypatch):
    basis = project_basis(project.load_project("demo"))
    create_episode(project, "demo")
    with pytest.raises(ValueError, match="项目已变化"):
        save(project, basis=basis)
    before = project.load_project("demo")

    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr("lib.episode.whole_script.write_episode_source_file", fail)
    with pytest.raises(OSError, match="disk unavailable"):
        save(project)
    assert project.load_project("demo") == before
    assert not list((project.get_project_path("demo") / "source").glob("screenplay-*"))
    assert not list((project.get_project_path("demo") / ".state").rglob("screenplay-*"))


def test_changed_import_cannot_be_silently_reconfirmed(project):
    result = save(project)
    episode_source_path(project.get_project_path("demo"), result["episodes"][0]).write_text("外部改动")
    with pytest.raises(ValueError, match="已被修改"):
        save(project)
