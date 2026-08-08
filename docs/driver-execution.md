# Driver execution

The controller supplies every unit-driver capability context with a `DriverExecution`. Drivers must use this boundary for
external commands and human-readable progress instead of printing or spawning subprocesses directly.

```python
from gitopsctr.execution import CommandOutput

context.execution.write("checking remote state")
result = context.execution.run("tool", "inspect", output=CommandOutput.CAPTURE)
```

Controller lifecycle messages such as `RUN`, `OBSERVE`, and `DONE` remain controller-owned. Plugin output is rendered
as a child transcript, keeping it visually distinct while reserving standard output for machine-readable command
results:

```text
    RUN      execute example reconciliation
    | external tool output
    OBSERVE  receipt published
```

## Output modes

- `CommandOutput.STREAM` renders output as it arrives and does not retain it in the returned result.
- `CommandOutput.CAPTURE` returns output without rendering it. Failed checked commands replay their diagnostics.
- `CommandOutput.TEE` renders and returns output.
- `CommandOutput.DISCARD` explicitly suppresses output.

The modes are a `StrEnum`; driver APIs should never pass untyped string modes.

Commands that return credentials or other secrets should pass `sensitive=True`. Their captured diagnostics remain on
the raised command error but are not automatically copied into the human transcript.
