"""Agent 실행 실시간 Trace 로거 (별도 터미널 모니터링용).

`Runner.run(..., hooks=...)` 의 `RunHooks` 를 통해 한 턴(Turn) 동안 발생하는 핵심 흐름만 간결하게 기록합니다:
1. 사용자 입력 (`[TURN START]`)
2. LLM이 어떤 Tool 호출을 요청했는지 (`<tool> tool 호출을 LLM이 요청`)
3. 해당 Tool이 어떤 인자로 실행되는지 (`<tool> tool을 다음 인자로 실행`, 불필요한 null 인자 생략)
4. Tool 실행 결과 Output (`<tool> tool 실행 결과 (Output → LLM에 전달됨)`)
5. LLM 최종 응답 메시지 (`[LLM 최종 응답]`)
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from agents import Agent, ModelResponse, RunContextWrapper, RunHooks, Tool

from app.agent.context import AgentUserContext

_SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-*]{4,}")

# 터미널 가독성을 위한 ANSI 색상 코드
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_MAGENTA = "\033[35m"
_RED = "\033[31m"


def _redact(text: str) -> str:
    return _SECRET_PATTERN.sub("sk-***", text)


def _now_str() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _strip_nulls(value: Any) -> Any:
    """Tool 인자 등에서 불필요하게 나열되는 null(None) 필드를 제거해 핵심 인자만 남깁니다."""
    if isinstance(value, dict):
        cleaned = {}
        for k, v in value.items():
            if v is None:
                continue
            cleaned[k] = _strip_nulls(v)
        return cleaned
    if isinstance(value, list):
        return [_strip_nulls(item) for item in value]
    return value


def _is_flat_primitive(value: Any) -> bool:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return True
    return False


def _compact_json_lines(value: Any, indent: int = 0) -> list[str]:
    """가독성을 위해 짧은 배열이나 단순 객체는 한 줄로, 중첩 구조는 들여쓰기로 포맷팅합니다."""
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return [f"{pad}{{}}"]
        if all(_is_flat_primitive(v) for v in value.values()) and len(value) <= 5:
            one_line = (
                "{ "
                + ", ".join(
                    f"{json.dumps(k, ensure_ascii=False)}: {json.dumps(v, ensure_ascii=False)}"
                    for k, v in value.items()
                )
                + " }"
            )
            if len(one_line) <= 110:
                return [f"{pad}{one_line}"]
        lines = [f"{pad}{{"]
        items = list(value.items())
        for idx, (k, v) in enumerate(items):
            comma = "," if idx < len(items) - 1 else ""
            key_str = json.dumps(k, ensure_ascii=False)
            child_lines = _compact_json_lines(v, indent + 1)
            first_child = child_lines[0].lstrip()
            if len(child_lines) == 1:
                lines.append(f"{pad}  {key_str}: {first_child}{comma}")
            else:
                lines.append(f"{pad}  {key_str}: {first_child}")
                lines.extend(child_lines[1:-1])
                lines.append(f"{child_lines[-1]}{comma}")
        lines.append(f"{pad}}}")
        return lines

    if isinstance(value, list):
        if not value:
            return [f"{pad}[]"]
        if all(_is_flat_primitive(item) for item in value):
            one_line = "[" + ", ".join(json.dumps(item, ensure_ascii=False) for item in value) + "]"
            if len(one_line) <= 100:
                return [f"{pad}{one_line}"]
        lines = [f"{pad}["]
        for idx, item in enumerate(value):
            comma = "," if idx < len(value) - 1 else ""
            child_lines = _compact_json_lines(item, indent + 1)
            if len(child_lines) == 1:
                lines.append(f"{child_lines[0]}{comma}")
            else:
                lines.extend(child_lines[:-1])
                lines.append(f"{child_lines[-1]}{comma}")
        lines.append(f"{pad}]")
        return lines

    return [f"{pad}{json.dumps(value, ensure_ascii=False)}"]


def _parse_json_if_possible(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        stripped = raw.strip()
        if (stripped.startswith("{") and stripped.endswith("}")) or (
            stripped.startswith("[") and stripped.endswith("]")
        ):
            try:
                return json.loads(stripped)
            except Exception:
                return None
    return None


def _format_payload(raw: Any, *, strip_null_fields: bool = False, indent_prefix: str = "   ") -> str:
    parsed = _parse_json_if_possible(raw)
    if parsed is not None:
        if strip_null_fields:
            parsed = _strip_nulls(parsed)
        lines = _compact_json_lines(parsed, indent=0)
        text = "\n".join(lines)
    else:
        text = str(raw)

    text = _redact(text)
    out_lines = text.splitlines() or [""]
    return "\n".join(f"{indent_prefix}{line}" for line in out_lines)


class AgentTraceWriter:
    """별도 터미널에서 `tail -f` 또는 `watch_trace.py` 로 볼 수 있도록 파일에 즉시 flush 하는 기록기."""

    def __init__(self, trace_path: Path, *, enabled: bool = True) -> None:
        self.trace_path = trace_path
        self.enabled = enabled

    def prepare_file(self) -> None:
        if not self.enabled:
            return
        try:
            self.trace_path.parent.mkdir(parents=True, exist_ok=True)
            self.trace_path.touch(exist_ok=True)
        except OSError:
            pass

    def write_block(self, text: str) -> None:
        if not self.enabled:
            return
        safe_text = _redact(text)
        try:
            self.trace_path.parent.mkdir(parents=True, exist_ok=True)
            with self.trace_path.open("a", encoding="utf-8") as f:
                f.write(safe_text)
                if not safe_text.endswith("\n"):
                    f.write("\n")
                f.flush()
        except OSError:
            pass


def resolve_trace_writer(default_data_dir: Path) -> AgentTraceWriter:
    """환경변수(AGENT_TRACE, AGENT_TRACE_FILE) 또는 기본 경로(data/agent_trace.log)로 Writer를 생성합니다."""
    raw_enabled = os.environ.get("AGENT_TRACE", "1").strip().lower()
    enabled = raw_enabled not in ("0", "false", "off", "no")
    custom_file = os.environ.get("AGENT_TRACE_FILE", "").strip()
    trace_path = Path(custom_file) if custom_file else (default_data_dir / "agent_trace.log")
    return AgentTraceWriter(trace_path=trace_path, enabled=enabled)


class AgentTraceHooks(RunHooks[AgentUserContext]):
    """한 번의 `Runner.run` 실행 동안 핵심 LLM 결정 및 Tool 실행/결과만 간결하게 기록하는 Hook."""

    def __init__(
        self,
        writer: AgentTraceWriter,
        *,
        user_info: str,
        conversation_id: str,
        user_message: str,
        model_name: str,
    ) -> None:
        self.writer = writer
        self.user_info = user_info
        self.conversation_id = conversation_id
        self.user_message = user_message
        self.model_name = model_name
        self.turn_start_mono = time.monotonic()
        self.llm_step = 0
        self.tool_count = 0
        self._llm_start_mono: float | None = None
        self._tool_start_mono: float | None = None

    def emit_turn_start(self) -> None:
        short_conv = self.conversation_id[:8] if self.conversation_id else "-"
        lines = [
            "",
            f"{_BOLD}{_CYAN}================================================================================{_RESET}",
            f"{_BOLD}{_CYAN}🟢 [TURN START]{_RESET} {_now_str()} | user={_BOLD}{self.user_info}{_RESET} | conv={short_conv}",
            f"{_BOLD}💬 사용자 입력:{_RESET}",
            _format_payload(self.user_message, indent_prefix="   "),
            f"{_DIM}--------------------------------------------------------------------------------{_RESET}",
        ]
        self.writer.write_block("\n".join(lines))

    async def on_llm_start(
        self,
        context: RunContextWrapper[AgentUserContext],
        agent: Agent[AgentUserContext],
        system_prompt: str | None,
        input_items: list[Any],
    ) -> None:
        # 매 스텝마다 누적되는 encrypted_content 및 중복 input_items 출력은 생략하고 타이머만 기록합니다.
        self.llm_step += 1
        self._llm_start_mono = time.monotonic()

    async def on_llm_end(
        self,
        context: RunContextWrapper[AgentUserContext],
        agent: Agent[AgentUserContext],
        response: ModelResponse,
    ) -> None:
        elapsed = (
            f"{time.monotonic() - self._llm_start_mono:.2f}s"
            if self._llm_start_mono is not None
            else "-"
        )
        usage = getattr(response, "usage", None)
        usage_str = ""
        if usage is not None:
            in_tok = getattr(usage, "input_tokens", 0) or 0
            out_tok = getattr(usage, "output_tokens", 0) or 0
            if in_tok or out_tok:
                usage_str = f", tokens: in={in_tok}/out={out_tok}"

        outputs = getattr(response, "output", []) or []
        lines: list[str] = []

        for item in outputs:
            item_type = getattr(item, "type", None) or (
                item.get("type") if isinstance(item, dict) else None
            )
            if item_type == "reasoning":
                # 요약 텍스트가 실제로 존재할 때만 출력하고, 빈 reasoning 항목은 생략합니다.
                summary_list = getattr(item, "summary", None) or []
                summary_texts = [
                    getattr(s, "text", "") for s in summary_list if getattr(s, "text", "")
                ]
                if summary_texts:
                    lines.append(f"\n{_MAGENTA}🧠 [Step {self.llm_step} | Reasoning 요약]{_RESET}")
                    lines.append(
                        _format_payload("\n".join(summary_texts), indent_prefix="   ")
                    )

            elif item_type == "function_call":
                name = getattr(item, "name", "unknown")
                lines.append(
                    f"\n{_BOLD}{_YELLOW}🤖 [Step {self.llm_step}] {name} tool 호출을 LLM이 요청{_RESET} "
                    f"{_DIM}({_now_str()}, LLM 응답 {elapsed}{usage_str}){_RESET}"
                )

            elif item_type == "message":
                content_list = getattr(item, "content", None) or []
                texts: list[str] = []
                for part in content_list:
                    part_text = getattr(part, "text", None) or getattr(part, "refusal", None)
                    if part_text:
                        texts.append(str(part_text))
                merged = "\n".join(texts) if texts else str(item)
                lines.append(
                    f"\n{_BOLD}{_GREEN}💬 [Step {self.llm_step}] LLM 최종 응답{_RESET} "
                    f"{_DIM}({_now_str()}, 소요 {elapsed}{usage_str}){_RESET}:"
                )
                lines.append(_format_payload(merged, indent_prefix="   "))

        if lines:
            self.writer.write_block("\n".join(lines))

    async def on_tool_start(
        self,
        context: RunContextWrapper[AgentUserContext],
        agent: Agent[AgentUserContext],
        tool: Tool,
    ) -> None:
        self.tool_count += 1
        self._tool_start_mono = time.monotonic()
        tool_name = getattr(context, "tool_name", None) or getattr(tool, "name", "unknown")
        tool_args = getattr(context, "tool_arguments", None)

        lines = [
            f"{_BOLD}{_MAGENTA}🔧 {tool_name} tool을 다음 인자로 실행:{_RESET}"
        ]
        if tool_args is not None:
            lines.append(_format_payload(tool_args, strip_null_fields=True, indent_prefix="   "))
        else:
            lines.append("   {}")
        self.writer.write_block("\n".join(lines))

    async def on_tool_end(
        self,
        context: RunContextWrapper[AgentUserContext],
        agent: Agent[AgentUserContext],
        tool: Tool,
        result: object,
    ) -> None:
        elapsed = (
            f"{time.monotonic() - self._tool_start_mono:.2f}s"
            if self._tool_start_mono is not None
            else "-"
        )
        tool_name = getattr(context, "tool_name", None) or getattr(tool, "name", "unknown")

        lines = [
            f"{_BOLD}{_CYAN}📦 {tool_name} tool 실행 결과 (Output → LLM에 전달됨, {elapsed}):{_RESET}",
            _format_payload(result, strip_null_fields=False, indent_prefix="   "),
        ]
        self.writer.write_block("\n".join(lines))

    def emit_turn_end(self, *, presentation_kind: str | None = None) -> None:
        total_elapsed = f"{time.monotonic() - self.turn_start_mono:.2f}s"
        pres_note = f" | presentation={presentation_kind}" if presentation_kind else ""
        lines = [
            f"{_DIM}--------------------------------------------------------------------------------{_RESET}",
            f"{_BOLD}{_CYAN}🏁 [TURN END]{_RESET} {_now_str()} "
            f"(총 소요: {total_elapsed} | LLM 호출: {self.llm_step}회 | Tool 실행: {self.tool_count}회{pres_note})",
            f"{_BOLD}{_CYAN}================================================================================{_RESET}",
        ]
        self.writer.write_block("\n".join(lines))

    def emit_turn_error(self, exc: BaseException) -> None:
        total_elapsed = f"{time.monotonic() - self.turn_start_mono:.2f}s"
        status_code = getattr(exc, "status_code", None)
        status_note = f" (HTTP {status_code})" if status_code is not None else ""
        lines = [
            f"{_DIM}--------------------------------------------------------------------------------{_RESET}",
            f"{_BOLD}{_RED}❌ [TURN ERROR]{_RESET} {_now_str()} "
            f"(총 소요: {total_elapsed} | {type(exc).__name__}{status_note})",
            f"{_BOLD}{_CYAN}================================================================================{_RESET}",
        ]
        self.writer.write_block("\n".join(lines))
