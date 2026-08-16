"""WhatsApp management contract."""

from nanoinfra.channels._manifest import DIRECT_GROUP_POLICIES, field
from nanoinfra.channels.contracts import ChannelManagementSpec, ChannelSetupSpec
from nanoinfra.channels.plugin import ChannelPlugin
from nanoinfra.channels.whatsapp.state import local_state_present
from nanoinfra.channels.whatsapp.validation import validate

SETUP_SPEC = ChannelSetupSpec(
    fields={
        "allowFrom": field("list", snapshot=False),
        "groupPolicy": field(
            "enum",
            choices=DIRECT_GROUP_POLICIES,
            default="open",
            snapshot=False,
        ),
        "databasePath": field(writable=False, snapshot=False),
    },
    official_url="https://faq.whatsapp.com/",
    validator=validate,
)

PLUGIN = ChannelPlugin(
    name="whatsapp",
    display_name="WhatsApp",
    runtime=f"{__package__}.runtime:WhatsAppChannel",
    setup=SETUP_SPEC,
    management=ChannelManagementSpec(local_state_present=local_state_present),
    dependencies=(
        # neonize binds whatsmeow, and WhatsApp breaks an older build when it changes its
        # protocol: a device then reads the QR and refuses to link, and the phone blames its own
        # connection. The floor moves with the ceiling on purpose. A ceiling alone would leave
        # every existing install on the version it already has, because pip upgrades nothing that
        # already satisfies the requirement, so the fix would reach nobody who needs it.
        "neonize>=0.4.3.post0,<0.5.0",
        "segno>=1.6.1,<2.0.0",
    ),
    webui="webui/index.ts",
)
