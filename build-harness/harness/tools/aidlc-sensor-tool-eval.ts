// tool-eval sensor (tool-calling-poc).
//
// Fires at the build-and-test gate on build-and-test-summary.md. Checks that
// the contract's eval plan covers V1~V6 (a deterministic case for each, a live
// case for V2 and V6), re-runs every deterministic case with pytest, and
// checks that the summary reports every case id.
//
// Prints one JSON line: { pass, findings, findings_count, pytest_exit }.

import { existsSync, readFileSync } from "node:fs";
import { isAbsolute, join, resolve } from "node:path";
import { spawnSync } from "node:child_process";

const CRITERIA = ["V1", "V2", "V3", "V4", "V5", "V6"];
const LIVE_REQUIRED = ["V2", "V6"];

interface Finding {
	criterion: string;
	message: string;
}

interface EvalCase {
	id?: string;
	criterion?: string;
	kind?: string;
	test?: string;
}

interface Contract {
	eval?: { python?: string; cases?: EvalCase[] };
}

function flag(argv: string[], name: string): string | undefined {
	const index = argv.indexOf(name);
	return index >= 0 ? argv[index + 1] : undefined;
}

function emit(findings: Finding[], pytestExit: number | null): never {
	console.log(JSON.stringify({
		pass: findings.length === 0,
		findings,
		findings_count: findings.length,
		pytest_exit: pytestExit,
	}));
	process.exit(0);
}

function intentRoot(outputPath: string): string | null {
	const match = /^(.*[\\/]intents[\\/][^\\/]+)[\\/]/.exec(outputPath);
	return match ? match[1] : null;
}

function readContract(intentDir: string): { contract?: Contract; error?: string } {
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

function pythonFor(projectDir: string, contract: Contract): string {
	const configured = contract.eval?.python;
	if (configured) return isAbsolute(configured) ? configured : join(projectDir, configured);
	const venv = join(projectDir, ".venv", "bin", "python");
	return existsSync(venv) ? venv : "python3";
}

export function main(argv: string[]): void {
	const outputPath = flag(argv, "--output-path");
	const projectDir = process.cwd();
	if (!outputPath) emit([{ criterion: "contract", message: "missing --output-path" }], null);
	const summaryPath = resolve(projectDir, outputPath);
	const intentDir = intentRoot(summaryPath);
	if (!intentDir) emit([{ criterion: "contract", message: `cannot locate intent record from ${outputPath}` }], null);
	const { contract, error } = readContract(intentDir);
	if (!contract) emit([{ criterion: "contract", message: error ?? "unreadable contract" }], null);

	const findings: Finding[] = [];
	const cases = contract.eval?.cases ?? [];

	// Coverage: deterministic case per criterion, live case for V2 and V6
	for (const criterion of CRITERIA) {
		const own = cases.filter((c) => c.criterion === criterion);
		if (!own.some((c) => c.kind === "deterministic")) {
			findings.push({ criterion, message: `no deterministic eval case for ${criterion}` });
		}
		if (LIVE_REQUIRED.includes(criterion) && !own.some((c) => c.kind === "live")) {
			findings.push({ criterion, message: `no live-model eval case for ${criterion}` });
		}
	}
	for (const c of cases) {
		if (!c.id) findings.push({ criterion: c.criterion ?? "eval", message: "eval case without `id`" });
		if (c.kind === "deterministic" && !c.test) {
			findings.push({ criterion: c.criterion ?? "eval", message: `deterministic case \`${c.id}\` has no pytest node id in \`test\`` });
		}
	}

	// Re-run deterministic cases
	const nodeIds = [...new Set(cases.filter((c) => c.kind === "deterministic" && c.test).map((c) => c.test as string))];
	let pytestExit: number | null = null;
	if (nodeIds.length > 0) {
		const python = pythonFor(projectDir, contract);
		const result = spawnSync(python, ["-m", "pytest", "-q", "-p", "no:cacheprovider", ...nodeIds], {
			cwd: projectDir,
			encoding: "utf-8",
			env: { ...process.env, PYTHONPATH: projectDir },
			timeout: 900_000,
		});
		pytestExit = result.status;
		if (result.error) {
			findings.push({ criterion: "eval", message: `pytest could not start (${python}): ${result.error.message}` });
		} else if (result.status !== 0) {
			const tail = `${result.stdout ?? ""}\n${result.stderr ?? ""}`.trim().split("\n").slice(-15).join("\n");
			findings.push({ criterion: "eval", message: `deterministic eval cases failed (pytest exit ${result.status}):\n${tail}` });
		}
	}

	// Summary reports every case
	const summary = existsSync(summaryPath) ? readFileSync(summaryPath, "utf-8") : "";
	for (const c of cases) {
		if (c.id && !summary.includes(c.id)) {
			findings.push({ criterion: c.criterion ?? "eval", message: `build-and-test-summary.md does not report case \`${c.id}\`` });
		}
	}

	emit(findings, pytestExit);
}

if (import.meta.main) main(process.argv.slice(2));
