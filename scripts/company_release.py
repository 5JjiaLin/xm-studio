"""Create an immutable source archive and build a new, inactive Windows release.

Never copies the developer's .venv, data root, environment files or credentials.
Does not switch current-release.json or stop the current application.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.company_ops import digest
from scripts.company_server import CODE_ROOT, atomic_json

SOURCE_DIRS = frozenset({
    "agent_runtime_profile", "alembic", "deploy", "docs", "frontend", "lib", "LICENSES", "packages",
    "scripts", "server", "skills", "tests",
})
SOURCE_FILES = frozenset({
    "alembic.ini", "pyproject.toml", "uv.lock", "plan.md", "README.md", "README.en.md", "LICENSE", "NOTICE",
    "SECURITY.md", "skills-lock.json", ".env.example", ".gitignore", ".gitattributes",
})
EXCLUDED_DIRS = frozenset({
    ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".git", "dist", ".vite",
    "test-results", "playwright-report", ".codex", "skill-optimization-workspace",
})
EXCLUDED_PATHS = frozenset({
    "deploy/projects", "deploy/vertex_keys", "deploy/claude_data", "deploy/production/projects",
    "deploy/production/vertex_keys", "deploy/production/claude_data", "deploy/production/pgdata",
})


def source_files(source: Path):
    for item in sorted(source.iterdir()):
        if item.name not in SOURCE_FILES and item.name not in SOURCE_DIRS:
            continue
        if item.is_symlink() or (os.name == "nt" and item.is_junction()):
            raise RuntimeError(f"Source archive rejects linked entries: {item}")
        if item.name in SOURCE_FILES and item.is_file():
            yield item
        elif item.name in SOURCE_DIRS and item.is_dir():
            for directory, dirs, files in os.walk(item, followlinks=False):
                dirs[:] = sorted(
                    name for name in dirs if name not in EXCLUDED_DIRS
                    and (Path(directory) / name).relative_to(source).as_posix() not in EXCLUDED_PATHS
                )
                for name in sorted(files):
                    path = Path(directory) / name
                    if name.endswith((".pyc", ".pyo", ".tsbuildinfo")):
                        continue
                    if (name.startswith(".env") and not name.endswith(".example")) or name.endswith(".local.json"):
                        continue
                    if path.is_symlink() or (os.name == "nt" and path.is_junction()):
                        raise RuntimeError(f"Source archive rejects linked files: {path}")
                    yield path
                for name in dirs:
                    path = Path(directory) / name
                    if path.is_symlink() or (os.name == "nt" and path.is_junction()):
                        raise RuntimeError(f"Source archive rejects linked directories: {path}")


def pnpm_command(pnpm: Path) -> list[str]:
    # Invoke the npm-installed CLI through Node directly. This avoids implicit
    # cmd.exe parsing of a .cmd shim and works when paths contain spaces.
    if pnpm.suffix.lower() in {".cmd", ".ps1"}:
        cli = pnpm.parent / "node_modules" / "pnpm" / "bin" / "pnpm.cjs"
        node = shutil.which("node.exe")
        if not node or not cli.is_file():
            raise RuntimeError("Cannot resolve Node and the pnpm CLI behind the supplied shim")
        return [node, str(cli)]
    return [str(pnpm)]


def prepare(root: Path, release_id: str, uv: Path, pnpm: Path, *, source: Path = CODE_ROOT) -> dict:
    root, source = root.resolve(), source.resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", release_id):
        raise ValueError("Invalid release identifier")
    release = root / "releases" / release_id
    if release.exists():
        raise RuntimeError("Release already exists; choose a new identifier")
    (root / "staging").mkdir(parents=True, exist_ok=True)
    release.mkdir(parents=True)
    archive = root / "staging" / f"source-{release_id}.zip"
    if archive.exists():
        raise RuntimeError("Source archive already exists")
    receipt = {"release_id": release_id, "path": str(release), "started_at": time.time(), "build_ok": False}
    try:
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
            manifest = {}
            for path in source_files(source):
                relative = path.relative_to(source).as_posix()
                target = release / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                manifest[relative] = digest(target)
                bundle.write(target, relative)
        atomic_json(release / "source-manifest.json", {"files": manifest})
        receipt.update(source_archive=str(archive), source_sha256=digest(archive), source_files=len(manifest))
        # Put the base interpreter under the deployment root so a dedicated
        # service account never depends on EDY's private AppData directory.
        runtime_dir = root / "runtime" / "python"
        subprocess.run([
            str(uv), "python", "install", "3.12.16", "--install-dir", str(runtime_dir), "--no-bin", "--no-registry"
        ], check=True)
        interpreters = list(runtime_dir.glob("cpython-3.12.16-*/python.exe"))
        if len(interpreters) != 1:
            raise RuntimeError("Cannot resolve the deployment's pinned Python interpreter")
        subprocess.run([str(uv), "sync", "--frozen", "--no-dev", "--python", str(interpreters[0])], cwd=release, check=True)
        frontend_command = pnpm_command(pnpm)
        subprocess.run([*frontend_command, "install", "--frozen-lockfile"], cwd=release / "frontend", check=True)
        subprocess.run([*frontend_command, "build"], cwd=release / "frontend", check=True)
        if not (release / "frontend" / "dist" / "index.html").is_file():
            raise RuntimeError("Frontend build did not produce index.html")
        receipt.update(
            build_ok=True,
            completed_at=time.time(),
            python=str(interpreters[0]),
            uv_lock_sha256=digest(release / "uv.lock"),
            pnpm_lock_sha256=digest(release / "frontend" / "pnpm-lock.yaml"),
            frontend_files={p.relative_to(release / "frontend" / "dist").as_posix(): digest(p)
                            for p in (release / "frontend" / "dist").rglob("*") if p.is_file()},
        )
    except Exception as exc:
        receipt["error"] = str(exc)
        raise
    finally:
        atomic_json(release / "release-info.json", receipt)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(r"D:\XMStudio"))
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--pnpm", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.root, args.release_id, args.uv, args.pnpm)
    print(json.dumps({k: v for k, v in result.items() if k != "frontend_files"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
