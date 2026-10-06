# Related work and positioning

Reviewed against primary project documentation on 2026-10-06. These links are
context, not a dependency list, code ancestry claim or endorsement.

| Project | Documented focus | Relationship to this example |
|---|---|---|
| [Ollama scheduling](https://docs.ollama.com/faq#how-does-ollama-handle-concurrent-requests) | Concurrent requests/model loading and a bounded pending-request queue | Already handles its own inference workload. GPUqueue adds coordination with participating external command processes and durable job records. |
| [GPU Task Spooler](https://github.com/justanhduc/task-spooler) | CPU/GPU command queue, GPU allocation and parallel job limits | A useful command-oriented alternative; GPUqueue explores Windows process ownership together with an HTTP inference path. |
| [simple_gpu_scheduler](https://github.com/ExpectationMax/simple_gpu_scheduler) | Runs submitted commands on selected GPUs using `CUDA_VISIBLE_DEVICES` | A deliberately small command scheduler. GPUqueue has a different, stateful desktop/HTTP scope. |
| [ollama-queue-proxy](https://github.com/TadMSTR/ollama-queue-proxy) | Ollama priority policy, per-client authentication, model-aware routing and failover | A useful proxy-oriented alternative. GPUqueue targets one trusted Windows account and combines local commands with inference; it does not implement that project's multi-client security model. |

The goal is to share an implementation idea that can be improved, not to claim
novelty or superiority. No comparative benchmark has been run. Workload fit,
platform, operational requirements and trust model matter more than feature count.
