#!/usr/bin/env bash
# Install the tool-calling-poc harness into an AI-DLC project.
#
# Usage: install.sh <project-dir> [space] [harness-dir]
#   space        defaults to aidlc/active-space (else "default")
#   harness-dir  defaults to the first of .claude .codex .kiro .cursor .opencode
#                that contains aidlc-common/stages
#
# Safe to re-run. Files it replaces are kept once as *.before-tool-calling-poc.
# Re-run after `aidlc config` refreshes the harness tree: stage files and
# sensors there are engine-owned and get overwritten.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
PROJECT=$(cd "${1:?usage: install.sh <project-dir> [space] [harness-dir]}" && pwd)
SPACE=${2:-$(cat "$PROJECT/aidlc/active-space" 2>/dev/null || echo default)}
HARNESS=${3:-}

if [ -z "$HARNESS" ]; then
  for candidate in .claude .codex .kiro .cursor .opencode; do
    if [ -d "$PROJECT/$candidate/aidlc-common/stages" ]; then HARNESS=$candidate; break; fi
  done
fi
HARNESS_DIR="$PROJECT/$HARNESS"
SPACE_DIR="$PROJECT/aidlc/spaces/$SPACE"
[ -n "$HARNESS" ] && [ -d "$HARNESS_DIR/aidlc-common/stages" ] || { echo "AI-DLC harness tree not found in $PROJECT" >&2; exit 1; }
[ -d "$SPACE_DIR/memory" ] || { echo "space not found: $SPACE_DIR (create it with /aidlc space create $SPACE)" >&2; exit 1; }

place() {  # place <src> <dst>: copy, keeping the first replaced original
  local src=$1 dst=$2
  mkdir -p "$(dirname "$dst")"
  if [ -f "$dst" ] && ! cmp -s "$src" "$dst" && [ ! -f "$dst.before-tool-calling-poc" ]; then
    cp "$dst" "$dst.before-tool-calling-poc"
  fi
  cp "$src" "$dst"
  echo "  $dst"
}

echo "space: $SPACE   harness: $HARNESS"

echo "[1/5] Team Knowledge"
place "$HERE/space/knowledge/aidlc-shared/tool-calling-for-llm-callers.md" \
      "$SPACE_DIR/knowledge/aidlc-shared/tool-calling-for-llm-callers.md"

echo "[2/5] Templates"
for template in "$HERE"/space/memory/templates/*.md; do
  place "$template" "$SPACE_DIR/memory/templates/$(basename "$template")"
done

echo "[3/5] Rules"
if grep -q "BEGIN tool-calling-poc rules" "$SPACE_DIR/memory/team.md" 2>/dev/null; then
  echo "  already present in $SPACE_DIR/memory/team.md"
else
  printf '\n' >> "$SPACE_DIR/memory/team.md"
  cat "$HERE/space/memory/team-rules.tool-calling.md" >> "$SPACE_DIR/memory/team.md"
  echo "  appended to $SPACE_DIR/memory/team.md"
fi

echo "[4/5] Sensors"
place "$HERE/harness/sensors/aidlc-tool-contract.md" "$HARNESS_DIR/sensors/aidlc-tool-contract.md"
place "$HERE/harness/sensors/aidlc-tool-eval.md" "$HARNESS_DIR/sensors/aidlc-tool-eval.md"
place "$HERE/harness/tools/aidlc-sensor-tool-contract.ts" "$HARNESS_DIR/tools/aidlc-sensor-tool-contract.ts"
place "$HERE/harness/tools/aidlc-sensor-tool-eval.ts" "$HARNESS_DIR/tools/aidlc-sensor-tool-eval.ts"
python3 - "$HARNESS_DIR/aidlc-common/stages/construction" <<'PY'
import re, sys
from pathlib import Path

# Import each sensor into its stage: frontmatter `sensors:` list and the
# body's `Imports:` line, which mirrors it.
stages = Path(sys.argv[1])
for stage, sensor in (("code-generation", "tool-contract"), ("build-and-test", "tool-eval")):
    path = stages / f"{stage}.md"
    text = path.read_text(encoding="utf-8")
    _, front, body = text.split("---\n", 2)
    if re.search(rf"^  - {re.escape(sensor)}$", front, re.M):
        print(f"  {path.name}: already imports {sensor}")
        continue
    front = re.sub(r"^(sensors:\n(?:  - .+\n)*)", rf"\1  - {sensor}\n", front, count=1, flags=re.M)
    body = re.sub(r"^(Imports: .*?)\.$", rf"\1, `{sensor}`.", body, count=1, flags=re.M)
    path.write_text(f"---\n{front}---\n{body}", encoding="utf-8")
    print(f"  {path.name}: imports {sensor}")
PY

echo "[5/5] Scope create-tool-draft"
place "$HERE/scopes/create-tool-draft.md" "$PROJECT/aidlc/scopes/create-tool-draft.md"

echo "Compiling stage graph"
(cd "$PROJECT" && aidlc engine graph compile)
echo "Done. Start a loop with: /aidlc --scope create-tool-draft <what the tool should do>"
