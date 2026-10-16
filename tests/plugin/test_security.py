"""Security regressions through tool handlers and the real Hermes policy APIs."""

import builtins
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_plugin import TOKEN, PROJECT, call, configured, plugin, service, tools


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.delenv("HERMES_WRITE_SAFE_ROOT", raising=False)
    return home


@pytest.fixture
def artifact_id(tools, configured):
    return call(tools, "artifact_publish", {
        "title": "Download fixture", "content": "harmless fixture", "kind": "markdown",
    })["artifact"]["id"]


@pytest.mark.parametrize("relative", [".ssh/id_ed25519", ".ssh/config", ".hermes/.env"])
@pytest.mark.parametrize("symlink", [False, True])
def test_download_refuses_real_policy_targets(tools, artifact_id, isolated_home, tmp_path, relative, symlink):
    from agent.file_safety import get_write_denied_error, is_write_approval_required

    target = isolated_home / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"original harmless fixture")
    assert get_write_denied_error(str(target)) or is_write_approval_required(str(target))
    requested = tmp_path / "friendly.md" if symlink else target
    if symlink:
        requested.symlink_to(target)
    result = call(tools, "artifact_download", {
        "artifact_id": artifact_id, "path": str(requested), "overwrite": True,
    })
    assert result["error"]["code"] == "invalid_arguments"
    assert target.read_bytes() == b"original harmless fixture"
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]


def test_download_honors_safe_root(tools, artifact_id, tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("HERMES_WRITE_SAFE_ROOT", str(allowed))
    target = tmp_path / "outside" / "copy.md"
    result = call(tools, "artifact_download", {"artifact_id": artifact_id, "path": str(target)})
    assert "HERMES_WRITE_SAFE_ROOT" in result["error"]["message"]
    assert not target.parent.exists()
    accepted = call(tools, "artifact_download", {"artifact_id": artifact_id, "path": str(allowed / "copy.md")})
    assert accepted["byte_length"] == 16


def test_download_resolves_symlinks_and_overwrites_atomically(tools, artifact_id, tmp_path):
    target = tmp_path / "actual.md"
    target.write_bytes(b"old")
    old_inode = target.stat().st_ino
    alias = tmp_path / "alias.md"
    alias.symlink_to(target)
    denied = call(tools, "artifact_download", {"artifact_id": artifact_id, "path": str(alias)})
    assert "exists" in denied["error"]["message"]
    assert target.read_bytes() == b"old"
    result = call(tools, "artifact_download", {"artifact_id": artifact_id, "path": str(alias), "overwrite": True})
    assert result == {"path": str(target), "byte_length": 16,
                      "sha256": hashlib.sha256(b"harmless fixture").hexdigest()}
    assert alias.is_symlink() and target.read_bytes() == b"harmless fixture"
    assert target.stat().st_ino != old_inode
    assert sorted(p.name for p in tmp_path.iterdir()) == ["actual.md", "alias.md", "home"]


def test_download_replace_failure_preserves_target_and_cleans_temp(tools, artifact_id, plugin, tmp_path, monkeypatch):
    target = tmp_path / "copy.md"
    target.write_bytes(b"keep me")

    def fail_replace(source, destination):
        assert Path(source).parent == target.parent
        assert Path(source).read_bytes() == b"harmless fixture"
        assert Path(destination) == target
        raise OSError("fixture replace failure")

    monkeypatch.setattr(plugin.tools.os, "replace", fail_replace)
    result = call(tools, "artifact_download", {"artifact_id": artifact_id, "path": str(target), "overwrite": True})
    assert "fixture replace failure" in result["error"]["message"]
    assert target.read_bytes() == b"keep me"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["copy.md", "home"]


@pytest.mark.parametrize("filename,kind", [(".env", "markdown"), ("id_ed25519", "markdown"),
                                          ("report.md", "html")])
def test_publish_refuses_disguised_or_contradictory_formats(tools, configured, tmp_path, filename, kind):
    source = tmp_path / filename
    source.write_bytes(b"harmless fixture")
    result = call(tools, "artifact_publish", {"path": str(source), "kind": kind, "title": "Refused"})
    assert result["error"]["code"] == "invalid_arguments"


def test_publish_checks_resolved_read_policy_before_reading(tools, configured, isolated_home, tmp_path, monkeypatch):
    source = isolated_home / ".hermes" / "auth.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"harmless fixture")
    alias = tmp_path / "report.md"
    alias.symlink_to(source)
    read_bytes = Path.read_bytes

    def reject_fixture_read(path):
        if path.resolve() == source:
            pytest.fail("blocked file was read")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_fixture_read)
    result = call(tools, "artifact_publish", {"path": str(alias), "title": "Refused"})
    assert "Access denied" in result["error"]["message"]


@pytest.mark.parametrize("suffix,kind,content", [
    (".html", "html", b"<h1>ok</h1>"), (".htm", "html", b"<p>ok</p>"),
    (".md", "markdown", b"# ok"), (".markdown", "markdown", b"# ok"),
    (".svg", "svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>'),
    (".png", "png", b"\x89PNG\r\n\x1a\nfixture"), (".webp", "webp", b"RIFFxxxxWEBPfixture"),
])
def test_supported_path_formats_remain_private(tools, configured, tmp_path, suffix, kind, content):
    source = tmp_path / ("report" + suffix)
    source.write_bytes(content)
    published = call(tools, "artifact_publish", {"path": str(source), "kind": kind, "title": "Supported"})
    assert published["visibility"] == "private" and published["artifact"]["kind"] == kind
    detail = call(tools, "artifact_get", {"artifact_id": published["artifact"]["id"]})
    assert detail["shares"] == []


@pytest.fixture
def multiplex_scope():
    from agent import secret_scope

    secret_scope.set_multiplex_active(True)
    tokens = []

    def bind(values):
        tokens.append(secret_scope.set_secret_scope(values))

    yield bind
    for token in reversed(tokens):
        secret_scope.reset_secret_scope(token)
    secret_scope.set_multiplex_active(False)


def test_real_secret_scope_overrides_environment(tools, configured, monkeypatch, multiplex_scope):
    multiplex_scope({"HERMES_ARTIFACTS_URL": configured, "HERMES_ARTIFACTS_TOKEN": TOKEN})
    monkeypatch.setenv("HERMES_ARTIFACTS_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HERMES_ARTIFACTS_TOKEN", "wrong-profile-fixture")
    result = call(tools, "artifact_list", {})
    assert "items" in result and "error" not in result


@pytest.mark.parametrize("key", ["HERMES_ARTIFACTS_URL", "HERMES_ARTIFACTS_TOKEN"])
@pytest.mark.parametrize("missing", [True, False])
def test_scoped_missing_or_empty_secret_never_borrows_environment(tools, configured, multiplex_scope, key, missing):
    values = {"HERMES_ARTIFACTS_URL": configured, "HERMES_ARTIFACTS_TOKEN": TOKEN}
    if missing:
        del values[key]
    else:
        values[key] = ""
    multiplex_scope(values)
    assert call(tools, "artifact_list", {})["error"]["code"] == "not_configured"


def test_unscoped_multiplex_denial_propagates(tools, configured, multiplex_scope):
    from agent.secret_scope import UnscopedSecretError

    multiplex_scope(None)
    with pytest.raises(UnscopedSecretError):
        call(tools, "artifact_list", {})


def test_standalone_secret_fallback_without_hermes(tmp_path):
    code = """
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location('standalone_client', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
client = module.ArtifactService.from_env()
assert client.base_url == 'http://standalone.invalid'
assert client._token == 'synthetic-standalone-token'
print('standalone import-only fallback passed')
"""
    result = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(PROJECT / "plugin" / "client.py")],
                            env={"PATH": os.environ["PATH"], "HOME": str(tmp_path),
                                 "HERMES_ARTIFACTS_URL": "http://standalone.invalid",
                                 "HERMES_ARTIFACTS_TOKEN": "synthetic-standalone-token"},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "standalone import-only fallback passed"
