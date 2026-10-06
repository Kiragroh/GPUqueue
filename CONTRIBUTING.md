# Contributing

Open an issue with the platform, relevant component, expected behavior and a
minimal synthetic reproduction. Redact private paths and identifiers; omit tokens,
runtime databases, prompts and model responses.

Development uses Python 3.13 on Windows:

```powershell
py -3.13 -m pip install -r requirements-dev.txt
py -3.13 -m pytest coordinator/tests tests -q
.\Build.ps1
```

Tests use synthetic HTTP backends and isolated temporary state. Do not add tests
that depend on another contributor's live coordinator, installed models, user task
definitions or GPU workloads. Never make CI start inference.

For scheduler changes, test admission and cleanup independently. Preserve process
ownership checks, durable state, loopback boundaries and the explicit recovery
state. A failed HTTP request does not prove the underlying model stopped.

Architecture updates should be grounded in current source. The editable Archify
specification is `docs/architecture.json`; use the upstream
[Archify](https://github.com/tt-a1i/archify) validator/deliver workflow and inspect
the resulting HTML in both themes. Do not edit the generated HTML by hand.

Public documentation is English. Keep performance and reliability claims tied to
repeatable evidence and distinguish a unit test from a real-device acceptance test.
