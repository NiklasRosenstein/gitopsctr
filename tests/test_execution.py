from __future__ import annotations

import io
import subprocess
import sys

import pytest

from gitopsctr.execution import (
    CommandOutput,
    DriverExecution,
    SubprocessCommandExecutor,
    TextDriverOutput,
)


def execution() -> tuple[DriverExecution, io.StringIO]:
    stream = io.StringIO()
    transcript = TextDriverOutput(stream)
    return DriverExecution(output=transcript, commands=SubprocessCommandExecutor(transcript)), stream


@pytest.mark.parametrize("mode", list(CommandOutput))
def test_command_output_modes_are_string_enums(mode):
    assert CommandOutput(mode.value) is mode


def test_executor_rejects_bare_string_output_modes():
    runtime, _ = execution()
    with pytest.raises(TypeError, match="CommandOutput"):
        runtime.run(sys.executable, "-c", "pass", output="capture")  # type: ignore[arg-type]


def test_driver_messages_and_streamed_command_output_share_the_child_transcript():
    runtime, transcript = execution()

    runtime.write("driver message\nsecond line")
    result = runtime.run(
        sys.executable,
        "-c",
        "import sys; print('command stdout'); print('command stderr', file=sys.stderr)",
    )

    assert result.stdout == ""
    assert result.stderr == ""
    lines = transcript.getvalue().splitlines()
    assert lines[:2] == [
        "    | driver message",
        "    | second line",
    ]
    assert set(lines[2:]) == {"    | command stdout", "    | command stderr"}


def test_capture_is_silent_and_tee_both_renders_and_returns_output():
    runtime, transcript = execution()
    command = (sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)")

    captured = runtime.run(*command, output=CommandOutput.CAPTURE)
    assert captured.stdout == "out\n"
    assert captured.stderr == "err\n"
    assert transcript.getvalue() == ""

    teed = runtime.run(*command, output=CommandOutput.TEE)
    assert teed.stdout == "out\n"
    assert teed.stderr == "err\n"
    assert set(transcript.getvalue().splitlines()) == {"    | out", "    | err"}


def test_discard_suppresses_output_and_capture_failure_replays_diagnostics():
    runtime, transcript = execution()
    runtime.run(sys.executable, "-c", "print('discarded')", output=CommandOutput.DISCARD)
    assert transcript.getvalue() == ""

    with pytest.raises(subprocess.CalledProcessError) as raised:
        runtime.run(
            sys.executable,
            "-c",
            "import sys; print('failed', file=sys.stderr); raise SystemExit(7)",
            output=CommandOutput.CAPTURE,
        )

    assert raised.value.returncode == 7
    assert raised.value.stderr == "failed\n"
    assert transcript.getvalue() == "    | failed\n"

    secret_runtime, secret_transcript = execution()
    with pytest.raises(subprocess.CalledProcessError):
        secret_runtime.run(
            sys.executable,
            "-c",
            "import sys; print('secret', file=sys.stderr); raise SystemExit(1)",
            output=CommandOutput.CAPTURE,
            sensitive=True,
        )
    assert secret_transcript.getvalue() == ""
