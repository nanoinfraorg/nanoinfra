"""Signal management contract."""

from nanoinfra.channels._manifest import agent_field, field, required
from nanoinfra.channels.contracts import ChannelSetupSpec
from nanoinfra.channels.plugin import ChannelPlugin

SETUP_SPEC = ChannelSetupSpec(
    fields={
        "agent": agent_field(),
        "phoneNumber": field(),
        "daemonHost": field(default="localhost"),
        "daemonPort": field("int", default=8080),
        "dm.allowFrom": field("list"),
        "group.allowFrom": field("list"),
    },
    required=(required("phoneNumber"),),
    official_url="https://github.com/bbernhard/signal-cli-rest-api",
)

PLUGIN = ChannelPlugin(
    name="signal",
    display_name="Signal",
    runtime=f"{__package__}.runtime:SignalChannel",
    setup=SETUP_SPEC,
    webui="webui/index.ts",
)
