# Driver execution

The controller gives each unit-driver capability a `DriverExecution` context. Drivers must use it for external commands
and progress output. Drivers must not print or start subprocesses directly.

```python
from gitopsctr.execution import CommandOutput

context.execution.write("checking remote state")
result = context.execution.run("tool", "inspect", output=CommandOutput.CAPTURE)
```

The controller owns lifecycle messages such as `RUN`, `OBSERVE`, and `DONE`. Plugin output appears as a child
transcript. Standard output stays available for machine-readable results:

```text
    RUN      execute example reconciliation
    | external tool output
    OBSERVE  receipt published
```

## Output modes

- `CommandOutput.STREAM` renders output as it arrives and does not retain it in the returned result.
- `CommandOutput.CAPTURE` returns output without rendering it. A failed checked command replays its diagnostics.
- `CommandOutput.TEE` renders and returns output.
- `CommandOutput.DISCARD` explicitly suppresses output.

The modes are a `StrEnum`; driver APIs should never pass untyped string modes.

Commands that return credentials or other secrets must pass `sensitive=True`. Captured diagnostics stay on the raised
command error. They are not copied to the human transcript.
