# Publication verification

Date: 2026-10-06. Platform: Windows, Python 3.13.

| Check | Result |
|---|---|
| Coordinator + watchdog synthetic tests | 84 passed |
| Desktop source boundary tests | 5 passed |
| Public desktop C# compilation | Passed |
| Compiled desktop with synthetic snapshots | 27 passed |
| Public installer PowerShell parser | Passed |
| Clean-machine public installer execution | Not performed |
| Real-model inference with the generalized public snapshot | Not performed |

The unit suites use isolated temporary state, synthetic HTTP backends and owned
test processes. They do not run model inference. No local runtime database,
token, private output, operational log or old Git history is published.

## Archify receipt

```text
diagram_type: architecture
output: docs/architecture.html
specification_sha256: cdc2494f657e03f7f75d34c922613299d1a68607c18f86e4a4f04a6302ef3650
artifact_sha256: f7a855eba4e859a16c6f4ab2499fd4034cee15c1ce064fe3e3c4c3817619926a
validation: 9/9 showcase, 0 errors, 0 warnings
browser_evidence: passed
visual_review: passed
correction_rounds: 1
```

Browser containment passed at 1440x900, 1600x1000, 1920x1080 and 2048x1320.
Light/dark screenshots were captured at both endpoint sizes. Final light
1440x900 and dark 2048x1320 renders were visually inspected for label fit,
crossings, whitespace and clipping. The screenshot preview is synthetic
documentation; it contains no live job data.

The declaration above distinguishes deterministic diagram validation, real
browser measurements and visual inspection. It is not a runtime acceptance
test of the GPU scheduler.

The first hosted Windows run exposed a hardware dependency in two legacy broker
tests: their fixture fell back to the host's NVIDIA probe. The fixture now supplies
fresh synthetic GPU telemetry, so those HTTP/lifecycle tests require no physical
GPU. No production scheduling policy was relaxed to accommodate CI.
