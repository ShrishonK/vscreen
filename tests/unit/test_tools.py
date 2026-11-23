"""External tool resolution. No binary is ever executed: subprocess is mocked."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from vscreen import tools
from vscreen.errors import ToolNotFound, ToolVersionError


class FakeCompleted:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture
def fake_vina(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Put a fake vina on PATH that reports a version. Records the calls made."""
    calls: list[list[str]] = []

    def which(name: str) -> str | None:
        return "/opt/conda/bin/vina" if name == "vina" else None

    def run(cmd: list[str], **kwargs: Any) -> FakeCompleted:
        calls.append(list(cmd))
        return FakeCompleted(stdout="AutoDock Vina v1.2.5\n")

    monkeypatch.setattr(tools.shutil, "which", which)
    monkeypatch.setattr(tools.subprocess, "run", run)
    return calls


def test_resolve_captures_the_version(fake_vina: list[list[str]]) -> None:
    resolved = tools.resolve_tool("vina")
    assert resolved.name == "vina"
    assert resolved.path == Path("/opt/conda/bin/vina")
    assert resolved.version == "AutoDock Vina v1.2.5"
    assert fake_vina == [["/opt/conda/bin/vina", "--version"]]


def test_resolution_is_cached_per_process(fake_vina: list[list[str]]) -> None:
    tools.resolve_tool("vina")
    tools.resolve_tool("vina")
    assert len(fake_vina) == 1
    tools.resolve_tool("vina", refresh=True)
    assert len(fake_vina) == 2
    tools.clear_cache()
    tools.resolve_tool("vina")
    assert len(fake_vina) == 3


def test_is_available(fake_vina: list[list[str]]) -> None:
    assert tools.is_available("vina") is True
    assert tools.is_available("gnina") is False


def test_missing_binary_names_the_install_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    with pytest.raises(ToolNotFound) as exc:
        tools.resolve_tool("vina")
    message = str(exc.value)
    assert "vina not found on PATH" in message
    assert "conda install -c conda-forge vina" in message


def test_every_registered_tool_has_an_install_hint() -> None:
    assert set(tools.REGISTRY) == {"vina", "gnina", "aizynthcli"}
    for tool in tools.REGISTRY.values():
        assert tool.install_hint, f"{tool.name} needs an install hint"
        assert tool.candidates


def test_unknown_tool_lists_what_is_registered() -> None:
    with pytest.raises(ToolNotFound, match="Registered tools"):
        tools.get_tool("schrodinger")


def test_silent_binary_is_an_error_not_a_blank_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(tools.subprocess, "run", lambda cmd, **kw: FakeCompleted(stdout="  \n"))
    with pytest.raises(ToolVersionError, match="Rule 5"):
        tools.resolve_tool("gnina")


def test_version_from_stderr_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        tools.subprocess, "run", lambda cmd, **kw: FakeCompleted(stderr="gnina 1.1\n")
    )
    assert tools.resolve_tool("gnina").version == "gnina 1.1"


def test_version_pattern_extracts_a_group(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = tools.ExternalTool(
        name="fake",
        candidates=("fake",),
        install_hint="n/a",
        version_pattern=r"v([0-9.]+)",
    )
    monkeypatch.setattr(tools.shutil, "which", lambda name: "/usr/bin/fake")
    monkeypatch.setattr(
        tools.subprocess, "run", lambda cmd, **kw: FakeCompleted(stdout="Fake tool v2.3.4\n")
    )
    assert tool.resolve().version == "2.3.4"

    unparseable = tools.ExternalTool(
        name="fake", candidates=("fake",), install_hint="n/a", version_pattern=r"v([0-9]+zzz)"
    )
    with pytest.raises(ToolVersionError, match="Could not parse"):
        unparseable.resolve()


def test_execution_failures_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools.shutil, "which", lambda name: f"/usr/bin/{name}")

    def boom(cmd: list[str], **kwargs: Any) -> FakeCompleted:
        raise OSError("Exec format error")

    monkeypatch.setattr(tools.subprocess, "run", boom)
    with pytest.raises(ToolVersionError, match="Exec format error"):
        tools.resolve_tool("vina")

    def timeout(cmd: list[str], **kwargs: Any) -> FakeCompleted:
        raise subprocess.TimeoutExpired(cmd, 20.0)

    monkeypatch.setattr(tools.subprocess, "run", timeout)
    tools.clear_cache()
    with pytest.raises(ToolVersionError, match="did not report a version"):
        tools.resolve_tool("vina")


# --------------------------------------------------------------------------- #
# rule 5: tool_versions is never empty
# --------------------------------------------------------------------------- #


def test_tool_versions_always_records_the_runtime() -> None:
    versions = tools.tool_versions([])
    assert set(versions) == {"python", "vscreen"}
    assert all(versions.values())


def test_tool_versions_includes_each_requested_tool(fake_vina: list[list[str]]) -> None:
    versions = tools.tool_versions(["vina"])
    assert versions["vina"] == "AutoDock Vina v1.2.5"
    assert "python" in versions and "vscreen" in versions


def test_tool_versions_raises_rather_than_recording_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    with pytest.raises(ToolNotFound):
        tools.tool_versions(["vina"])


# --------------------------------------------------------------------------- #
# rule 6: GPU is never assumed
# --------------------------------------------------------------------------- #


def test_gpu_absent_when_nvidia_smi_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    assert tools.gpu_available(env={}) is False


def test_gpu_detected_from_nvidia_smi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda cmd, **kw: FakeCompleted(stdout="GPU 0: NVIDIA A100 (UUID: GPU-abc)\n"),
    )
    assert tools.gpu_available(env={}) is True


def test_gpu_can_be_forced_for_remote_runners(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    assert tools.gpu_available(env={"VSCREEN_FORCE_GPU": "1"}) is True


def test_gpu_absent_when_nvidia_smi_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(
        tools.subprocess, "run", lambda cmd, **kw: FakeCompleted(stdout="", returncode=9)
    )
    assert tools.gpu_available(env={}) is False
