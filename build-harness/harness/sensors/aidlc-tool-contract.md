---
id: tool-contract
kind: deterministic
command: bun tools/aidlc-sensor-tool-contract.ts
default_severity: blocking
fire_on: gate
description: Checks that the Agent's real tools, instructions, and Runner calls match the requirements tool-contract block (V1~V6)
category: tool-calling
matches: "**/code-generation/code-summary.md"
input_schema:
  output_path: string
  stage_slug: string
output_schema:
  pass: boolean
  findings: object[]
  findings_count: integer
  warnings: string[]
timeout_seconds: 300
---

# tool-contract sensor

tool-calling-poc harness. Fires once at the code-generation gate, on
`code-summary.md`.

It reads the single `json tool-contract` block in the active intent's
`inception/requirements-analysis/requirements.md`, loads the Agent named by
`agent.module` / `agent.factory` with the project's Python, and compares:

| Criterion | Check |
|---|---|
| V1 | Contract tools = Agent tools; description at least 50 chars; contract params = model-visible params; every param and nested field has a description |
| V2 | Static instructions mention every tool name |
| V3 | No `forbidden_model_params` name appears anywhere in a tool schema |
| V4 | Every `Runner.run*` call under `runner.source_globs` passes `max_turns`, equal to the contract when literal |
| V5 | Every contract param has a `validation` rule |
| V6 | Every tool declares `untrusted_result_fields`; read tools declare at least one |

It checks that decisions reached the code. Whether descriptions and
instructions are good enough, and whether the model behaves, is decided by the
`tool-eval` sensor's cases and by the human at the gate.
