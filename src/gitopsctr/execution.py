"""Typed execution and transcript boundary for unit plugins."""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TextIO


class CommandOutput(StrEnum):
    """How an external command's output is handled."""

    STREAM = "stream"
    CAPTURE = "capture"
    TEE = "tee"
    DISCARD = "discard"


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


class DriverOutput(Protocol):
    """Receives human-readable output produced inside a plugin execution."""

    def write(self, text: str) -> None:
        """Write one or more lines to the driver transcript."""


class CommandExecutor(Protocol):
    """Executes external commands without exposing their output to the terminal directly."""

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        output: CommandOutput = CommandOutput.STREAM,
        check: bool = True,
        sensitive: bool = False,
    ) -> CommandResult:
        """Run a command using the requested typed output mode."""


@dataclass(frozen=True)
class TextDriverOutput:
    """Render plugin output as an indented child transcript."""

    stream: TextIO
    prefix: str = "    | "

    def write(self, text: str) -> None:
        if not text:
            return
        for line in text.splitlines():
            print(f"{self.prefix}{line}", file=self.stream, flush=True)


@dataclass(frozen=True)
class SubprocessCommandExecutor:
    transcript: DriverOutput

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        output: CommandOutput = CommandOutput.STREAM,
        check: bool = True,
        sensitive: bool = False,
    ) -> CommandResult:
        if not isinstance(output, CommandOutput):
            raise TypeError("output must be a CommandOutput")
        command = tuple(args)
        if output in {CommandOutput.CAPTURE, CommandOutput.DISCARD}:
            completed = subprocess.run(
                command,
                check=False,
                text=True,
                cwd=cwd,
                env=dict(env) if env is not None else None,
                input=input_text,
                capture_output=output is CommandOutput.CAPTURE,
                stdout=subprocess.DEVNULL if output is CommandOutput.DISCARD else None,
                stderr=subprocess.DEVNULL if output is CommandOutput.DISCARD else None,
            )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
        else:
            stdout, stderr, returncode = self._stream(
                command,
                cwd=cwd,
                env=env,
                input_text=input_text,
                retain=output is CommandOutput.TEE,
            )
            completed = subprocess.CompletedProcess(command, returncode, stdout, stderr)
        if check and completed.returncode != 0:
            if output is CommandOutput.CAPTURE and not sensitive:
                self.transcript.write(stdout)
                self.transcript.write(stderr)
            raise subprocess.CalledProcessError(completed.returncode, command, output=stdout, stderr=stderr)
        retained_stdout = stdout if output in {CommandOutput.CAPTURE, CommandOutput.TEE} else ""
        retained_stderr = stderr if output in {CommandOutput.CAPTURE, CommandOutput.TEE} else ""
        return CommandResult(command, completed.returncode, retained_stdout, retained_stderr)

    def _stream(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        input_text: str | None,
        retain: bool,
    ) -> tuple[str, str, int]:
        process = subprocess.Popen(
            command,
            text=True,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        events: queue.Queue[tuple[str, str | None]] = queue.Queue()

        def read_stream(name: str, stream: TextIO) -> None:
            for line in stream:
                events.put((name, line))
            events.put((name, None))

        threads = [
            threading.Thread(target=read_stream, args=("stdout", process.stdout), daemon=True),
            threading.Thread(target=read_stream, args=("stderr", process.stderr), daemon=True),
        ]
        for thread in threads:
            thread.start()
        if input_text is not None:
            assert process.stdin is not None
            try:
                process.stdin.write(input_text)
            except BrokenPipeError:
                pass
            finally:
                process.stdin.close()

        chunks: dict[str, list[str]] = {"stdout": [], "stderr": []}
        completed_streams = 0
        while completed_streams < 2:
            name, line = events.get()
            if line is None:
                completed_streams += 1
                continue
            if retain:
                chunks[name].append(line)
            self.transcript.write(line)
        for thread in threads:
            thread.join()
        return "".join(chunks["stdout"]), "".join(chunks["stderr"]), process.wait()


@dataclass(frozen=True)
class DriverExecution:
    """Controller-provided services available during plugin execution."""

    output: DriverOutput
    commands: CommandExecutor

    @classmethod
    def console(cls) -> DriverExecution:
        output = TextDriverOutput(sys.stderr)
        return cls(output=output, commands=SubprocessCommandExecutor(output))

    def write(self, text: str) -> None:
        self.output.write(text)

    def run(
        self,
        *args: str,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        output: CommandOutput = CommandOutput.STREAM,
        check: bool = True,
        sensitive: bool = False,
    ) -> CommandResult:
        return self.commands.run(
            args,
            cwd=cwd,
            env=env,
            input_text=input_text,
            output=output,
            check=check,
            sensitive=sensitive,
        )


def default_driver_execution() -> DriverExecution:
    return DriverExecution.console()
