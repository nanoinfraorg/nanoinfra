"""CLI commands for nanoinfra."""

# pyright: reportConstantRedefinition=false, reportMissingTypeStubs=false, reportPrivateUsage=false, reportUnusedFunction=false, reportUnusedImport=false

import asyncio
import contextlib
import json
import os
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

# Force UTF-8 encoding for Windows console
if sys.platform == "win32":
    if sys.stdout.encoding != "utf-8":
        os.environ["PYTHONIOENCODING"] = "utf-8"
        # Re-open stdout/stderr with UTF-8 encoding
        with suppress(Exception):
            for stream in (sys.stdout, sys.stderr):
                reconfigure = getattr(stream, "reconfigure", None)
                if callable(reconfigure):
                    reconfigure(encoding="utf-8", errors="replace")

# The `websockets` library (the gateway's HTTP+WS transport) reads this env
# var into a module-level constant the first time it's imported, capping
# every raw request/header line at 8KB by default. Every WebUI mutation
# route smuggles its JSON payload through one custom header rather than a
# real POST body (the library rejects any request with one), so a diagram
# with enough nodes/edges can exceed that cap. Set a generous ceiling before
# anything below has a chance to import `websockets` — this must run before
# that happens, not after, since the constant is read once at import time.
os.environ.setdefault("WEBSOCKETS_MAX_LINE_LENGTH", "131072")

# Keep console encoding setup before importing CLI UI/logging libraries.
import typer  # noqa: E402
from loguru import logger  # noqa: E402

# Remove default handler and re-add with unified nanoinfra format
logger.remove()
_log_handler_id = logger.add(
    sys.stderr,
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <5}</level> | "
        "<cyan>{extra[channel]}</cyan> | "
        "<level>{message}</level>"
    ),
    level="INFO",
    colorize=None,
    filter=lambda record: record["extra"].setdefault("channel", "-") or True,
    # diagnose off, because it prints the value of every local in a traceback. The executor
    # resolves a plaintext credential into a local, and a resolved command routinely embeds
    # one, so a diagnosed traceback writes both into a log that outlives the incident.
    # backtrace stays on: the file and the line of each frame cost nothing (#41).
    backtrace=True,
    diagnose=False,
)


from rich.console import Console  # noqa: E402
from rich.markup import escape  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.text import Text  # noqa: E402

from nanoinfra import __logo__, __version__  # noqa: E402
from nanoinfra import optional_features as feature_support  # noqa: E402
from nanoinfra.agent.hooks import create_file_edit_activity_hook  # noqa: E402
from nanoinfra.agent.loop import AgentLoop  # noqa: E402
from nanoinfra.cli import terminal as cli_terminal  # noqa: E402
from nanoinfra.cli.agent import agent  # noqa: E402
from nanoinfra.cli.gateway import create_gateway_app  # noqa: E402
from nanoinfra.cli.gateway_runtime import _run_gateway  # noqa: E402
from nanoinfra.cli.log_control import _set_nanoinfra_logs  # noqa: E402
from nanoinfra.cli.provider import provider_app  # noqa: E402
from nanoinfra.cli.runtime_config import (  # noqa: E402
    _load_inspection_config,
    _load_runtime_config,
    _model_display,
    _print_config_error,
    _print_model_setup_steps,
    _provider_setup_error,
)
from nanoinfra.cli.webui import webui  # noqa: E402
from nanoinfra.cli.webui_support import (  # noqa: E402
    _prepare_webui_bundle_for_gateway,
    _validate_gateway_startup,
)
from nanoinfra.config.paths import get_workspace_path  # noqa: E402
from nanoinfra.config.schema import Config  # noqa: E402
from nanoinfra.security.network import is_loopback_host  # noqa: E402
from nanoinfra.utils.helpers import sanitize_surrogates as _sanitize_surrogates  # noqa: E402,F401
from nanoinfra.utils.helpers import (  # noqa: E402
    sync_workspace_templates,
)

# Backward-compatible import for callers that used the former module location.
SafeFileHistory = cli_terminal.SafeFileHistory


app = typer.Typer(
    name="nanoinfra",
    context_settings={"help_option_names": ["-h", "--help"]},
    help=f"{__logo__} nanoinfra - Personal AI Assistant",
    no_args_is_help=True,
)

console = Console()

def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} nanoinfra v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """nanoinfra - Personal AI Assistant."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


@app.command()
def onboard(
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    wizard: bool = typer.Option(False, "--wizard", help="Use interactive wizard"),
    non_interactive_refresh: bool = typer.Option(False, "--refresh", help="Refresh config, preserving existing settings without prompting"),
):
    """Initialize nanoinfra configuration and workspace."""
    from nanoinfra.config.loader import get_config_path, load_config, save_config, set_config_path
    from nanoinfra.config.schema import Config

    explicit_config = config is not None
    if config:
        config_path = Path(config).expanduser().resolve()
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")
    else:
        config_path = get_config_path()

    def _apply_workspace_override(loaded: Config) -> Config:
        if workspace:
            loaded.agents.defaults.workspace = workspace
        return loaded

    loaded_config: Config | None = None
    # Create or update config
    if config_path.exists():
        if wizard:
            loaded_config = _apply_workspace_override(load_config(config_path))
        else:
            should_refresh = non_interactive_refresh
            if not non_interactive_refresh:
                console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
                console.print(
                    "  [bold]y[/bold] = overwrite with defaults (existing values will be lost)"
                )
                console.print(
                    "  [bold]N[/bold] = refresh config, keeping existing values and adding new fields"
                )
                if typer.confirm("Overwrite?"):
                    loaded_config = _apply_workspace_override(Config())
                    save_config(loaded_config, config_path)
                    console.print(f"[green]✓[/green] Config reset to defaults at {config_path}")
                else:
                    should_refresh = True

            if should_refresh:
                loaded_config = _apply_workspace_override(load_config(config_path))
                save_config(loaded_config, config_path)
                console.print(
                    f"[green]✓[/green] Config refreshed at {config_path} (existing values preserved)"
                )
    else:
        loaded_config = _apply_workspace_override(Config())
        # In wizard mode, don't save yet - the wizard will handle saving if should_save=True
        if not wizard:
            save_config(loaded_config, config_path)
            console.print(f"[green]✓[/green] Created config at {config_path}")

    assert loaded_config is not None

    # Run interactive wizard if enabled
    if wizard:
        from nanoinfra.cli.onboard import run_onboard

        try:
            result = run_onboard(initial_config=loaded_config)
            if not result.should_save:
                console.print("[yellow]Configuration discarded. No changes were saved.[/yellow]")
                return

            loaded_config = result.config
            save_config(loaded_config, config_path)
            console.print(f"[green]✓[/green] Config saved at {config_path}")
        except Exception as e:
            console.print(f"[red]✗[/red] Error during configuration: {e}")
            console.print("[yellow]Please run 'nanoinfra onboard' again to complete setup.[/yellow]")
            raise typer.Exit(1)
    _onboard_plugins(config_path)

    # Create workspace, preferring the configured workspace path.
    workspace_path = get_workspace_path(loaded_config.workspace_path)
    if not workspace_path.exists():
        workspace_path.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] Created workspace at {workspace_path}")

    sync_workspace_templates(workspace_path)

    webui_cmd = "nanoinfra webui"
    if explicit_config:
        webui_cmd += f' -c "{config_path}"'

    typer.echo(f"\n✓ nanoinfra is ready. Run: {webui_cmd}")


def _onboard_plugins(config_path: Path) -> None:
    """Inject default config for all discovered channels (built-in + plugins)."""
    import json

    from nanoinfra.channels.contracts import channel_default_config
    from nanoinfra.channels.registry import discover_plugins
    from nanoinfra.config.loader import merge_missing_defaults

    plugins = discover_plugins()
    if not plugins:
        return

    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)

    channels = data.setdefault("channels", {})
    for name, plugin in plugins.items():
        defaults = channel_default_config(plugin)
        if name not in channels:
            channels[name] = defaults
        else:
            channels[name] = merge_missing_defaults(channels[name], defaults)

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _print_enable_options(
    extras: dict[str, list[str] | None],
    channel_plugins: dict[str, Any],
    config: Config,
) -> None:
    table = Table(title="Available Features")
    table.add_column("Name", style="cyan")
    table.add_column("Type")
    table.add_column("Enabled")

    for item in sorted(set(channel_plugins) | set(extras)):
        plugin = channel_plugins.get(item)
        is_channel = plugin is not None
        enabled = (
            feature_support.channel_enabled(
                config,
                item,
                plugin,
                default_enabled=plugin.default_enabled,
            )
            if is_channel
            else feature_support.extra_installed(item, extras[item])
        )
        table.add_row(
            item,
            "channel" if is_channel else "feature",
            "[green]yes[/green]" if enabled else "[dim]no[/dim]",
        )

    console.print(table)


def _read_trigger_cli_message(message: str | None) -> str:
    """Read a trigger message from an argument or stdin."""
    if message and message.strip():
        return message
    try:
        if not sys.stdin.isatty():
            content = sys.stdin.read()
            if content.strip():
                return content
    except Exception:
        pass
    console.print("[red]Error: trigger message is required[/red]")
    raise typer.Exit(1)


@app.command()
def trigger(
    trigger_id: str = typer.Argument(..., help="Trigger ID returned by /trigger"),
    message: str | None = typer.Argument(None, help="Message to deliver; stdin is used when omitted"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Deliver a local trigger message to its bound chat session."""
    from nanoinfra.triggers.local_store import (
        LocalTriggerStore,
        TriggerDisabledError,
        TriggerNotFoundError,
        TriggerStoreError,
    )

    runtime_config = _load_runtime_config(config, workspace)
    content = _read_trigger_cli_message(message)
    store = LocalTriggerStore(runtime_config.workspace_path)
    try:
        delivery = store.enqueue(trigger_id, content)
    except (TriggerNotFoundError, TriggerDisabledError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1) from exc
    except (TriggerStoreError, ValueError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]Queued[/green] {delivery.trigger_id} ({delivery.id})")


# ============================================================================
# OpenAI-Compatible API Server
# ============================================================================


@app.command()
def serve(
    port: int | None = typer.Option(None, "--port", "-p", help="API server port"),
    host: str | None = typer.Option(None, "--host", "-H", help="Bind address"),
    timeout: float | None = typer.Option(None, "--timeout", "-t", help="Per-request timeout (seconds)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show nanoinfra runtime logs"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Start the OpenAI-compatible API server (/v1/chat/completions)."""
    try:
        from aiohttp import web  # noqa: F401
    except ImportError:
        console.print("[red]aiohttp is required. Install with: nanoinfra plugins enable api[/red]")
        raise typer.Exit(1)

    from nanoinfra.api.server import create_app, drain_outbound
    from nanoinfra.bus.queue import MessageBus
    from nanoinfra.providers.image_generation import image_gen_provider_configs
    from nanoinfra.session.manager import SessionManager

    # The API's own lines stay audible without `--verbose`: an access log and a traceback are the
    # two things a server must never swallow (#215).
    _set_nanoinfra_logs(verbose, always_on=("nanoinfra.api",))

    runtime_config = _load_runtime_config(config, workspace)
    api_cfg = runtime_config.api
    host = host if host is not None else api_cfg.host
    port = port if port is not None else api_cfg.port
    timeout = timeout if timeout is not None else api_cfg.timeout
    api_key = api_cfg.api_key.strip() if api_cfg.api_key else ""
    if not is_loopback_host(host) and not api_key:
        console.print(
            f"[red]Error: host {host} is available beyond this device but api_key is not set. "
            "Set api.api_key in config to prevent unauthenticated access.[/red]"
        )
        raise typer.Exit(1)
    sync_workspace_templates(runtime_config.workspace_path)
    bus = MessageBus()
    session_manager = SessionManager(runtime_config.workspace_path)
    try:
        agent_loop = AgentLoop.from_config(
            runtime_config, bus,
            session_manager=session_manager,
            image_generation_provider_configs=image_gen_provider_configs(runtime_config),
            hook_factories=[create_file_edit_activity_hook],
        )
    except ValueError as exc:
        console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(1) from exc

    model_name, preset_tag = _model_display(runtime_config)
    console.print(f"{__logo__} Starting OpenAI-compatible API server")
    console.print(f"  [cyan]Endpoint[/cyan] : http://{host}:{port}/v1/chat/completions")
    console.print(f"  [cyan]Model[/cyan]    : {model_name}{preset_tag}")
    console.print("  [cyan]Session[/cyan]  : api:default")
    console.print(f"  [cyan]Timeout[/cyan]  : {timeout}s")
    if not is_loopback_host(host):
        console.print(
            "[yellow]API is available beyond this device "
            "(authentication required).[/yellow]"
        )
    console.print()

    api_app = create_app(
        agent_loop, model_name=model_name, request_timeout=timeout,
        api_key=api_key,
    )

    drain: dict[str, Any] = {}

    async def on_startup(_app: Any) -> None:
        await agent_loop._connect_mcp()
        # Nothing else here consumes the outbound bus, and `publish_outbound` blocks on a full
        # queue: without this a turn stalls after a thousand events and its request hangs.
        drain["task"] = asyncio.create_task(drain_outbound(bus))

    async def on_cleanup(_app: Any) -> None:
        task = drain.pop("task", None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await agent_loop.close_mcp()

    api_app.on_startup.append(on_startup)
    api_app.on_cleanup.append(on_cleanup)

    def _log_aiohttp(message: object) -> None:
        logger.info("{}", message)

    web.run_app(api_app, host=host, port=port, print=_log_aiohttp)


# ============================================================================
# WebUI Launcher
# ============================================================================

app.command(name="webui")(webui)


# ============================================================================
# Gateway / Server
# ============================================================================


app.add_typer(
    create_gateway_app(
        console=console,
        log_handler_id=_log_handler_id,
        load_runtime_config=_load_runtime_config,
        run_gateway=_run_gateway,
        validate_startup_config=_validate_gateway_startup,
        prepare_webui_bundle=lambda config, mode: _prepare_webui_bundle_for_gateway(
            config,
            mode=mode,
        ),
    ),
    name="gateway",
)


# ============================================================================
# Agent Commands
# ============================================================================


app.command(name="agent")(agent)


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status(
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Show channel status."""
    from nanoinfra.channels.registry import discover_all

    _, loaded = _load_inspection_config(config=config)

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled")

    for name, cls in sorted(discover_all().items()):
        section = getattr(loaded.channels, name, None)
        if section is None:
            enabled = False
        elif isinstance(section, dict):
            enabled = cast(dict[str, Any], section).get("enabled", False)
        else:
            enabled = getattr(section, "enabled", False)
        table.add_row(
            cls.display_name,
            "[green]\u2713[/green]" if enabled else "[dim]\u2717[/dim]",
        )

    console.print(table)


@channels_app.command("login")
def channels_login(
    channel_name: str = typer.Argument(..., help="Channel name (e.g. weixin, whatsapp)"),
    force: bool = typer.Option(False, "--force", "-f", help="Force re-authentication even if already logged in"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Authenticate with a channel via QR code or other interactive login."""
    from nanoinfra.bus.queue import MessageBus
    from nanoinfra.channels.registry import discover_all

    _, loaded = _load_inspection_config(config=config)
    channel_cfg: Any = getattr(loaded.channels, channel_name, None) or {}

    # Validate channel exists
    all_channels = discover_all()
    if channel_name not in all_channels:
        available = ", ".join(all_channels.keys())
        console.print(f"[red]Unknown channel: {channel_name}[/red]  Available: {available}")
        raise typer.Exit(1)

    console.print(f"{__logo__} {all_channels[channel_name].display_name} Login\n")

    channel_factory = all_channels[channel_name]
    channel = channel_factory(channel_cfg, bus=MessageBus())

    success = asyncio.run(channel.login(force=force))

    if not success:
        raise typer.Exit(1)


# ============================================================================
# Plugin Commands
# ============================================================================

plugins_app = typer.Typer(help="Manage optional nanoinfra features")
app.add_typer(plugins_app, name="plugins")

# Named agent-plugins and not plugins: `nanoinfra plugins` already means optional features
# (channel SDKs and friends), and Agent Plugins are a different thing entirely.
#
# Read-only on purpose, and not merely by preference. Activation is declared in
# tools.agentPlugins and reconciled by the executor (#141), so a CLI that enabled a package would
# be a second authority contradicting the first. Inspection is what an operator needs here, since
# the WebUI panel is read-only for the same reason.
agent_plugins_app = typer.Typer(help="Inspect installed Agent Plugins")
app.add_typer(agent_plugins_app, name="agent-plugins")


def _agent_plugin_state(config_path: str | None):
    """Resolve the workspace and the discovered packages for one command."""
    from nanoinfra.agent.plugins import discover_agent_plugins
    from nanoinfra.config.loader import load_config, set_config_path
    from nanoinfra.config.paths import get_workspace_path

    resolved = Path(config_path).expanduser().resolve() if config_path else None
    if resolved is not None:
        set_config_path(resolved)
    config = load_config(resolved)
    workspace = get_workspace_path(config.agents.defaults.workspace)
    declared = set(config.tools.agent_plugins or [])
    return workspace, discover_agent_plugins(workspace), declared


@agent_plugins_app.command("list")
def agent_plugins_list(
    config_path: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """List installed Agent Plugins and whether each one is active."""
    workspace, plugins, declared = _agent_plugin_state(config_path)
    if not plugins:
        console.print(f"No Agent Plugins installed under {escape(str(workspace / 'plugins'))}.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("NAME")
    table.add_column("STATE")
    table.add_column("BUNDLES")
    for plugin in plugins:
        bundles: list[str] = []
        if plugin.mcp_servers:
            bundles.append(f"{len(plugin.mcp_servers)} mcp")
        # A package listed in config that is not active failed its fingerprint check, which is a
        # different situation from one nobody listed.
        if plugin.enabled:
            state = "active"
        elif plugin.name in declared:
            state = "modified"
        else:
            state = "inactive"
        table.add_row(plugin.name, state, ", ".join(bundles) or "skills only")
    console.print(table)

    unknown = sorted(declared - {plugin.name for plugin in plugins})
    if unknown:
        console.print(
            f"[yellow]tools.agentPlugins names no installed package: {escape(', '.join(unknown))}"
            "[/yellow]"
        )


@agent_plugins_app.command("show")
def agent_plugins_show(
    name: str = typer.Argument(..., help="Agent Plugin identity (e.g. acme.deploy-toolkit)"),
    config_path: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Show one Agent Plugin's components and why it is or is not active."""
    _workspace, plugins, declared = _agent_plugin_state(config_path)
    plugin = next((item for item in plugins if item.name == name), None)
    if plugin is None:
        console.print(f"[red]No Agent Plugin named {escape(name)} is installed.[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]{escape(plugin.display_name)}[/bold] ({escape(plugin.name)})")
    if plugin.description:
        console.print(escape(plugin.description))
    console.print(f"  root       {escape(str(plugin.root))}")
    console.print(f"  category   {escape(plugin.category)}")
    if plugin.repository:
        console.print(f"  repository {escape(plugin.repository)}")
    console.print(f"  mcp        {escape(', '.join(plugin.mcp_servers)) or 'none'}")
    if plugin.permissions:
        console.print(f"  permissions {escape(', '.join(plugin.permissions))}")

    if plugin.enabled:
        console.print("  state      [green]active[/green]")
    elif plugin.name in declared:
        console.print(
            "  state      [yellow]modified[/yellow] — listed in tools.agentPlugins, but its "
            "content changed since it was activated. Restart the gateway to review and re-bind it."
        )
    else:
        console.print(
            "  state      inactive — add it to tools.agentPlugins in config.json to activate it."
        )


@plugins_app.command("list")
def plugins_list(
    config_path: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """List optional nanoinfra features."""
    from nanoinfra.channels.registry import discover_plugins
    from nanoinfra.config.loader import load_config, set_config_path

    resolved_config_path = Path(config_path).expanduser().resolve() if config_path else None
    if resolved_config_path is not None:
        set_config_path(resolved_config_path)

    _print_enable_options(
        feature_support.optional_dependency_groups(),
        discover_plugins(),
        load_config(resolved_config_path),
    )


@plugins_app.command("enable")
def plugins_enable(
    name: str = typer.Argument(..., help="Feature name (e.g. weixin, matrix, bedrock)"),
    config_path: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    logs: bool = typer.Option(False, "--logs/--no-logs", help="Show optional package install logs"),
):
    """Enable a nanoinfra feature."""
    from nanoinfra.config.loader import get_config_path, set_config_path

    resolved_config_path = Path(config_path).expanduser().resolve() if config_path else None
    if resolved_config_path is not None:
        set_config_path(resolved_config_path)
    resolved_config_path = resolved_config_path or get_config_path()
    _set_nanoinfra_logs(logs)

    try:
        payload = feature_support.enable_optional_feature(
            name,
            config_path=resolved_config_path,
            runner=feature_support.run_install_command,
        )
    except feature_support.OptionalFeatureError as exc:
        console.print(f"[red]{escape(exc.message)}[/red]")
        raise typer.Exit(1) from exc

    message = payload.get("last_action", {}).get("message") or f"Enabled feature '{name}'"
    console.print(f"[green]{escape(message)}[/green]")


@plugins_app.command("disable")
def plugins_disable(
    name: str = typer.Argument(..., help="Channel name (e.g. telegram, matrix, slack)"),
    config_path: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Disable a nanoinfra channel feature."""
    from nanoinfra.config.loader import get_config_path, set_config_path

    resolved_config_path = Path(config_path).expanduser().resolve() if config_path else None
    if resolved_config_path is not None:
        set_config_path(resolved_config_path)
    resolved_config_path = resolved_config_path or get_config_path()

    try:
        payload = feature_support.disable_optional_feature(name, config_path=resolved_config_path)
    except feature_support.OptionalFeatureError as exc:
        console.print(f"[red]{escape(exc.message)}[/red]")
        raise typer.Exit(1) from exc

    message = payload.get("last_action", {}).get("message") or f"Disabled channel '{name}'"
    console.print(f"[green]{escape(message)}[/green] in {resolved_config_path}")


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status(
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
):
    """Show nanoinfra status."""
    config_path, loaded = _load_inspection_config(config=config, workspace=workspace)
    workspace_path = loaded.workspace_path

    console.print(f"{__logo__} nanoinfra Status\n")

    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(
        f"Workspace: {workspace_path} "
        f"{'[green]✓[/green]' if workspace_path.exists() else '[red]✗[/red]'}"
    )

    if config_path.exists():
        from nanoinfra.config.errors import ConfigLoadError
        from nanoinfra.config.loader import resolve_config_env_vars, resolve_env_refs
        from nanoinfra.providers.registry import PROVIDERS

        _model, _preset_tag = _model_display(loaded)
        console.print(f"Model: {_model}{_preset_tag}")

        provider_ready = False
        try:
            resolved = resolve_config_env_vars(
                loaded.model_copy(deep=True),
                config_path=config_path,
            )
        except ConfigLoadError as exc:
            console.print("Agent: [red]✗ configuration is not ready[/red]")
            _print_config_error(exc)
        else:
            provider_error = _provider_setup_error(resolved)
            if provider_error:
                console.print(Text(f"Agent: ✗ {provider_error}", style="red"))
                console.print("Complete provider/model setup:")
                _print_model_setup_steps(config_path)
            else:
                provider_ready = True
                console.print("Agent: [green]✓ provider/model configuration is ready[/green]")

        # Check API keys from registry
        for spec in PROVIDERS:
            p = getattr(loaded.providers, spec.name, None)
            if p is None:
                continue
            if spec.is_oauth:
                console.print(f"{spec.label}: [green]✓ (OAuth)[/green]")
            elif spec.is_local:
                # Local deployments show api_base instead of api_key
                if resolve_env_refs(p.api_base or ""):
                    console.print(f"{spec.label}: [green]✓ {p.api_base}[/green]")
                else:
                    console.print(f"{spec.label}: [dim]not set[/dim]")
            else:
                has_key = bool(resolve_env_refs(p.api_key or ""))
                console.print(f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}")

        if provider_ready:
            console.print()
            console.print('Next: [cyan]nanoinfra agent -m "Hello!"[/cyan]')
            console.print(
                "[dim]Status does not call the model or verify network access and credentials.[/dim]"
            )
    else:
        console.print("Agent: [red]✗ configuration file not found[/red]")
        console.print("Create the provider/model configuration:")
        _print_model_setup_steps(config_path)


# ============================================================================
# Data connectors
# ============================================================================

# Read plus one write, and the write is a consent flow rather than an activation. Activating a
# connector is declared in config and applied by the executor -- the same rule
# `tools.agentPlugins` states -- so a CLI that enabled one would be a second authority
# contradicting the first. Consent is different in kind: it is a person at a browser saying "act
# as me", which no config file and no chat turn can do on their behalf.
connectors_app = typer.Typer(help="Inspect data connectors and authorise one")
app.add_typer(connectors_app, name="connectors")


def _print_plain(text: str) -> None:
    """Print a line from the consent flow with no markup interpretation.

    A URL carries square brackets in a query string often enough, and rich would read those as
    markup and print a URL nobody can paste.
    """
    console.print(escape(text))


def _connector_state(config_path: str | None):
    from nanoinfra.config.loader import load_config, set_config_path
    from nanoinfra.connectors.registry import discover_connectors

    resolved = Path(config_path).expanduser().resolve() if config_path else None
    if resolved is not None:
        set_config_path(resolved)
    config = load_config(resolved)
    return config, discover_connectors()


@connectors_app.command("list")
def connectors_list(
    config_path: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """List installed connectors, their operations, and each operation's capability class."""
    from nanoinfra.connectors.registry import operation_summary
    from nanoinfra.connectors.setup import resolve_active

    config, installed = _connector_state(config_path)
    if not installed:
        console.print("No connectors are installed.")
        return

    active, problems = resolve_active(config.connectors)
    active_names = {entry.name for entry in active}
    enabled = {entry.name: {op.name for op in entry.operations} for entry in active}

    table = Table(show_header=True, header_style="bold")
    table.add_column("CONNECTOR")
    table.add_column("STATE")
    table.add_column("OPERATION")
    table.add_column("CLASS")
    for name, plugin in sorted(installed.items()):
        if name in active_names:
            state = "active"
        elif name in config.connectors.active:
            state = "[yellow]not activated[/yellow]"
        else:
            state = "[dim]inactive[/dim]"
        for row in operation_summary(plugin):
            offered = row["name"] in enabled.get(name, set())
            operation = row["name"] if offered or name not in active_names else f"[dim]{row['name']}[/dim]"
            table.add_row(name, state, operation, str(row["capability_class"]))
            state = ""
            name = ""
    console.print(table)

    for problem in problems:
        console.print(f"[yellow]{escape(str(problem))}[/yellow]")


@connectors_app.command("authorize")
def connectors_authorize(
    name: str = typer.Argument(..., help="Connector name (e.g. google-calendar)"),
    client_id: str = typer.Option(..., "--client-id", help="OAuth client id for this deployment"),
    client_secret: str = typer.Option(
        ...,
        "--client-secret",
        prompt=True,
        hide_input=True,
        help="OAuth client secret. Prompted, so it stays out of the shell history.",
    ),
    account: str = typer.Option(
        "", "--account", help="Pre-fill the consent screen with this address"
    ),
    port: int = typer.Option(
        0, "--port", help="Loopback port for the redirect. Defaults to the connector default."
    ),
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the URL and open nothing"),
    manual: bool = typer.Option(
        False,
        "--manual",
        help=(
            "Start no local server: open the URL yourself and paste the URL Google redirects "
            "to. Use this from a shell with no browser, or when the browser runs elsewhere."
        ),
    ),
    config_path: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Consent as a person, and store the refresh token this connector will act with.

    The connector acts as whoever completes this flow, so run it as that account. Two secrets
    are written -- the refresh token and the client secret -- and their ids are printed with the
    config to add. Nothing else is printed, and neither value is.
    """
    from nanoinfra.config.paths import get_workspace_path
    from nanoinfra.connectors.authorize import (
        DEFAULT_LOOPBACK_PORT,
        AuthorizationError,
        run_consent_flow,
    )
    from nanoinfra.secrets.store import SecretStore

    config, installed = _connector_state(config_path)
    plugin = installed.get(name)
    if plugin is None:
        console.print(
            f"[red]No connector named {escape(name)} is installed.[/red] Installed: "
            f"{escape(', '.join(sorted(installed)) or 'none')}."
        )
        raise typer.Exit(1)

    try:
        authorization = run_consent_flow(
            plugin,
            client_id=client_id,
            client_secret=client_secret,
            port=port or DEFAULT_LOOPBACK_PORT,
            login_hint=account,
            open_browser=not (no_browser or manual),
            manual=manual,
            print_fn=_print_plain,
        )
    except AuthorizationError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc

    workspace = get_workspace_path(config.agents.defaults.workspace)
    store = SecretStore(workspace)
    stem = name.replace("-", "_")
    try:
        refresh_secret = store.create(
            {
                "name": f"{stem}_refresh_token",
                "kind": "token",
                "providerId": "local",
                "value": authorization.refresh_token,
            }
        )
        client_secret_record = store.create(
            {
                "name": f"{stem}_client_secret",
                "kind": "api_key",
                "providerId": "local",
                "value": client_secret,
            }
        )
    except Exception as exc:  # noqa: BLE001 -- the store names its own failures
        console.print(f"[red]The consent worked and the secret store refused: {escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc

    credential = f"{stem}_credential"
    console.print()
    console.print("[green]Authorised.[/green] Two secrets were stored, and no value was printed.")
    console.print()
    console.print("Add this to your config, review it, and restart the gateway:")
    console.print()
    console.print(
        escape(
            json.dumps(
                {
                    "connectors": {
                        "credentials": {
                            credential: {
                                "clientId": client_id,
                                "secretRef": refresh_secret.id,
                                "clientSecretRef": client_secret_record.id,
                                "scopes": list(authorization.granted_scopes)
                                or list(plugin.credential.declared_scopes()),
                            }
                        },
                        "connectors": {name: {"credential": credential}},
                        "active": [name],
                    }
                },
                indent=2,
            )
        )
    )


# ============================================================================
# OAuth Login
# ============================================================================

app.add_typer(provider_app, name="provider")


if __name__ == "__main__":
    app()
