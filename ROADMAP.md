# Roadmap and known limitations

The current code is a reference prototype. Items below are opportunities for
development, not implemented guarantees.

- [ ] Diagnose model-release failures with safe stage/error-class metadata; add
  regression coverage for real Ollama embedding-model cleanup behavior.
- [ ] Clean-machine validation of the public installer, rollback and uninstall.
- [ ] English desktop/web localization and an explicit language setting.
- [ ] Configurable, validated CPU embedding profiles instead of a single alias.
- [ ] Configurable admission policy and better measured peak-memory estimates.
- [ ] Bounded retention/export for durable history and response storage.
- [ ] Per-application identity and access controls if the trust model expands.
- [ ] Additional backend compatibility tests for streaming, cancellation and
  uncommon OpenAI-style features. No universal drop-in API claim.
- [ ] Reproducible workload benchmarks: latency, fairness, throughput, memory and
  failure recovery compared with native Ollama scheduling and other queues.
- [ ] A tested process-identity recipe for more Windows virtual environments.
- [ ] Signed release tooling and appropriate endpoint-protection review.
- [ ] Explore multi-GPU or other platforms as separate design work, not a flag
  that pretends Windows Job Objects and DPAPI are portable.

Keep conservative behavior until stronger guarantees are demonstrated. Do not
turn uncertain cleanup into automatic replay or unconditional reservation release.
