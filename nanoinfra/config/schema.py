"""Configuration schema using Pydantic."""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

from loguru import logger
from pydantic import AliasChoices, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from nanoinfra.config.connectors import ConnectorRuntimeConfig
from nanoinfra.config.gates import GatesConfig
from nanoinfra.config_base import Base
from nanoinfra.cron.types import CronSchedule

if TYPE_CHECKING:
    from nanoinfra.agent.tools.cli_apps import CliAppsToolConfig
    from nanoinfra.agent.tools.filesystem import FileToolsConfig
    from nanoinfra.agent.tools.image_generation import ImageGenerationToolConfig
    from nanoinfra.agent.tools.self import MyToolConfig
    from nanoinfra.agent.tools.shell import ExecToolConfig
    from nanoinfra.agent.tools.web import WebToolsConfig


class ChannelsConfig(Base):
    """Configuration for chat channels.

    Built-in and plugin channel configs are stored as extra fields (dicts).
    Each channel parses its own config in __init__.
    Per-channel "streaming": true enables streaming output (requires send_delta impl).
    """

    model_config = ConfigDict(extra="allow")

    send_progress: bool = True  # stream agent's text progress to the channel
    send_tool_hints: bool = True  # stream tool-call hints (e.g. read_file("…"))
    show_reasoning: bool = True  # surface model reasoning when channel implements it
    extract_document_text: bool = True  # Deprecated and ignored; documents are read on demand
    send_max_retries: int = Field(default=3, ge=0, le=10)  # Max delivery attempts (initial send included)
    transcription_provider: str = "groq"  # Deprecated: use top-level transcription.provider
    transcription_language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}$")  # Deprecated: use top-level transcription.language


class TranscriptionConfig(Base):
    """Cross-channel audio transcription configuration."""

    enabled: bool = True
    provider: str | None = None  # Validated by nanoinfra.audio.transcription_registry.
    model: str | None = None
    language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}$")
    max_duration_sec: int = Field(default=120, ge=1, le=600)
    max_upload_mb: int = Field(default=25, ge=1, le=100)


class DreamConfig(Base):
    """Dream memory consolidation configuration."""

    _HOUR_MS = 3_600_000

    enabled: bool = True  # Register the periodic Dream consolidation job on startup
    interval_h: int = Field(default=2, ge=1)  # Every 2 hours by default
    cron: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )  # Legacy cron expression override
    model_override: str | None = Field(
        default=None,
        validation_alias=AliasChoices("modelOverride", "model", "model_override"),
    )  # Model preset name for Dream sessions

    def build_schedule(self, timezone: str) -> CronSchedule:
        """Build the runtime schedule, preferring the legacy cron override if present."""
        if self.cron:
            return CronSchedule(kind="cron", expr=self.cron, tz=timezone)
        return CronSchedule(kind="every", every_ms=self.interval_h * self._HOUR_MS)

    def describe_schedule(self) -> str:
        """Return a human-readable summary for logs and startup output."""
        if self.cron:
            return f"cron {self.cron} (legacy)"
        hours = self.interval_h
        return f"every {hours}h"


class InlineFallbackConfig(Base):
    """One inline fallback model configuration."""

    model: str
    provider: str
    max_tokens: int | None = None
    context_window_tokens: int | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None


FallbackCandidate = str | InlineFallbackConfig


class ModelPresetConfig(Base):
    """A named set of model + generation parameters for quick switching."""

    label: str | None = None
    model: str
    provider: str = "auto"
    max_tokens: int = 8192
    context_window_tokens: int = 200_000
    temperature: float = 0.1
    reasoning_effort: str | None = None

    def to_generation_settings(self) -> Any:
        from nanoinfra.providers.base import GenerationSettings
        return GenerationSettings(
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            reasoning_effort=self.reasoning_effort,
        )


class AgentDefaults(Base):
    """Default agent configuration.

    This is the deployment's own agent: the one that answers when no named agent is chosen, which
    is every turn in a deployment that names none. The agent-shaped fields below are the ones a
    named agent has and this did not, so the default agent could be *read* on the Agents page and
    not edited (#265, completed in #266). Everything else here is a deployment setting rather than
    an agent's, and lives in its own Settings panel.

    **It is one more agent; the only thing that makes it different is that it cannot be deleted.**
    That is not tidiness. It is the agent that answers when nobody chooses, so it is the agent
    that answers most -- and while it was the one agent that could not be narrowed, a deployment
    with a dozen MCP servers and thirty skills installed paid for all of them on every turn it
    took, with no control anywhere that said otherwise.

    Deliberately absent: ``description``, because it needs no line explaining it to a peer --
    nothing delegates *to* it.
    """

    #: Appended after the platform's own prompt sections, exactly as a named agent's is. It
    #: specialises the deployment's agent and cannot replace the tool contract or the safety notes.
    addendum: str = ""
    #: Prompt sections this deployment replaces for the default agent, by section name. Which
    #: sections may be replaced is `agent/prompt_sections.py`'s answer, not this schema's.
    prompt_sections: dict[str, str] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("promptSections", "prompt_sections"),
        serialization_alias="promptSections",
    )
    #: The `tools.groups` the default agent may use. **Absent means every group; an empty list
    #: means none** -- which is how a deployment turns its own agent into a coordinator that has
    #: to ask a peer for anything grouped. See `NamedAgentConfig.tool_groups`.
    tool_groups: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices("toolGroups", "tool_groups"),
        serialization_alias="toolGroups",
    )
    #: Skills loaded in full for the default agent. Absent means the catalogue is summarised as
    #: today; an empty list means none. Distinct from ``disabled_skills`` beside it, which removes
    #: a skill from the whole deployment: this chooses which of the rest are loaded whole here.
    skills: list[str] | None = None
    #: Connectors and MCP servers the default agent may reach, with the same three states its
    #: `tool_groups` has: absent is *no ceiling*, an empty list is *declared, and empty*.
    #:
    #: The second is why they are nullable (#266). Twelve installed MCP servers is twelve schema
    #: sets in the first message of every conversation, and while empty meant *all of them* there
    #: was no way to write down "this agent loads none" -- so the largest recurring cost in a
    #: deployment had no control at all.
    connectors: list[str] | None = None
    mcp_servers: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices("mcpServers", "mcp_servers"),
        serialization_alias="mcpServers",
    )
    #: The peers the default agent may delegate to. Membership is the authorization, exactly as it
    #: is for a named agent.
    #:
    #: This began as "the default agent can never delegate", and the consequence was a trap: a
    #: composer whose agent choice did not survive a reload fell back to the default agent, which
    #: held no roster, so delegation became impossible without the operator being told why. The
    #: deployment's own agent is one more agent, and whether it delegates is config's answer.
    delegates: list[str] = Field(default_factory=list)

    #: A fresh install gets ``default`` inside ``tools.workspacesRoot``. An install
    #: that predates the root keeps what its own config.json says; see
    #: ``config.paths.default_workspace_path`` for why that is not migrated for them.
    workspace: str = "~/.nanoinfra/workspaces/default"
    model_preset: str | None = None  # Active preset name — takes precedence over fields below
    #: Empty, and deliberately: **nothing ships a model**.
    #:
    #: This used to be `anthropic/claude-opus-4-5`, and the cost was a deployment appearing to run
    #: on a model it had no credential for. `config.json` said so in writing, the Models panel
    #: showed it as a row called `Default`, and the conclusion a reader drew from that -- that the
    #: deployment had silently fallen back to Opus -- was wrong. A packaged model is a decision
    #: nobody made, presented as one they did.
    #:
    #: What is primary instead is the **first preset this deployment adds**, and it stays primary
    #: until something else is chosen: `resolve_default_preset` falls to the first entry of
    #: `model_presets`, and creating a preset while none is active makes that one active. So the
    #: answer to "which model answers" is always a preset somebody wrote down.
    #:
    #: Still a real field, not removed: an operator who sets a model inline -- `nanoinfra` with a
    #: `--model`, or the provider CLI -- has chosen one, and that choice is the implicit `default`
    #: preset exactly as it always was.
    model: str = ""
    provider: str = (
        "auto"  # Provider name (e.g. "anthropic", "openrouter") or "auto" for auto-detection
    )
    max_tokens: int = 8192
    context_window_tokens: int = 200_000
    context_block_limit: int | None = None
    temperature: float = 0.1
    fallback_models: list[FallbackCandidate] = Field(default_factory=list)
    max_tool_iterations: int = 200
    #: Upper bound because each subagent is a full provider conversation: without one, a typo in
    #: this field is a fork bomb against the provider account. Enforced in the schema rather than in
    #: the UI, so the config file cannot express what the UI refuses.
    max_concurrent_subagents: int = Field(default=1, ge=1, le=8)
    fail_on_tool_error: bool = True
    max_tool_result_chars: int = 16_000
    provider_retry_mode: Literal["standard", "persistent"] = "standard"
    tool_hint_max_length: int = Field(
        default=40,
        ge=20,
        le=500,
        validation_alias=AliasChoices("toolHintMaxLength"),
        serialization_alias="toolHintMaxLength",
    )  # Max characters for tool hint display (e.g. "$ cd …/project && npm test")
    reasoning_effort: str | None = None  # low / medium / high / xhigh / max / adaptive / none — LLM thinking effort; None preserves the provider default
    timezone: str = "UTC"  # IANA timezone, e.g. "Asia/Shanghai", "America/New_York"
    bot_name: str = "nanoinfra"  # Display name shown in CLI prompts (e.g. "{name} is thinking...")
    bot_icon: str = "🐈"  # Short icon (emoji or text) shown next to the bot name in CLI; "" to omit
    unified_session: bool = False  # Share one session across all channels (single-user multi-device)
    disabled_skills: list[str] = Field(default_factory=list)  # Skill names to exclude from loading (e.g. ["summarize", "skill-creator"])
    session_ttl_minutes: int = Field(
        default=15,
        ge=0,
        validation_alias=AliasChoices("idleCompactAfterMinutes", "sessionTtlMinutes"),
        serialization_alias="idleCompactAfterMinutes",
    )  # Auto-compact idle threshold in minutes (0 = disabled)
    #: What happens to a message that arrives while a turn is already running (#209).
    #:
    #: `queue` is the request→response shape of the API: the message becomes its own turn and waits
    #: behind the one in flight, keeping its own `turn_id` and its own answer. `inject` folds it
    #: into the running turn, which is what every deployment did before this field -- a correction
    #: reaches the work in progress, and in exchange it has no response of its own and the record
    #: says whoever started the turn asked for it.
    mid_turn_messages: Literal["queue", "inject"] = Field(
        default="queue",
        validation_alias=AliasChoices("midTurnMessages", "mid_turn_messages"),
        serialization_alias="midTurnMessages",
    )
    idle_compact_check_interval_seconds: int = Field(
        default=60,
        ge=0,
    )  # Minimum interval in seconds between scans for idle sessions
    consolidation_ratio: float = Field(
        default=0.5,
        ge=0.1,
        le=0.95,
        validation_alias=AliasChoices("consolidationRatio"),
        serialization_alias="consolidationRatio",
    )  # Consolidation target ratio (0.5 = 50% of budget retained after compression)
    dream: DreamConfig = Field(default_factory=DreamConfig)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError(f"unknown timezone {value!r}") from None
        return value


class NamedAgentConfig(Base):
    """One named agent: its own model, its own tools, its own bindings (#247).

    An agent's tool set is **declared, not accumulated**. Every field left unset inherits from
    ``agents.defaults``, which is what keeps a two-line agent meaningful -- a name, a preset, and
    the tools it is allowed to see.

    There is deliberately no equivalent of a "custom" type whose capability list means *no
    restrictions*. A type like that is a hole kept open for compatibility nobody here needs.
    """

    description: str = ""
    #: Which model this agent answers with. Names a `modelPresets` entry; unset inherits the
    #: deployment default.
    model_preset: str | None = Field(
        default=None,
        validation_alias=AliasChoices("modelPreset", "model_preset"),
        serialization_alias="modelPreset",
    )
    #: The `tools.groups` this agent may use.
    #:
    #: **Absent means every group; an empty list means none.** Two different answers, and they
    #: were one until a coordinator turned out to be unconfigurable: while `[]` meant *all*, a
    #: deployment could not take `servers` away from an agent, so that agent always ran a host
    #: command itself and never had a reason to ask a peer. Absent is what every deployment that
    #: has narrowed nothing carries, so nothing changes for them.
    tool_groups: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices("toolGroups", "tool_groups"),
        serialization_alias="toolGroups",
    )
    #: Skills loaded in full for this agent. Absent means the catalogue is summarised as today;
    #: an empty list means none are loaded in full. Same distinction as `tool_groups`.
    skills: list[str] | None = None
    #: Connectors and MCP servers this agent may reach, with the same three states `tool_groups`
    #: has: absent is *no ceiling*, an empty list is *declared, and empty*. See `AgentDefaults`
    #: for why the empty case had to become expressible.
    connectors: list[str] | None = None
    mcp_servers: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices("mcpServers", "mcp_servers"),
        serialization_alias="mcpServers",
    )
    #: Appended after the platform's own prompt sections; it specialises an agent and cannot
    #: replace the tool contract or the safety notes. See the Prompt tab design (#256).
    addendum: str = ""
    #: Prompt sections this agent replaces outright, by section name (#256). Which sections *may*
    #: be replaced is decided in `agent/prompt_sections.py`, not here: naming a fixed one -- the
    #: tool contract, the safety notes -- is refused when the prompt is assembled rather than
    #: quietly ignored. A replaced section is still named in the manifest and marked as
    #: overridden, because a record that hid a replacement would make two different prompts look
    #: identical.
    prompt_sections: dict[str, str] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("promptSections", "prompt_sections"),
        serialization_alias="promptSections",
    )
    #: The peers this agent may delegate to. **Membership is the authorization** -- it lives in
    #: config because that is where authority lives, and the executor revalidates it rather than
    #: trusting a tool call (#249).
    delegates: list[str] = Field(default_factory=list)


#: A named agent is addressed as ``@agent:<name>``; the composer's own token pattern is the same
#: set minus the colon it splits on (``webui/src/components/thread/ThreadComposer.tsx``).
_MENTIONABLE_AGENT_NAME = re.compile(r"^[\w.-]+$", re.UNICODE)


class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)
    # Named agents (#247). Empty is the shape every deployment has today: one agent, described by
    # `defaults`. A named agent inherits `defaults` and narrows it.
    named: dict[str, NamedAgentConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _names_must_be_mentionable(self) -> "AgentsConfig":
        """An agent is addressed as ``@agent:<name>`` in a message, so the name has to survive
        being a token: no space, and no colon for the parser to split on.

        Checked here rather than filtered in the picker, because an agent that config accepts and
        the composer silently hides is a worse outcome than a config that says why.
        """
        for name in self.named:
            if not name or not _MENTIONABLE_AGENT_NAME.match(name):
                raise ValueError(
                    f"agents.named[{name!r}] is not a usable agent name: use letters, digits, "
                    "'-', '_' or '.', so the agent can be addressed as @agent:<name>"
                )
        return self

    @model_validator(mode="after")
    def _delegates_must_exist(self) -> "AgentsConfig":
        """A roster naming an agent that does not exist would fail at delegation time.

        Refused at load instead, because the roster is authority: an operator who mistypes a peer
        should be told by the config that refuses to load, not by an agent that cannot find who it
        was told to ask.
        """
        for name, agent in self.named.items():
            for peer in agent.delegates:
                if peer not in self.named:
                    raise ValueError(
                        f"agents.named[{name!r}].delegates names {peer!r}, "
                        "which is not a configured agent"
                    )
                if peer == name:
                    raise ValueError(
                        f"agents.named[{name!r}] lists itself as a delegate"
                    )
        # The deployment's own agent is subject to the same rule, because it is one more agent
        # (#266) and a mistyped peer there fails exactly the same way: a manager that cannot find
        # who it was told to ask. It cannot list *itself*, and there is nothing to spell that
        # with -- `agents.defaults` has no name in the roster.
        for peer in self.defaults.delegates:
            if peer not in self.named:
                raise ValueError(
                    f"agents.defaults.delegates names {peer!r}, which is not a configured agent"
                )
        return self


class ProviderConfig(Base):
    """LLM provider configuration."""

    # User-facing name for dynamic custom providers.
    display_name: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    api_key: str | None = Field(default=None, repr=False)
    api_base: str | None = None
    api_type: Literal["auto", "chat_completions", "responses"] = "auto"  # Request API surface
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)
    extra_body: dict[str, Any] | None = None  # Extra provider request fields; shape depends on provider/API surface
    extra_query: dict[str, str] | None = None  # Extra query params (e.g. api-version for Azure-style gateways)
    proxy: str | None = None  # Explicit HTTP proxy; image downloads trust its DNS and egress
    thinking_style: str | None = None  # Thinking/reasoning style for custom providers

    # Valid values mirror the keys of _THINKING_STYLE_MAP in
    # nanoinfra/providers/openai_compat_provider.py. Kept duplicated here to
    # avoid an import cycle (schema.py must not import from providers/).
    _VALID_THINKING_STYLES: ClassVar[tuple[str, ...]] = (
        "thinking_type",
        "enable_thinking",
        "reasoning_split",
    )

    @field_validator("thinking_style")
    @classmethod
    def _validate_thinking_style(cls, v: str | None) -> str | None:
        if not v:  # None or "" -> no injection, valid (backwards compatible)
            return v
        if v not in cls._VALID_THINKING_STYLES:
            raise ValueError(
                f"Invalid thinking_style {v!r}. "
                f"Must be one of: {', '.join(repr(s) for s in cls._VALID_THINKING_STYLES)} "
                f"(or empty/omitted)."
            )
        return v


class BedrockProviderConfig(ProviderConfig):
    """AWS Bedrock Runtime provider configuration."""

    region: str | None = None  # AWS region, falls back to AWS_REGION/AWS_DEFAULT_REGION/profile
    profile: str | None = None  # Optional AWS shared config profile


class ProvidersConfig(Base):
    """Configuration for LLM providers.

    Supports custom providers via extra fields — any additional field
    becomes an OpenAI-compatible custom provider.
    """

    model_config = ConfigDict(extra="allow")

    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # Any OpenAI-compatible endpoint
    azure_openai: ProviderConfig = Field(default_factory=ProviderConfig)  # Azure OpenAI (model = deployment name)
    bedrock: BedrockProviderConfig = Field(default_factory=BedrockProviderConfig)  # AWS Bedrock Converse
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    assemblyai: ProviderConfig = Field(default_factory=ProviderConfig)  # AssemblyAI voice transcription
    huggingface: ProviderConfig = Field(default_factory=ProviderConfig)
    skywork: ProviderConfig = Field(default_factory=ProviderConfig)  # Skywork / APIFree API gateway
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)
    modelscope: ProviderConfig = Field(default_factory=ProviderConfig)
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    ollama: ProviderConfig = Field(default_factory=ProviderConfig)  # Ollama local models
    lm_studio: ProviderConfig = Field(default_factory=ProviderConfig)  # LM Studio local models
    atomic_chat: ProviderConfig = Field(default_factory=ProviderConfig)  # Atomic Chat local models
    ovms: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenVINO Model Server (OVMS)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    kimi_coding: ProviderConfig = Field(default_factory=ProviderConfig)  # Kimi Coding Plan (Anthropic Messages API)
    minimax: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax_anthropic: ProviderConfig = Field(default_factory=ProviderConfig)  # MiniMax Anthropic endpoint (thinking)
    mistral: ProviderConfig = Field(default_factory=ProviderConfig)
    stepfun: ProviderConfig = Field(default_factory=ProviderConfig)  # Step Fun (阶跃星辰) — LLM + ASR (set apiBase to Plan URL for ASR)
    xiaomi_mimo: ProviderConfig = Field(default_factory=ProviderConfig)  # Xiaomi MIMO (小米)
    longcat: ProviderConfig = Field(default_factory=ProviderConfig)  # LongCat
    ant_ling: ProviderConfig = Field(default_factory=ProviderConfig)  # Ant Ling
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API gateway
    siliconflow: ProviderConfig = Field(default_factory=ProviderConfig)  # SiliconFlow (硅基流动)
    edenai: ProviderConfig = Field(default_factory=ProviderConfig)  # Eden AI API gateway
    novita: ProviderConfig = Field(default_factory=ProviderConfig)  # Novita AI
    volcengine: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine (火山引擎)
    volcengine_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine Coding Plan
    byteplus: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus (VolcEngine international)
    byteplus_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus Coding Plan
    openai_codex: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # OpenAI Codex (OAuth)
    xai_grok: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # xAI Grok (OAuth)
    github_copilot: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # Github Copilot (OAuth)
    qianfan: ProviderConfig = Field(default_factory=ProviderConfig)  # Qianfan (百度千帆)
    nvidia: ProviderConfig = Field(default_factory=ProviderConfig)  # NVIDIA NIM (nvapi- keys)
    opencode: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenCode Zen (canonical provider id)
    opencode_zen: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenCode Zen (curated coding models)
    opencode_go: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenCode Go (low-cost coding models)

    @model_validator(mode="after")
    def convert_extra_providers(self):
        """Convert extra fields (custom providers) to ProviderConfig objects."""
        if self.model_extra:
            from nanoinfra.providers.registry import find_by_name

            for key, value in self.model_extra.items():
                if spec := find_by_name(key):
                    raise ValueError(
                        f"providers.{key} conflicts with built-in provider {spec.name!r}; "
                        "use the built-in provider key or choose a different custom provider name"
                    )
                if isinstance(value, dict):
                    self.model_extra[key] = ProviderConfig.model_validate(value)
        return self

    @model_validator(mode="after")
    def _validate_api_type_scope(self) -> "ProvidersConfig":
        for name in self.__class__.model_fields:
            if name == "openai":
                continue
            provider = getattr(self, name, None)
            if isinstance(provider, ProviderConfig) and provider.api_type != "auto":
                raise ValueError("providers.<name>.api_type is only supported for providers.openai")
        for provider in (self.model_extra or {}).values():
            if isinstance(provider, ProviderConfig) and provider.api_type != "auto":
                raise ValueError("providers.<name>.api_type is only supported for providers.openai")
        return self


class HeartbeatConfig(Base):
    """Heartbeat service configuration (now backed by cron)."""

    enabled: bool = True
    interval_s: int = 30 * 60  # 30 minutes
    keep_recent_messages: int = 8


class SkillsMarketplaceConfig(Base):
    """WebUI Agent Skills marketplace configuration."""

    # Base URL of the self-hosted nanoinfra skills-server catalog (submission
    # pipeline + security scan shield + versioning), used alongside skills.sh
    # as a marketplace provider. Override to point at a different deployment
    # (e.g. a self-hosted instance).
    nanoinfra_base_url: str = "https://skills.nanoinfra.org"


class ApiConfig(Base):
    """OpenAI-compatible API server configuration."""

    # Whether the *gateway* also serves `/v1` on `port` (#214). Default false, so no deployment
    # gains an open port by upgrading; `nanoinfra serve` is unaffected and stays the entry point
    # for an API-only deployment.
    #
    # Worth the flag rather than always-on: serving it from the gateway is one process and one
    # agent loop instead of two, which is the point -- every piece of runtime the gateway
    # assembles is a piece a second process has to remember to assemble too, and the missing one
    # last time was the outbound drain.
    enabled: bool = False
    host: str = "127.0.0.1"  # Safer default: local-only bind.
    port: int = 8900
    timeout: float = 120.0  # Per-request timeout in seconds.
    api_key: str = Field(default="", repr=False)

    @model_validator(mode="after")
    def wildcard_host_requires_auth(self) -> "ApiConfig":
        if self.host not in ("0.0.0.0", "::"):
            return self
        if self.api_key.strip():
            return self
        raise ValueError(
            "host is 0.0.0.0 (all interfaces) but api_key is not set "
            "- set api.api_key to prevent unauthenticated access"
        )


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = "127.0.0.1"  # Safer default: local-only bind.
    port: int = 18790
    restart_mode: Literal["auto", "exec", "spawn", "exit"] = "auto"
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)


class MCPServerConfig(Base):
    """MCP server connection configuration (stdio or HTTP)."""

    # The middle state the Apps page was missing (#206). A configured server was in every prompt,
    # always, and the only way out was the trash icon -- which loses the command, the env, the
    # headers and the `enabledTools` list with it. `false` keeps all of that and simply does not
    # connect the server, so its schemas are in no prompt.
    #
    # Worth a deployment-wide flag on its own: three servers exposing the same fifteen tools is
    # ~15K tokens per turn to use one of them.
    enabled: bool = True
    # When the schemas are sent (#204). `always` is what every deployment did before this field
    # existed and stays the default: all of this server's tool schemas in every prompt.
    #
    # `mention` sends one *line* instead -- name, tool count, and how to attach -- and the schemas
    # only when the turn names the server (`@server` in the composer, or `mcpPresets` on an
    # automation). The line is the reason this is not simply "send nothing": a model that cannot see
    # that a capability exists cannot say "I can do that if you attach it". It either fails or
    # quietly substitutes something worse, and a silently worse answer is harder to notice than a
    # large bill.
    #
    # Distinct from `enabled: false`, which does not connect the server at all: a `mention` server
    # is connected and one word away.
    attach: Literal["always", "mention"] = "always"
    type: Literal["stdio", "sse", "streamableHttp"] | None = None  # auto-detected if omitted
    command: str = ""  # Stdio: command to run (e.g. "npx")
    args: list[str] = Field(default_factory=list)  # Stdio: command arguments
    env: dict[str, str] = Field(default_factory=dict)  # Stdio: extra env vars
    cwd: str = ""  # Stdio: working directory for MCP server runtime artifacts
    url: str = ""  # HTTP/SSE: endpoint URL
    headers: dict[str, str] = Field(default_factory=dict)  # HTTP/SSE: custom headers
    tool_timeout: int = 30  # seconds before a tool call is cancelled
    enabled_tools: list[str] = Field(default_factory=lambda: ["*"])  # Only register these tools; accepts raw MCP names or wrapped mcp_<server>_<tool> names; ["*"] = all capabilities (tools, resources, prompts); any restriction = only listed tools, no resources/prompts


class KnowledgeConfig(Base):
    """The knowledge base in the workspace, and how it is searched (#237).

    Documents live in ``workspaces/<ws>/knowledge/`` -- folders and subfolders, whatever the
    operator drops there -- and the index sits beside them. Nothing is injected into a prompt: the
    agent reaches it through ``knowledge_search``, because a knowledge base in the stable prompt
    block is the *31K for a hola* problem (#203) at a larger scale, and it grows with the operator's
    own writing.
    """

    enabled: bool = False
    # `lexical` is BM25F over the fielded index and carries no dependencies of its own. `hybrid`
    # adds vector search and needs the optional extra; it is not the default because for runbooks
    # the words in the question are usually the words in the document. The limit is worth knowing
    # rather than discovering: lexical will not match "pod won't start" against a document that
    # says "CrashLoopBackOff".
    mode: Literal["lexical", "hybrid"] = "lexical"
    #: How often the indexing system job runs. Quiet by default: the search tool checks freshness
    #: itself, so this pass exists to collect deletions and to catch what nobody searched for.
    reindex_interval_s: int = Field(
        default=900,
        ge=60,
        validation_alias=AliasChoices("reindexIntervalS", "reindex_interval_s"),
        serialization_alias="reindexIntervalS",
    )
    #: Never indexed. Config rather than code, because an operator's tree has secrets ours cannot
    #: guess -- and removable only deliberately, which is why the defaults are written out.
    exclude: list[str] = Field(
        default_factory=lambda: [
            ".env",
            ".env.*",
            "*.pem",
            "*.key",
            "id_*",
            "secrets/**",
            "**/.git/**",
        ]
    )
    #: One 2 GB log file must not become the knowledge base.
    max_file_bytes: int = Field(
        default=2_000_000,
        ge=1_024,
        validation_alias=AliasChoices("maxFileBytes", "max_file_bytes"),
        serialization_alias="maxFileBytes",
    )
    max_total_bytes: int = Field(
        default=200_000_000,
        ge=1_024,
        validation_alias=AliasChoices("maxTotalBytes", "max_total_bytes"),
        serialization_alias="maxTotalBytes",
    )
    #: How many fragments one search returns. Small on purpose: a citation the model has to read is
    #: worth more than ten it skims.
    max_results: int = Field(
        default=5,
        ge=1,
        le=25,
        validation_alias=AliasChoices("maxResults", "max_results"),
        serialization_alias="maxResults",
    )


class ToolGroupConfig(Base):
    """A named group of built-in tools, and when their schemas reach the prompt (#210).

    `attach: "mention"` is the same trade an MCP server and a connector already get, for the
    clusters that were measured as the expensive ones: the diagram tools cost 2,438 tokens and the
    SSH server tools 1,419, on every turn, whether or not the turn is about either.

    `tools` may be omitted for a group nanoinfra defines (`diagrams`, `servers`), which is what
    lets a deployment write `{"diagrams": {"attach": "mention"}}` and mean the five diagram tools.
    Naming tools explicitly is taken at its word, including a name from a plugin.
    """

    #: The built-in tool names in this group. Empty means "the group nanoinfra defines".
    tools: list[str] = Field(default_factory=list)
    # `always` is what every deployment did before this field existed and stays the default: all of
    # these schemas in every prompt. `mention` sends one *line* instead -- the group, its tool
    # count and how to attach -- and the schemas only for a turn that names it. The line is the
    # reason this is not simply "send nothing": a model that cannot see a capability exists cannot
    # say "I can do that if you attach it".
    attach: Literal["always", "mention"] = "always"
    #: What the group is for, in the advertised line. Defaults to nanoinfra's own wording.
    description: str = ""


def _lazy_default(module_path: str, class_name: str) -> Any:
    """Deferred import helper for ToolsConfig default factories."""
    import importlib
    module = importlib.import_module(module_path)
    return getattr(module, class_name)()


class ToolsConfig(Base):
    """Tools configuration.

    Field types for tool-specific sub-configs are resolved via model_rebuild()
    at the bottom of this file so tool config classes can stay next to their
    tool implementations.

    Both construction paths ask for that resolution first (#57). A caller builds
    a ToolsConfig on its own, so the guard cannot live on ``Config`` alone.
    """

    def __init__(self, **values: Any) -> None:
        ensure_tool_config_refs()
        super().__init__(**values)

    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any) -> "ToolsConfig":
        ensure_tool_config_refs()
        return cast("ToolsConfig", super().model_validate(obj, **kwargs))

    web: WebToolsConfig = Field(default_factory=lambda: _lazy_default("nanoinfra.agent.tools.web", "WebToolsConfig"))
    exec: ExecToolConfig = Field(default_factory=lambda: _lazy_default("nanoinfra.agent.tools.shell", "ExecToolConfig"))
    file: FileToolsConfig = Field(default_factory=lambda: _lazy_default("nanoinfra.agent.tools.filesystem", "FileToolsConfig"))
    cli_apps: CliAppsToolConfig = Field(default_factory=lambda: _lazy_default("nanoinfra.agent.tools.cli_apps", "CliAppsToolConfig"))
    my: MyToolConfig = Field(default_factory=lambda: _lazy_default("nanoinfra.agent.tools.self", "MyToolConfig"))
    image_generation: ImageGenerationToolConfig = Field(
        default_factory=lambda: _lazy_default("nanoinfra.agent.tools.image_generation", "ImageGenerationToolConfig"),
    )
    restrict_to_workspace: bool = True  # keep tool access inside the workspace; see loader._migrate_config for the upgrade pin (#135)
    #: Where workspaces live, and the boundary a *client* may name one inside.
    #:
    #: Declared rather than derived: the parent of ``agents.defaults.workspace`` is
    #: ``~/.nanoinfra``, which holds ``config.json`` with the provider keys and the
    #: secrets store, so deriving it would hand the WebUI a reader for both.
    #:
    #: The configured workspace is allowed even when it sits outside this root,
    #: because config is git-reviewed and widens deliberately while a client-supplied
    #: path does not -- the same split ``webui/file_preview.py`` records.
    workspaces_root: str = "~/.nanoinfra/workspaces"
    webui_allow_local_service_access: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "webuiAllowLocalServiceAccess",
            "webui_allow_local_service_access",
            "allowLocalPreviewAccess",
            "allow_local_preview_access",
        ),
    )  # allow WebUI Full Access shell checks against localhost services; legacy allowLocalPreviewAccess still reads
    webui_allow_remote_package_install: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "webuiAllowRemotePackageInstall",
            "webui_allow_remote_package_install",
        ),
    )  # allow non-local WebUI clients to install optional packages and agent skills
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    # Declared groups of built-in tools (#210). Keyed by group name; `diagrams` and `servers` are
    # defined by nanoinfra, so a deployment only writes the mode it wants.
    groups: dict[str, ToolGroupConfig] = Field(default_factory=dict)
    # The knowledge base and its search (#237). Off by default: an empty index answers nothing, and
    # a deployment that never drops a document should not pay for a walk of its workspace.
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    # Agent Plugin identities the operator has activated (#141). This list is the authority: the
    # executor reconciles activation markers against it, and enabling a package that ships an
    # mcp.json grants a new stdio process, so the decision belongs in a git-reviewed file rather
    # than in a directory the agent can write.
    agent_plugins: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("agentPlugins", "agent_plugins"),
        serialization_alias="agentPlugins",
    )
    ssrf_whitelist: list[str] = Field(default_factory=list)  # CIDR ranges to exempt from SSRF blocking (e.g. ["100.64.0.0/10"] for Tailscale)


class Config(BaseSettings):
    """Root configuration for nanoinfra."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    skills_marketplace: SkillsMarketplaceConfig = Field(
        default_factory=SkillsMarketplaceConfig,
        validation_alias=AliasChoices("skillsMarketplace", "skills_marketplace"),
        serialization_alias="skillsMarketplace",
    )
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    gates: GatesConfig = Field(default_factory=GatesConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    # Data connectors and the credentials they name. Beside `gates` rather than under `tools`
    # because both halves are authority: a credential says what the deployment holds, and a
    # connector's `credential` key is the only thing that lets a package resolve it.
    connectors: ConnectorRuntimeConfig = Field(default_factory=ConnectorRuntimeConfig)
    model_presets: dict[str, ModelPresetConfig] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("modelPresets", "model_presets"),
        serialization_alias="modelPresets",
    )

    def __init__(self, **values: Any) -> None:
        ensure_tool_config_refs()
        super().__init__(**values)

    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any) -> "Config":
        """Resolve the references first, the way ``__init__`` does (#57).

        ``model_validate`` never calls ``__init__``, so the retry that guarded one construction
        path left the other open. `Config.model_validate` is the path the provider tests take, and
        it raised ``PydanticUserError`` for every process whose first import lost the eager
        resolve.
        """
        ensure_tool_config_refs()
        return cast("Config", super().model_validate(obj, **kwargs))

    @model_validator(mode="after")
    def _validate_model_preset(self) -> "Config":
        if "default" in self.model_presets:
            raise ValueError("model_preset name 'default' is reserved for agents.defaults")
        name = self.agents.defaults.model_preset
        if name and name != "default" and name not in self.model_presets:
            raise ValueError(f"model_preset {name!r} not found in model_presets")
        dream_name = self.agents.defaults.dream.model_override
        if dream_name and dream_name != "default" and dream_name not in self.model_presets:
            raise ValueError(f"Dream model preset {dream_name!r} not found in model_presets")
        for fallback in self.agents.defaults.fallback_models:
            if isinstance(fallback, str) and fallback not in self.model_presets:
                raise ValueError(f"fallback_models entry {fallback!r} not found in model_presets")
        return self

    def resolve_default_preset(self) -> ModelPresetConfig:
        """Return the implicit `default` preset from agents.defaults fields.

        With no inline model -- which is every deployment that has not set one, since nothing
        ships a model any more -- this is **the first preset**. That is the rule stated plainly:
        the first model configuration a deployment adds is the primary one, and it answers until
        something else is chosen. Insertion order is the order they were added, because
        `model_presets` is a dict and config preserves the order the file was written in.

        The numbers still come from `agents.defaults` when a preset does not override them, so a
        deployment's token caps and temperature stay where its other settings are.
        """
        d = self.agents.defaults
        if not d.model.strip() and self.model_presets:
            return next(iter(self.model_presets.values()))
        return ModelPresetConfig(
            model=d.model, provider=d.provider, max_tokens=d.max_tokens,
            context_window_tokens=d.context_window_tokens,
            temperature=d.temperature, reasoning_effort=d.reasoning_effort,
        )

    def resolve_preset(self, name: str | None = None) -> ModelPresetConfig:
        """Return effective model params from a named preset or the implicit default."""
        name = self.agents.defaults.model_preset if name is None else name
        if not name or name == "default":
            return self.resolve_default_preset()
        if name not in self.model_presets:
            raise KeyError(f"model_preset {name!r} not found in model_presets")
        return self.model_presets[name]

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

    def _match_provider(
        self, model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
    ) -> tuple["ProviderConfig | None", str | None]:
        """Match provider config and its registry name. Returns (config, spec_name)."""
        from nanoinfra.providers.registry import (
            PROVIDERS,
            find_by_name,
        )

        resolved = preset or self.resolve_preset()
        forced = resolved.provider

        def _custom_provider_by_name(name: str) -> tuple[ProviderConfig, str] | None:
            normalized = name.replace("-", "_").lower()
            for attr_name, provider in (self.providers.model_extra or {}).items():
                if not isinstance(provider, ProviderConfig):
                    continue
                if attr_name.replace("-", "_").lower() == normalized:
                    return provider, attr_name
            return None

        if forced != "auto":
            spec = find_by_name(forced)
            if spec:
                p = getattr(self.providers, spec.name, None)
                return (p, spec.name) if p else (None, None)
            custom = _custom_provider_by_name(forced)
            if custom is not None:
                return custom
            return None, None

        model_lower = (model or resolved.model).lower()
        model_normalized = model_lower.replace("-", "_")
        model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
        normalized_prefix = model_prefix.replace("-", "_")
        prefixed_provider = find_by_name(model_prefix) if model_prefix else None

        def _kw_matches(kw: str) -> bool:
            kw = kw.lower()
            return kw in model_lower or kw.replace("-", "_") in model_normalized

        # Explicit provider prefix wins — prevents `github-copilot/...codex` matching openai_codex.
        for spec in PROVIDERS:
            if spec.is_transcription_only:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and model_prefix and normalized_prefix == spec.name:
                if spec.is_oauth or spec.is_local or spec.is_direct or p.api_key:
                    return p, spec.name

        # Check for custom provider by prefix (e.g., "companyProxy/gpt-4").
        # Return the matching provider even when apiBase is missing, so a
        # malformed explicit prefix fails instead of falling through to a
        # different custom provider.
        if model_prefix:
            custom = _custom_provider_by_name(normalized_prefix)
            if custom is not None:
                return custom

        # Match by keyword (order follows PROVIDERS registry)
        for spec in PROVIDERS:
            if spec.is_transcription_only:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and any(_kw_matches(kw) for kw in spec.keywords):
                # Local providers (Ollama, vLLM, …) keep model-family keywords
                # like "nemotron" or "llama" to enable bare-model auto-routing,
                # but those keywords collide with cloud-hosted variants of the
                # same family (e.g. `nvidia/nemotron-...` via OpenRouter). Only
                # honor a local keyword match when the user has actually
                # configured that local endpoint via `api_base` — mirrors the
                # gate already used by the local-fallback loop below.
                if spec.is_local:
                    # A qualified model belongs to its explicit provider or a
                    # gateway fallback, never to a different local provider
                    # whose model-family keyword happens to match.
                    foreign_prefix = bool(
                        prefixed_provider is not None and prefixed_provider.name != spec.name
                    )
                    if not p.api_base or foreign_prefix:
                        continue
                if spec.is_oauth or spec.is_local or spec.is_direct or p.api_key:
                    return p, spec.name

        # Fallback: configured local providers can route models without
        # provider-specific keywords (for example plain "llama3.2" on Ollama).
        # Prefer providers whose detect_by_base_keyword matches the configured api_base
        # (e.g. Ollama's "11434" in "http://localhost:11434") over plain registry order.
        local_fallback: tuple[ProviderConfig, str] | None = None
        if prefixed_provider is None:
            for spec in PROVIDERS:
                if not spec.is_local:
                    continue
                p = getattr(self.providers, spec.name, None)
                if not (p and p.api_base):
                    continue
                if spec.detect_by_base_keyword and spec.detect_by_base_keyword in p.api_base:
                    return p, spec.name
                if local_fallback is None:
                    local_fallback = (p, spec.name)
        if local_fallback:
            return local_fallback

        # Fallback: gateways first, then others (follows registry order)
        # OAuth providers are NOT valid fallbacks — they require explicit model selection
        for spec in PROVIDERS:
            if spec.is_oauth or spec.is_transcription_only:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and p.api_key:
                return p, spec.name

        # Final fallback: check for any configured custom provider
        for attr_name, p in (self.providers.model_extra or {}).items():
            if isinstance(p, ProviderConfig) and p.api_base:
                return p, attr_name

        return None, None

    def get_provider(
        self,
        model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
    ) -> ProviderConfig | None:
        """Get matched provider config (api_key, api_base, extra_headers). Falls back to first available."""
        p, _ = self._match_provider(model, preset=preset)
        return p

    def get_provider_name(
        self,
        model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
    ) -> str | None:
        """Get the registry name of the matched provider (e.g. "deepseek", "openrouter")."""
        _, name = self._match_provider(model, preset=preset)
        return name

    def get_api_key(
        self,
        model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
    ) -> str | None:
        """Get API key for the given model. Falls back to first available key."""
        p = self.get_provider(model, preset=preset)
        return p.api_key if p else None

    def get_api_base(
        self,
        model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
    ) -> str | None:
        """Get API base URL for the given model, falling back to the provider default when present."""
        from nanoinfra.providers.registry import find_by_name

        p, name = self._match_provider(model, preset=preset)
        if p and p.api_base:
            return p.api_base
        if name:
            spec = find_by_name(name)
            if spec and spec.default_api_base:
                return spec.default_api_base
        return None

    model_config = SettingsConfigDict(
        env_prefix="NANOINFRA_",
        env_nested_delimiter="__",
    )


def _resolve_tool_config_refs() -> None:
    """Resolve forward references in ToolsConfig by importing tool config classes.

    Must be called after all modules are loaded (breaks circular imports).
    Re-exports the classes into this module's namespace so existing imports
    like ``from nanoinfra.config.schema import ExecToolConfig`` continue to work.
    """
    import sys

    from nanoinfra.agent.tools.cli_apps import CliAppsToolConfig
    from nanoinfra.agent.tools.filesystem import FileToolsConfig
    from nanoinfra.agent.tools.image_generation import ImageGenerationToolConfig
    from nanoinfra.agent.tools.self import MyToolConfig
    from nanoinfra.agent.tools.shell import ExecToolConfig
    from nanoinfra.agent.tools.web import WebFetchConfig, WebSearchConfig, WebToolsConfig

    # Re-export into this module's namespace
    mod = sys.modules[__name__]
    mod.ExecToolConfig = ExecToolConfig  # type: ignore[attr-defined]
    mod.FileToolsConfig = FileToolsConfig  # type: ignore[attr-defined]
    mod.CliAppsToolConfig = CliAppsToolConfig  # type: ignore[attr-defined]
    mod.WebToolsConfig = WebToolsConfig  # type: ignore[attr-defined]
    mod.WebSearchConfig = WebSearchConfig  # type: ignore[attr-defined]
    mod.WebFetchConfig = WebFetchConfig  # type: ignore[attr-defined]
    mod.MyToolConfig = MyToolConfig  # type: ignore[attr-defined]
    mod.ImageGenerationToolConfig = ImageGenerationToolConfig  # type: ignore[attr-defined]

    ToolsConfig.model_rebuild()
    Config.model_rebuild()


def ensure_tool_config_refs() -> None:
    """Resolve the tool config references, unless they are resolved already (#57).

    This is the lazy half the eager attempt below needs. The eager attempt fails
    whenever the first import of a process reaches this module through a cycle,
    and for a long time nothing retried it, so a whole process held a ``Config``
    class that raised on every use of its ``tools`` field.

    The check reads one attribute after the first success, so a caller pays
    nothing. Without the check each construction would import eight modules and
    rebuild two models.

    Both models call this from both of their construction paths.
    ``model_validate`` does not call ``__init__``, and a guard on one path is a
    guard on neither.
    """
    if Config.__pydantic_complete__ and ToolsConfig.__pydantic_complete__:
        return
    _resolve_tool_config_refs()


# Eagerly resolve when the import chain allows it. The chain reaches
# ``nanoinfra.agent``, and that package imports the agent context, which imports
# the session manager, so any process whose first import is the session manager
# arrives here mid-cycle and this attempt fails.
#
# The failure is a timing artifact and not a real dependency problem: the same
# call succeeds later in the same process. ``ensure_tool_config_refs`` above is
# what makes it later.
#
# The cause reaches a log. A silent pass here cost a bisect over seven test files
# before #57, because the symptom appeared in an unrelated module.
try:
    _resolve_tool_config_refs()
except ImportError as exc:
    logger.debug(
        "Tool config references need a later resolve: {}. "
        "A construction of Config or ToolsConfig resolves them (#57).",
        exc,
    )
