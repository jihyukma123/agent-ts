// tool-contract sensor (tool-calling-poc).
//
// Fires at the code-generation gate. Reads the `json tool-contract` block from
// the active intent's requirements.md, loads the real Agent through a Python
// probe, and checks that the code matches the contract on the V1~V6 points a
// script can decide: tool set, descriptions, model-visible params, forbidden
// identity params, max_turns on every Runner call, value rules, untrusted
// result fields. Sufficiency of wording and model behavior stay with eval.
//
// Prints one JSON line: { pass, findings, findings_count, warnings }.

import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { isAbsolute, join, resolve } from "node:path";
import { spawnSync } from "node:child_process";

const MIN_DESCRIPTION_CHARS = 50;
const CRITERIA = ["V1", "V2", "V3", "V4", "V5", "V6"];

interface Finding {
	criterion: string;
	message: string;
}

interface ContractParam {
	name: string;
	validation?: string;
}

interface ContractTool {
	name: string;
	effect?: string;
	params?: ContractParam[];
	untrusted_result_fields?: string[];
}

interface Contract {
	version?: number;
	agent?: { module?: string; factory?: string; factory_args?: unknown[] };
	runner?: { max_turns?: number; source_globs?: string[] };
	forbidden_model_params?: string[];
	tools?: ContractTool[];
	eval?: { python?: string };
}

interface ProbeTool {
	name: string;
	description: string;
	schema: Record<string, unknown>;
}

interface Probe {
	tools: ProbeTool[];
	instructions: string | null;
	instructions_kind: string | null;
	runner_calls: { path: string; line: number; has_max_turns: boolean; max_turns: unknown }[];
	errors: string[];
}

const PROBE = String.raw`
import ast, glob, importlib, json, sys

contract = json.load(open(sys.argv[1], encoding="utf-8"))
out = {"tools": [], "instructions": None, "instructions_kind": None, "runner_calls": [], "errors": []}
try:
    spec = contract["agent"]
    target = getattr(importlib.import_module(spec["module"]), spec["factory"])
    agent = target(*spec.get("factory_args", [])) if callable(target) else target
    for tool in agent.tools:
        out["tools"].append({
            "name": getattr(tool, "name", ""),
            "description": getattr(tool, "description", "") or "",
            "schema": getattr(tool, "params_json_schema", None) or {},
        })
    instructions = agent.instructions
    if isinstance(instructions, str):
        out["instructions"], out["instructions_kind"] = instructions, "static"
    else:
        out["instructions_kind"] = "dynamic" if instructions else "none"
except Exception as exc:
    out["errors"].append(f"agent load failed: {type(exc).__name__}: {exc}")

seen = set()
for pattern in contract.get("runner", {}).get("source_globs") or ["**/*.py"]:
    for path in sorted(glob.glob(pattern, recursive=True)):
        if path in seen:
            continue
        seen.add(path)
        try:
            tree = ast.parse(open(path, encoding="utf-8").read(), path)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            func = getattr(node, "func", None)
            if (isinstance(node, ast.Call) and isinstance(func, ast.Attribute)
                    and func.attr in ("run", "run_sync", "run_streamed")
                    and isinstance(func.value, ast.Name) and func.value.id == "Runner"):
                value = next((k.value for k in node.keywords if k.arg == "max_turns"), None)
                literal = value.value if isinstance(value, ast.Constant) else None
                out["runner_calls"].append({
                    "path": path, "line": node.lineno,
                    "has_max_turns": value is not None, "max_turns": literal,
                })
print(json.dumps(out, ensure_ascii=False))
`;

function flag(argv: string[], name: string): string | undefined {
	const index = argv.indexOf(name);
	return index >= 0 ? argv[index + 1] : undefined;
}

function emit(findings: Finding[], warnings: string[]): never {
	console.log(JSON.stringify({
		pass: findings.length === 0,
		findings,
		findings_count: findings.length,
		warnings,
	}));
	process.exit(0);
}

// <...>/intents/<intent>/... → <...>/intents/<intent>
function intentRoot(outputPath: string): string | null {
	const match = /^(.*[\\/]intents[\\/][^\\/]+)[\\/]/.exec(outputPath);
	return match ? match[1] : null;
}

export function readContract(intentDir: string): { contract?: Contract; error?: string } {
	const path = join(intentDir, "inception", "requirements-analysis", "requirements.md");
	if (!existsSync(path)) return { error: `requirements.md not found: ${path}` };
	const blocks = [...readFileSync(path, "utf-8").matchAll(/```json tool-contract[ \t]*\r?\n([\s\S]*?)```/g)];
	if (blocks.length !== 1) {
		return { error: `requirements.md must contain exactly one \`json tool-contract\` block (found ${blocks.length})` };
	}
	try {
		return { contract: JSON.parse(blocks[0][1]) as Contract };
	} catch (error) {
		return { error: `tool-contract block is not valid JSON: ${(error as Error).message}` };
	}
}

export function pythonFor(projectDir: string, contract: Contract): string {
	const configured = contract.eval?.python;
	if (configured) return isAbsolute(configured) ? configured : join(projectDir, configured);
	const venv = join(projectDir, ".venv", "bin", "python");
	return existsSync(venv) ? venv : "python3";
}

function runProbe(projectDir: string, python: string, contract: Contract): Probe | string {
	const dir = mkdtempSync(join(tmpdir(), "tool-contract-"));
	try {
		const probePath = join(dir, "probe.py");
		const contractPath = join(dir, "contract.json");
		writeFileSync(probePath, PROBE, "utf-8");
		writeFileSync(contractPath, JSON.stringify(contract), "utf-8");
		const result = spawnSync(python, [probePath, contractPath], {
			cwd: projectDir,
			encoding: "utf-8",
			env: { ...process.env, PYTHONPATH: projectDir },
			timeout: 240_000,
		});
		if (result.error) return `python probe could not start (${python}): ${result.error.message}`;
		const line = (result.stdout ?? "").trim().split("\n").reverse().find((l) => l.startsWith("{"));
		if (!line) return `python probe produced no result (exit ${result.status}): ${(result.stderr ?? "").slice(-500)}`;
		return JSON.parse(line) as Probe;
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
}

function propertiesOf(schema: Record<string, unknown>): Record<string, Record<string, unknown>> {
	return (schema.properties ?? {}) as Record<string, Record<string, unknown>>;
}

// Nested object fields the model also fills (Pydantic models under $defs).
function nestedFieldsWithoutDescription(schema: Record<string, unknown>): string[] {
	const defs = (schema.$defs ?? {}) as Record<string, Record<string, unknown>>;
	const missing: string[] = [];
	for (const [defName, def] of Object.entries(defs)) {
		for (const [field, spec] of Object.entries(propertiesOf(def))) {
			if (!String(spec.description ?? "").trim()) missing.push(`${defName}.${field}`);
		}
	}
	return missing;
}

// Every property name anywhere in the schema, top level and $defs.
function allPropertyNames(schema: Record<string, unknown>): string[] {
	const names = Object.keys(propertiesOf(schema));
	const defs = (schema.$defs ?? {}) as Record<string, Record<string, unknown>>;
	for (const def of Object.values(defs)) names.push(...Object.keys(propertiesOf(def)));
	return names;
}

function check(contract: Contract, probe: Probe): { findings: Finding[]; warnings: string[] } {
	const findings: Finding[] = [];
	const warnings: string[] = [];
	const add = (criterion: string, message: string) => findings.push({ criterion, message });

	for (const error of probe.errors) add("contract", error);
	const contractTools = contract.tools ?? [];
	const codeTools = new Map(probe.tools.map((t) => [t.name, t]));
	const declared = new Set(contractTools.map((t) => t.name));

	// V1 — tool set, descriptions, model-visible params
	for (const name of codeTools.keys()) {
		if (!declared.has(name)) add("V1", `tool \`${name}\` is exposed to the model but missing from contract; declare it in \`requirements.md\` or remove from Agent tools`);
	}
	for (const tool of contractTools) {
		const code = codeTools.get(tool.name);
		if (!code) {
			add("V1", `contract tool \`${tool.name}\` is not exposed by the Agent; ensure it is registered in Agent tools`);
			continue;
		}
		if (code.description.trim().length < MIN_DESCRIPTION_CHARS) {
			add("V1", `\`${tool.name}\` description is ${code.description.trim().length} chars; write what it does, when to use, when not to use, and return values (minimum ${MIN_DESCRIPTION_CHARS} chars required)`);
		}
		const props = propertiesOf(code.schema);
		const contractParams = new Set((tool.params ?? []).map((p) => p.name));
		for (const param of Object.keys(props)) {
			if (!contractParams.has(param)) add("V1", `\`${tool.name}.${param}\` is visible to the model but missing from contract params; add it to contract params in requirements.md`);
			if (!String(props[param].description ?? "").trim()) add("V1", `\`${tool.name}.${param}\` has no description in the model-visible schema; add Field(description=...) or docstring Args entry`);
		}
		for (const param of contractParams) {
			if (!(param in props)) add("V1", `contract param \`${tool.name}.${param}\` is not in model-visible schema; add parameter to tool function or fix name`);
		}
		for (const field of nestedFieldsWithoutDescription(code.schema)) {
			add("V1", `\`${tool.name}\` nested field \`${field}\` has no description; add Field(description=...) to nested Pydantic model`);
		}
	}

	// V2 — instructions name every tool (floor; behavior is eval's job)
	if (probe.instructions_kind === "static" && probe.instructions !== null) {
		for (const name of codeTools.keys()) {
			if (!probe.instructions.includes(name)) add("V2", `Agent instructions never mention \`${name}\`; add guidance stating when to call it, when NOT to call it, and required prerequisites`);
		}
	} else if (probe.instructions_kind === "dynamic") {
		warnings.push("V2: instructions are dynamic; tool mentions not checked statically");
	} else {
		add("V2", "Agent has no instructions; define INSTRUCTIONS with tool usage policy");
	}

	// V3 — identity params never visible to the model
	const forbidden = new Set((contract.forbidden_model_params ?? []).map((p) => p.toLowerCase()));
	if (forbidden.size === 0) add("V3", "contract `forbidden_model_params` is empty");
	for (const tool of probe.tools) {
		for (const name of allPropertyNames(tool.schema)) {
			if (forbidden.has(name.toLowerCase())) add("V3", `\`${tool.name}\` exposes forbidden identity param \`${name}\` to model; remove it from tool signature and derive solely from server context`);
		}
	}

	// V4 — explicit max_turns on every Runner call
	const limit = contract.runner?.max_turns;
	if (typeof limit !== "number" || limit < 1) add("V4", "contract `runner.max_turns` must be a positive number");
	if (probe.runner_calls.length === 0) add("V4", "no `Runner.run*` call found under `runner.source_globs`");
	for (const call of probe.runner_calls) {
		const where = `${call.path}:${call.line}`;
		if (!call.has_max_turns) add("V4", `${where} calls Runner without \`max_turns\`; pass explicit max_turns=${limit} to prevent infinite tool loops`);
		else if (typeof call.max_turns === "number" && call.max_turns !== limit) {
			add("V4", `${where} uses max_turns=${call.max_turns} but the contract says ${limit}; align max_turns with contract value`);
		}
	}

	// V5 — every model-visible param has a value rule
	for (const tool of contractTools) {
		for (const param of tool.params ?? []) {
			if (!String(param.validation ?? "").trim()) add("V5", `contract param \`${tool.name}.${param.name}\` has no \`validation\` rule; specify value constraints (format, range, enum, existence check)`);
		}
	}

	// V6 — read tools name the fields carrying external text
	for (const tool of contractTools) {
		if (!Array.isArray(tool.untrusted_result_fields)) {
			add("V6", `\`${tool.name}\` must declare \`untrusted_result_fields\` list in contract (empty list only if returning no external text)`);
		} else if (tool.effect === "read" && tool.untrusted_result_fields.length === 0) {
			add("V6", `read tool \`${tool.name}\` declares no untrusted result fields; specify fields containing external user text (e.g. ['title', 'description'])`);
		}
	}

	return { findings, warnings };
}

export function main(argv: string[]): void {
	const outputPath = flag(argv, "--output-path");
	const projectDir = process.cwd();
	if (!outputPath) emit([{ criterion: "contract", message: "missing --output-path" }], []);
	const intentDir = intentRoot(resolve(projectDir, outputPath));
	if (!intentDir) emit([{ criterion: "contract", message: `cannot locate intent record from ${outputPath}` }], []);
	const { contract, error } = readContract(intentDir);
	if (!contract) emit([{ criterion: "contract", message: error ?? "unreadable contract" }], []);
	if (contract.version !== 1 || !contract.agent?.module || !contract.agent?.factory) {
		emit([{ criterion: "contract", message: "contract needs `version: 1` and `agent.module` / `agent.factory`" }], []);
	}
	const probe = runProbe(projectDir, pythonFor(projectDir, contract), contract);
	if (typeof probe === "string") emit([{ criterion: "contract", message: probe }], []);
	const { findings, warnings } = check(contract, probe);
	warnings.push(`criteria checked: ${CRITERIA.join(", ")}`);
	emit(findings, warnings);
}

if (import.meta.main) main(process.argv.slice(2));
