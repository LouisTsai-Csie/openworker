"""The tool runner: protocol, daemon, relay, client (design doc `sandbox-design.md`).

These run the real thing end to end: the runner packed into its single file, a daemon
process, an attach relay per pipe, and the client. No sandbox technology is involved
(`runner-local`), so what is tested here is the protocol and its delivery rules. The
daemon listens on a Unix socket, or on Windows a named pipe; the shell is bash there and
PowerShell here, so the commands come in two spellings.
"""

from __future__ import annotations

import ast
import base64
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

from coworker.sandbox.bundle import build_runner_zipapp
from coworker.sandbox import client as client_mod
from coworker.sandbox.client import RunnerClient
from coworker.sandbox.executor import RunnerExecutor
from coworker.sandbox.providers.runner_local import RunnerLocalProvider
from coworker.sandbox.runner import protocol as P
from coworker.sandbox.workspace import DirectWorkspace, RunnerWorkspace, open_workspace

RUNNER_DIR = Path(__file__).resolve().parents[1] / "coworker" / "sandbox" / "runner"
_WIN = sys.platform == "win32"
posix_only = pytest.mark.skipif(_WIN, reason="bash syntax")


def _sleep(seconds: float) -> str:
    # `Start-Sleep 0.4` would sleep 0 s: its `-Seconds` is an integer in PowerShell 5.
    return f"Start-Sleep -Milliseconds {int(seconds * 1000)}" if _WIN else f"sleep {seconds}"


def _marker(path: Path) -> str:
    """A command that announces itself, to be chained in front of a long one."""
    return f"New-Item -ItemType File -Path '{path}'" if _WIN else f"touch {path}"


def _await_started(path: Path, timeout: float = 30.0) -> None:
    """Block until the shell has actually begun the command that writes `path`.

    A fixed `time.sleep` here is a race, not a wait. Under `-n auto` the machine is busy
    and the restart/kill/second command below can land before the shell has started,
    after which the test fails on a missing dict key instead of saying what went wrong
    (CI, 2026-10-20).
    """
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), f"the command never started (no {path.name} after {timeout}s)"


def _when_started(path: Path, action, timeout: float = 30.0) -> threading.Thread:
    """Run `action` as soon as the command that writes `path` is actually going.

    The counterpart to `_await_started` for the cases where the main thread is the one
    blocked in `run()`. A `threading.Timer` with a fixed delay was a guess at when the
    shell would be ready, and under `-n auto` the guess fires too early.
    """

    def wait_then_act() -> None:
        deadline = time.monotonic() + timeout
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        action()

    thread = threading.Thread(target=wait_then_act, daemon=True)
    thread.start()
    return thread


def _cd_and_set(folder: str, name: str, value: str) -> str:
    return f"cd {folder}; $env:{name}='{value}'" if _WIN else f"cd {folder} && export {name}={value}"


def _echo_var_and_leaf(name: str) -> str:
    """Prints the variable and the current folder's last component."""
    return f'echo "$env:{name} $(Split-Path -Leaf (Get-Location))"' if _WIN else f"echo ${name} $(basename $PWD)"


LEAF = "Split-Path -Leaf (Get-Location)" if _WIN else "basename $PWD"


@pytest.fixture
def runner(tmp_path):
    """(provider, client) on a fresh daemon. The relay never leaves on silence here."""
    zipapp = build_runner_zipapp(tmp_path / "dist")
    provider = RunnerLocalProvider(cwd=tmp_path, runner_path=zipapp, relay_silence_seconds=0)
    provider.create()
    client = RunnerClient(provider.open_runner, ping_seconds=0.5)
    client.connect()
    yield provider, client
    client.close()
    provider.destroy()


# -- packaging --------------------------------------------------------------------------


def test_runner_uses_only_the_standard_library():
    """The runner is mounted into any image that has Python, so it may import nothing else."""
    allowed = set(sys.stdlib_module_names)
    for source in RUNNER_DIR.glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue  # relative imports stay inside the package
            for name in names:
                if source.name == "toolcalls.py" and name.startswith("aisuite.toolkits."):
                    continue  # only when run from a checkout; the packed file carries its own copy
                assert name.split(".")[0] in allowed, f"{source.name} imports {name}"


def test_zipapp_is_one_file_named_by_content(tmp_path):
    first = build_runner_zipapp(tmp_path)
    assert first == build_runner_zipapp(tmp_path)  # reused, not rebuilt
    with zipfile.ZipFile(first) as zf:
        names = set(zf.namelist())
    assert "__main__.py" in names and "owrunner/daemon.py" in names and "owrunner/executor.py" in names


# -- framing ----------------------------------------------------------------------------


def test_a_frame_is_always_one_line():
    frame = P.encode(P.result("r-1", {"output": "line one\nline two\r\n\ttabbed ☃ \x00"}))
    assert frame.count(b"\n") == 1 and frame.endswith(b"\n")
    assert P.decode(frame)["result"]["output"] == "line one\nline two\r\n\ttabbed ☃ \x00"


def test_lines_that_are_not_frames_are_dropped():
    assert P.decode(b"Welcome to Ubuntu 24.04\n") is None
    assert P.decode(b"[1, 2, 3]\n") is None
    assert P.decode(b"{not json}\n") is None
    assert P.decode(b"\xff\xfe\n") is None


@posix_only
def test_multi_line_output_and_shell_syntax_survive_the_pipe(runner, tmp_path):
    _, client = runner
    ex = RunnerExecutor(client, cwd=str(tmp_path))
    assert ex.run("printf 'a\\nb\\nc\\n'")["output"].startswith("a\nb\nc\n")
    # Redirection, a pipeline and a heredoc go to a real bash unchanged.
    assert ex.run("printf 'x\\ny\\nx\\n' > out.log 2>&1")["output"].strip() == ""
    assert ex.run("sort out.log | uniq -c | sort -rn | head -1")["output"].split() == ["2", "x"]
    ex.run("cat > notes.md <<'EOF'\n# Notes\nsecond \"line\"\nEOF")
    assert (tmp_path / "notes.md").read_text() == '# Notes\nsecond "line"\n'


# -- the contract -----------------------------------------------------------------------


def test_hello_reports_the_runner_environment(runner):
    _, client = runner
    assert client.hello["protocol_version"] == P.PROTOCOL_VERSION
    assert client.hello["instance_id"] == client.instance_id
    assert client.hello["os"] and client.hello["shell"]


def test_a_client_with_another_protocol_version_is_refused(runner):
    _, client = runner
    with pytest.raises(P.RunnerError) as err:
        client.call("runner.hello", {"protocol_version": 999}, timeout=10)
    assert err.value.code == P.VERSION_MISMATCH


def test_results_look_the_same_as_the_in_process_executor(runner, tmp_path):
    """A session must look the same to the model in both modes: same keys, same values."""
    _, client = runner
    direct = DirectWorkspace(cwd=tmp_path)
    try:
        command = "echo hi; cmd /c exit 1" if _WIN else "echo hi; false"
        ours = RunnerExecutor(client, cwd=str(tmp_path)).run(command)
        theirs = direct.executor.run(command)
    finally:
        direct.close()
    assert ours == theirs


def test_each_shell_keeps_its_own_folder_and_variables(runner, tmp_path):
    _, client = runner
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    one = RunnerExecutor(client, cwd=str(tmp_path), shell="agent-1")
    two = RunnerExecutor(client, cwd=str(tmp_path), shell="agent-2")
    one.run(_cd_and_set("a", "WHO", "one"))
    two.run(_cd_and_set("b", "WHO", "two"))
    assert one.run(_echo_var_and_leaf("WHO"))["output"].split() == ["one", "a"]
    assert two.run(_echo_var_and_leaf("WHO"))["output"].split() == ["two", "b"]


def test_a_slow_shell_does_not_block_another(runner, tmp_path):
    _, client = runner
    slow = RunnerExecutor(client, cwd=str(tmp_path), shell="slow")
    fast = RunnerExecutor(client, cwd=str(tmp_path), shell="fast")
    marker = tmp_path / "started"
    worker = threading.Thread(target=lambda: slow.run(f"{_marker(marker)}; {_sleep(2)}"))
    worker.start()
    _await_started(marker)
    started = time.monotonic()
    assert fast.run("echo quick")["exit_code"] == 0
    # The slow shell is mid-command; the fast one must not be queued behind it. Measured
    # after the marker, so this is the fast shell's latency alone.
    assert time.monotonic() - started < 2.0
    worker.join()


def test_one_shell_runs_one_command_at_a_time(runner, tmp_path):
    _, client = runner
    ex = RunnerExecutor(client, cwd=str(tmp_path))
    started = tmp_path / "started"
    worker = threading.Thread(target=lambda: ex.run(f"{_marker(started)}; {_sleep(1.5)}"))
    worker.start()
    _await_started(started)
    second = ex.run("echo too-soon")
    worker.join()
    assert "running another command" in second["error"] and second["exit_code"] is None


def test_live_output_is_a_view_and_the_result_is_the_record(runner, tmp_path):
    _, client = runner
    seen: list[str] = []
    ex = RunnerExecutor(client, cwd=str(tmp_path), on_output=seen.append)
    fast = ex.run("echo instant")
    assert fast["output"].startswith("instant") and seen == []  # too quick for a live frame
    ticks = f'foreach ($i in 1..3) {{ "tick $i"; {_sleep(0.4)} }}' if _WIN else "for i in 1 2 3; do echo tick $i; sleep 0.4; done"
    slow = ex.run(ticks)
    assert "tick 1" in "".join(seen)  # the view arrived while it ran
    assert all(f"tick {i}" in slow["output"] for i in (1, 2, 3))  # the record is complete


def test_user_stop_ends_the_command_and_the_session_carries_on(runner, tmp_path):
    """Same behaviour as the in-process executor: Stop ends the command within moments and
    the session carries on in the same folder. (Today a Stop also restarts the shell, in
    both modes, so variables are lost. That is a separate, older bug; this test does not
    pin it.)"""
    _, client = runner
    (tmp_path / "work").mkdir()
    ex = RunnerExecutor(client, cwd=str(tmp_path))
    ex.run("cd work")
    marker = tmp_path / "started"
    _when_started(marker, ex.interrupt_now)
    started = time.monotonic()
    stopped = ex.run(f"{_marker(marker)}; {_sleep(20)}", timeout=30)
    # Generous against the 20 s sleep: the point is that the stop ended it early, and the
    # window now also covers however long the loaded machine took to start the shell.
    assert time.monotonic() - started < 15
    assert stopped["error"] == "interrupted by user"
    assert ex.run(LEAF)["output"].strip() == "work"


# -- delivery after a break -------------------------------------------------------------


def test_a_cut_stream_resumes_and_no_result_is_lost(runner, tmp_path):
    _provider, client = runner
    (tmp_path / "away").mkdir()
    ex = RunnerExecutor(client, cwd=str(tmp_path))
    ex.run(_cd_and_set("away", "KEEP", "yes"))
    instance = client.instance_id
    box: dict = {}
    started = tmp_path / "started"
    worker = threading.Thread(
        target=lambda: box.update(
            ex.run(f"{_marker(started)}; {_sleep(1.5)}; echo finished-while-away")
        )
    )
    worker.start()
    _await_started(started)
    client._transport.proc.kill()  # the stream is cut while the command runs
    worker.join(timeout=20)
    assert "finished-while-away" in box.get("output", "")  # delivered after the resume
    assert client.instance_id == instance and client.restarts == 0  # same daemon
    assert ex.run(_echo_var_and_leaf("KEEP"))["output"].split() == ["yes", "away"]  # same shell


def test_the_same_request_id_runs_once(runner, tmp_path):
    _, client = runner
    target = tmp_path / "count.txt"
    append = f"Add-Content -Path '{target}' -Value x" if _WIN else f"echo x >> {target}"
    frame = P.encode(P.request("r-fixed", "shell.run", {"shell": "main", "command": append, "cwd": str(tmp_path)}))
    for _ in range(3):
        client._send(client._transport, frame)
    deadline = time.monotonic() + 10
    while not target.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)
    assert target.read_text() == "x\n"


def test_a_restarted_sandbox_fails_the_running_command_and_reopens_in_the_last_folder(
    runner, tmp_path, monkeypatch
):
    # A reconnect attempt waits `_HELLO_TIMEOUT` (60 s in production, sized for a cold VM
    # whose disk is still warming up) before it gives up and retries. A restart reliably
    # loses the first attempt or two -- the old workspace is closing, and the new daemon is
    # not listening yet -- so with the real budget one restart can take minutes, which is
    # how this test failed in CI with a 30 s join. What is under test is WHICH error the
    # in-flight command is given, not how patient the client is, so shrink the budget.
    monkeypatch.setattr(client_mod, "_HELLO_TIMEOUT", 5.0)
    provider, client = runner
    (tmp_path / "work").mkdir()
    ex = RunnerExecutor(client, cwd=str(tmp_path))
    ex.run(_cd_and_set("work", "GONE", "soon"))
    box: dict = {}
    started = tmp_path / "started"
    worker = threading.Thread(
        target=lambda: box.update(ex.run(f"{_marker(started)}; {_sleep(30)}", timeout=60))
    )
    worker.start()
    _await_started(started)
    provider.restart_daemon()  # a new runner instance, as after a sandbox restart
    # 5 attempts x 5 s plus backoff is ~32 s worst case; past that the client has given up
    # and the assertion below will say so.
    worker.join(timeout=60)
    assert not worker.is_alive(), "the restarted command never returned"
    assert "outcome of this command is unknown" in box["error"]  # never re-sent by the client
    assert client.restarts == 1
    after = ex.run(f'echo "[$env:GONE] $({LEAF})"' if _WIN else "echo [$GONE] $(basename $PWD)")
    assert after["output"].split() == ["[]", "work"]  # variables lost, folder kept


def test_a_relay_left_behind_goes_away_by_itself(tmp_path):
    """Spike finding C: a provider may leave the relay running after a stream is cut."""
    import subprocess

    zipapp = build_runner_zipapp(tmp_path / "dist")
    provider = RunnerLocalProvider(cwd=tmp_path, runner_path=zipapp)
    provider.create()
    try:
        relay = subprocess.Popen(
            [sys.executable, str(zipapp), "attach", "--socket", provider.socket_path, "--silence-seconds", "1"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert relay.wait(timeout=10) == 0  # nobody spoke: it left
    finally:
        provider.destroy()


# -- files ------------------------------------------------------------------------------


def test_files_are_read_in_pages_and_written_with_a_precondition(runner, tmp_path):
    _, client = runner
    big = tmp_path / "out.log"
    big.write_bytes("".join(f"line {i}\n" for i in range(1, 1001)).encode())  # the same bytes on Windows
    page = client.call("fs.read", {"path": str(big), "offset_line": 10, "limit_lines": 3}, timeout=10)
    assert page["text"] == "line 10\nline 11\nline 12\n"
    assert (page["start_line"], page["end_line"], page["total_lines"], page["eof"]) == (10, 12, 1000, False)

    raw = client.call("fs.read", {"path": str(big), "offset_bytes": 0, "limit_bytes": 6}, timeout=10)
    assert base64.b64decode(raw["data_b64"]) == b"line 1" and raw["eof"] is False

    note = tmp_path / "note.txt"
    written = client.call("fs.write", {"path": str(note), "text": "one\n"}, timeout=10)
    client.call("fs.write", {"path": str(note), "text": "two\n", "if_match_sha256": written["sha256"]}, timeout=10)
    with pytest.raises(P.RunnerError) as err:  # based on content that is no longer there
        client.call("fs.write", {"path": str(note), "text": "three\n", "if_match_sha256": written["sha256"]}, timeout=10)
    assert err.value.code == P.FS_ERROR and note.read_text() == "two\n"

    found = client.call("fs.search", {"path": str(tmp_path), "pattern": r"^line 99\d$"}, timeout=10)
    assert [m["line"] for m in found["matches"]] == list(range(990, 1000))


def test_a_relative_path_resolves_against_the_shells_folder(runner, tmp_path):
    _, client = runner
    (tmp_path / "sub").mkdir()
    write = "cd sub; Set-Content -Path here.txt -Value hello" if _WIN else "cd sub && echo hello > here.txt"
    RunnerExecutor(client, cwd=str(tmp_path), shell="agent-1").run(write)
    got = client.call("fs.read", {"path": "here.txt", "shell": "agent-1"}, timeout=10)
    assert got["text"].splitlines() == ["hello"]
    listing = client.call("fs.list", {"path": ".", "shell": "agent-1"}, timeout=10)
    assert [e["name"] for e in listing["entries"]] == ["here.txt"]


def test_proc_run_takes_an_argument_list_without_a_shell(runner, tmp_path):
    _, client = runner
    argv = [sys.executable, "-c", "import sys; print('|'.join(sys.argv[1:]))", "a b", "$HOME"]
    done = client.call("proc.run", {"argv": argv, "cwd": str(tmp_path)}, timeout=30)
    assert done["exit_code"] == 0 and done["stdout"].strip() == "a b|$HOME"  # no shell: nothing expanded


@pytest.mark.skipif(not _WIN, reason="the named pipe's security descriptor is Windows only")
def test_the_pipe_admits_only_the_listed_accounts(tmp_path):
    """On Windows the daemon may run as another account; its pipe must admit exactly the
    accounts it was given (the server's user). A pipe that lists only the Anonymous
    account refuses us, one that lists our own account does not."""
    import subprocess

    from coworker.sandbox import launch, winsec
    from coworker.sandbox.runner import winpipe

    zipapp = build_runner_zipapp(tmp_path / "dist")
    for sid, expect_refused in ((winsec.current_user_sid(), False), ("S-1-5-7", True)):
        folder, address = launch.runner_dir()
        daemon = subprocess.Popen(
            [*launch.runner_command(zipapp), "serve", "--socket", address, "--dir", folder, "--allow-sid", sid, "--cwd", str(tmp_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **launch.spawn_kwargs(),
        )  # fmt: skip
        try:
            launch.wait_for_runner(address, daemon)
            try:
                stream = winpipe.connect(address)
            except PermissionError:
                refused = True
            else:
                refused = False
                stream.close()
            assert refused is expect_refused, f"pipe for {sid}: refused={refused}"
        finally:
            daemon.kill()
            daemon.wait(timeout=10)
            shutil.rmtree(folder, ignore_errors=True)


# -- choosing a workspace ---------------------------------------------------------------


def test_direct_is_the_default_and_reports_no_enforcement(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENWORKER_SANDBOX_PROVIDER", raising=False)
    ws = open_workspace(cwd=tmp_path)
    try:
        assert isinstance(ws, DirectWorkspace)
        assert ws.describe()["provider"] == "direct" and ws.describe()["enforcement"] == "none"
    finally:
        ws.close()


def test_the_provider_setting_selects_the_runner(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWORKER_SANDBOX_PROVIDER", "runner-local")
    ws = open_workspace(cwd=tmp_path)
    try:
        assert isinstance(ws, RunnerWorkspace) and ws.describe()["provider"] == "runner-local"
        assert ws.executor.run("echo via-runner")["output"].strip() == "via-runner"
    finally:
        ws.close()
    with pytest.raises(ValueError):
        open_workspace(cwd=tmp_path, provider="no-such-thing")


def test_the_server_binary_can_run_as_the_runner(tmp_path):
    """`openworker-server sandbox-runner ...` is how the packaged app starts the runner
    (coworker/sandbox/launch.py): the same daemon, without loading the server."""
    import subprocess
    import sys

    from coworker.sandbox import launch

    assert launch.maybe_run_runner(["--port", "1"]) is False
    with pytest.raises(SystemExit):
        launch.maybe_run_runner(["sandbox-runner", "--help"])
    # From source the runner is the zipapp under -S; frozen it is the binary itself.
    assert launch.runner_command(tmp_path / "r.pyz")[:2] == [sys.executable, "-S"]
    done = subprocess.run([sys.executable, "-m", "coworker.server.run", "sandbox-runner", "--help"], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and "serve" in done.stdout and "attach" in done.stdout
