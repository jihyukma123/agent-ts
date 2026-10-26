"""독립 Unit이 도구와 presentation mapper를 조립하는 fail-closed registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Mapping

from agents import Tool

from app.ticketing.models import TicketPresentationV1

ToolFactory = Callable[[], Tool]
PresentationMapper = Callable[[Any], TicketPresentationV1]

ALLOWED_PRESENTATION_KINDS = frozenset(TicketPresentationV1.model_fields["kind"].annotation.__args__)


@dataclass(frozen=True)
class TicketPluginV1:
    contract_version: int
    tool_name: str
    function_tool_factory: ToolFactory
    presentation_mappers: Mapping[str, PresentationMapper] = field(default_factory=dict)


class RegistryV1:
    def __init__(self) -> None:
        self._tools: dict[str, ToolFactory] = {}
        self._mappers: dict[str, PresentationMapper] = {}

    def register_plugin(self, plugin: TicketPluginV1) -> None:
        if plugin.contract_version != 1:
            raise ValueError("unsupported plugin contract version")
        if not plugin.tool_name or plugin.tool_name in self._tools:
            raise ValueError("duplicate or empty tool name")
        for kind in plugin.presentation_mappers:
            if kind not in ALLOWED_PRESENTATION_KINDS:
                raise ValueError("unknown presentation kind")
            if kind in self._mappers:
                raise ValueError("duplicate presentation mapper")
        self._tools[plugin.tool_name] = plugin.function_tool_factory
        self._mappers.update(plugin.presentation_mappers)

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def build_tools(self) -> tuple[Tool, ...]:
        tools = tuple(factory() for factory in self._tools.values())
        if tuple(tool.name for tool in tools) != self.tool_names:
            raise ValueError("tool factory name does not match registration")
        return tools

    @property
    def presentation_mappers(self) -> Mapping[str, PresentationMapper]:
        return MappingProxyType(self._mappers)

    def mapper_for(self, kind: str) -> PresentationMapper:
        try:
            return self._mappers[kind]
        except KeyError as exc:
            raise ValueError("unsupported presentation kind") from exc
