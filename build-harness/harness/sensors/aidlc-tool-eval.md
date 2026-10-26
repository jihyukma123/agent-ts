---
id: tool-eval
kind: deterministic
command: bun tools/aidlc-sensor-tool-eval.ts
default_severity: blocking
fire_on: gate
description: Checks V1~V6 eval coverage, re-runs deterministic eval cases with pytest, and checks the summary reports every case
category: tool-calling
matches: "**/build-and-test/build-and-test-summary.md"
input_schema:
  output_path: string
  stage_slug: string
output_schema:
  pass: boolean
  findings: object[]
  findings_count: integer
  pytest_exit: integer
timeout_seconds: 1200
---

# tool-eval sensor

tool-calling-poc harness. Fires once at the build-and-test gate, on
`build-and-test-summary.md`.

From the `json tool-contract` block's `eval.cases`:

- every criterion V1~V6 has at least one `deterministic` case, and V2 and V6
  also have a `live` case;
- every deterministic case names a pytest node id, and all of them pass when
  re-run here;
- `build-and-test-summary.md` mentions every case id.

Live cases are not executed by the sensor (cost and nondeterminism). Their
results are reported in the summary's `## Live Model Eval` section, which the
`required-sections` template enforces, and judged by the human at the gate.
