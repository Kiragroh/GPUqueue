# Running and operating the prototype

## First installation

Install Python 3.13 and the requirements into the interpreter you will pass to
`Install.ps1`. Install Ollama separately and prepare the model you intend to use.
The installer is intentionally a **first-install** helper: it refuses existing
GPUqueue task definitions, installation configuration or a listener on port 11436.
It is not a migration tool for a private deployment.

The program directory is `%LOCALAPPDATA%\Programs\GPUqueue`. Runtime data lives
under `%LOCALAPPDATA%\GPUqueue\state`. Never copy that runtime directory to GitHub.
No machine-wide account or service is created. Availability begins at user logon;
there is no claim of service availability before login or after logout.

The public installer has source/parser verification; it has not been executed
against a clean Windows machine as part of this publication. The operational
local deployment used earlier installation tooling. Test the public installer in
a disposable Windows environment before adopting it on a managed workstation.

## Health and diagnostics

```powershell
Invoke-RestMethod http://127.0.0.1:11436/health
Get-ScheduledTask -TaskName 'GPUqueue Supervisor','GPUqueue Tray'
```

`worker_alive=true` confirms the scheduler thread, not successful model execution.
`watchdog.json`, `watchdog.log` and backend stdout/stderr logs are in the runtime
directory. A blocked job is distinct from an unavailable service. A CPU blockage
does not necessarily prevent GPU work, and vice versa, although same-model
ownership checks can also span lanes.

## Recovery

First inspect the exact job and its last state transitions. For commands, verify
the original owning process, child and descendants. For Ollama, verify backend
state and loaded models. A timeout does not prove inference stopped.

`client.py recover JOB_ID --confirmed-idle` is an **operator assertion**, not an
automatic diagnostic. Use it only after independently confirming safe cleanup.
It marks an uncertain job interrupted; it does not replay the request. Review any
saved output before intentionally resubmitting work. Never acknowledge recovery
speculatively to make the display green.

Known issue: some CPU embedding responses have been saved successfully and then
entered recovery during model release. The current generic error does not expose
the underlying cleanup exception. This publication records the limitation rather
than silently changing the cleanup policy.

## Process identity and virtual environments

The command client verifies process PID and birth time, and the guard requires
the current process to be the registered child. Windows virtual-environment
redirectors may add another process layer and fail that ownership check. Do not
disable the guard. Use a tested base interpreter with the required packages,
or implement and test a same-process environment bootstrap.

`GPUQUEUE_PYTHON` can select the interpreter used by an unwrapped guarded entry
point to start its supervisor. That interpreter needs this project's dependencies.
It does not change the rule that the eventual workload process must have the
registered identity.

## Updating and removing

Do not overwrite the live broker or stop the supervisor's task while jobs run:
Windows may also terminate task descendants. Drain new starts, wait for all active
work to complete, resolve recovery states explicitly, and take a consistent SQLite
backup before a planned backend update. Keep the existing runtime directory and
Windows user identity; DPAPI ciphertext is bound to that identity.

The tray can be updated separately while backend work continues. Its autostart
checkbox only toggles the existing `GPUqueue Tray` task. No extra Startup-folder
entry is needed. An automated public updater/uninstaller is not provided yet.

To retire a deployment, first drain and verify it idle, then stop and unregister
only its two tasks. Preserve/export needed history before manually removing its
known program/runtime directories. Never kill unrelated CUDA or Ollama processes.
