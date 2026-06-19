import app.execution as execution
from app.config import Config, DockerConfig
from app.execution import DockerBackend, SubprocessBackend, get_execution_backend, sanitize_label


def test_backend_selection_follows_docker_flag():
    config = Config()
    assert isinstance(get_execution_backend(config), SubprocessBackend)
    config.docker = DockerConfig(enabled=True)
    assert isinstance(get_execution_backend(config), DockerBackend)


def test_subprocess_backend_passes_env_and_cwd():
    inv = SubprocessBackend().build(["claude", "--model", "x"], "/tmp/repo", {"ANTHROPIC_API_KEY": "k"}, "label")
    assert inv.argv == ["claude", "--model", "x"]
    assert inv.cwd == "/tmp/repo"
    assert inv.env.get("ANTHROPIC_API_KEY") == "k"
    assert inv.cancel_argv is None


def test_docker_backend_builds_isolated_run():
    docker = DockerConfig(enabled=True, image="claude-worker-agent:latest", memory="2g", cpus="2")
    inv = DockerBackend(docker).build(["claude"], "/tmp/repo", {"ANTHROPIC_API_KEY": "k"}, "cw_alice_7_claude")
    assert inv.argv[:3] == ["docker", "run", "--rm"]
    assert "claude-worker-agent:latest" in inv.argv
    assert "--cap-drop" in inv.argv and "ALL" in inv.argv
    assert "-e" in inv.argv and "ANTHROPIC_API_KEY=k" in inv.argv
    assert "--memory" in inv.argv and "2g" in inv.argv
    # Cancellation kills the named container out-of-band.
    assert inv.cancel_argv == ["docker", "kill", "cw_alice_7_claude"]


def test_docker_backend_without_workspace_skips_mount():
    inv = DockerBackend(DockerConfig(enabled=True)).build(["claude"], None, {}, "cw_local_plan")
    assert "-v" not in inv.argv  # no workspace to mount during discovery/planning


def test_docker_backend_runs_as_host_user_on_posix(monkeypatch):
    # Simulate a POSIX host so the host-uid path is exercised even on Windows CI.
    monkeypatch.setattr(execution.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(execution.os, "getgid", lambda: 1000, raising=False)
    inv = DockerBackend(DockerConfig(enabled=True, run_as_host_user=True)).build(
        ["claude"], "/tmp/repo", {"ANTHROPIC_API_KEY": "k"}, "cw_alice_7_claude"
    )
    assert "--user" in inv.argv
    assert "1000:1000" in inv.argv
    # The agent gets a writable HOME so the CLI cache works as the host uid.
    assert "HOME=/tmp" in inv.argv


def test_docker_backend_host_user_can_be_disabled(monkeypatch):
    monkeypatch.setattr(execution.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(execution.os, "getgid", lambda: 1000, raising=False)
    inv = DockerBackend(DockerConfig(enabled=True, run_as_host_user=False)).build(
        ["claude"], "/tmp/repo", {}, "cw_local_plan"
    )
    assert "--user" not in inv.argv


def test_sanitize_label_makes_valid_container_name():
    assert " " not in sanitize_label("cw alice/run 7")
    assert "/" not in sanitize_label("cw alice/run 7")
    assert sanitize_label("")  # never empty
