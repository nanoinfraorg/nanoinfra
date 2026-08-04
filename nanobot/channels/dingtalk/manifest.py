"""DingTalk management contract."""

from nanoinfra.channels._manifest import field, required_fields
from nanoinfra.channels.contracts import ChannelSetupSpec
from nanoinfra.channels.plugin import ChannelPlugin

SETUP_SPEC = ChannelSetupSpec(
    fields={
        "clientId": field(),
        "clientSecret": field("secret"),
        "allowFrom": field("list"),
    },
    required=required_fields("clientId", "clientSecret"),
    official_url="https://open.dingtalk.com/",
)

PLUGIN = ChannelPlugin(
    name="dingtalk",
    display_name="DingTalk",
    runtime=f"{__package__}.runtime:DingTalkChannel",
    setup=SETUP_SPEC,
    dependencies=("dingtalk-stream>=0.24.0,<1.0.0",),
    webui="webui/index.ts",
)
