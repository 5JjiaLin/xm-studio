"""Local CLI transport, path boundaries and deployment lock exclusion."""

import getpass
from pathlib import Path

import portalocker
import pytest

from scripts import company_admin


@pytest.fixture
def root(tmp_path):
    directory = tmp_path / "deployment"
    (directory / "data").mkdir(parents=True)
    (directory / "ops").mkdir()
    (directory / "data" / "arcreel.db").touch()
    return directory


@pytest.mark.parametrize("lock", ["instance.lock", "operations.lock"])
def test_refuses_live_host_and_operations_lock(root, lock):
    with (
        portalocker.Lock(root / "ops" / lock, mode="a", timeout=0),
        pytest.raises(RuntimeError, match="running or another operation"),
        company_admin.offline_deployment(root),
    ):
        pytest.fail("must not admit an account change")
    with (
        company_admin.offline_deployment(root),
        pytest.raises(portalocker.exceptions.LockException),
        portalocker.Lock(root / "ops" / "instance.lock", mode="a", timeout=0),
    ):
        pytest.fail("host startup must be blocked during account changes")


def test_refuses_missing_database_relative_root_and_inspection_copy(root):
    with pytest.raises(ValueError, match="absolute"):
        company_admin.validate_root(Path("relative"))
    (root / "ops" / "inspection-only.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="inspection-only"):
        company_admin.validate_root(root)
    (root / "ops" / "inspection-only.json").unlink()
    (root / "data" / "arcreel.db").unlink()
    with pytest.raises(ValueError, match=r"Existing data/arcreel\.db"):
        company_admin.validate_root(root)


def test_refuses_redirected_database(root, monkeypatch):
    original = Path.is_symlink
    database = root / "data" / "arcreel.db"
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == database or original(path))
    with pytest.raises(ValueError, match="symlinks or junctions"):
        company_admin.validate_root(root)


def test_password_requires_hidden_input_and_confirmation(monkeypatch):
    monkeypatch.setattr(getpass, "getpass", lambda prompt: "short")
    with pytest.raises(ValueError, match="12 characters"):
        company_admin.prompt_password()
    answers = iter(["long-test-password", "different-password"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt: next(answers))
    with pytest.raises(ValueError, match="do not match"):
        company_admin.prompt_password()


def test_echoing_password_fallback_fails_before_reading(monkeypatch):
    import warnings

    def fallback(prompt):
        warnings.warn("echo fallback", getpass.GetPassWarning, stacklevel=2)
        pytest.fail("must fail before any echoing read")

    monkeypatch.setattr(getpass, "getpass", fallback)
    with pytest.raises(getpass.GetPassWarning):
        company_admin.prompt_password()


def test_noninteractive_main_never_prompts_or_changes_accounts(root, monkeypatch, capsys):
    monkeypatch.setattr(company_admin.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(company_admin, "prompt_password", lambda: pytest.fail("must not prompt"))
    assert company_admin.main(["initialize", "--root", str(root), "--username", "employee-admin"]) == 1
    assert "Command failed" in capsys.readouterr().err


def test_main_redacts_database_exception_parameters(root, monkeypatch, capsys):
    monkeypatch.setattr(company_admin.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(company_admin, "local_operator", lambda: "test-operator")
    monkeypatch.setattr(company_admin, "prompt_password", lambda: "secret-test-password")
    replies = iter(["IT-test", "initialize employee-admin"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(replies))

    async def failing_change(*args, **kwargs):
        raise RuntimeError("SQL parameters: secret-test-password and $argon2id$hash")

    monkeypatch.setattr(company_admin, "apply_change", failing_change)
    assert company_admin.main(["initialize", "--root", str(root), "--username", "employee-admin"]) == 1
    output = capsys.readouterr()
    assert "secret-test-password" not in output.out + output.err
    assert "$argon2id$hash" not in output.out + output.err
