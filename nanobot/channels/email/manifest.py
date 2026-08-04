"""Email management contract."""

from nanoinfra.channels._manifest import field, required_fields
from nanoinfra.channels.contracts import ChannelSetupSpec
from nanoinfra.channels.email.validation import validate
from nanoinfra.channels.plugin import ChannelPlugin

SETUP_SPEC = ChannelSetupSpec(
    fields={
        "consentGranted": field("bool", default=False),
        "imapHost": field(),
        "imapPort": field("int", default=993),
        "imapUsername": field(),
        "imapPassword": field("secret"),
        "smtpHost": field(),
        "smtpPort": field("int", default=587),
        "smtpUsername": field(),
        "smtpPassword": field("secret"),
        "fromAddress": field(),
        "pollIntervalSeconds": field("int", default=30),
        "allowFrom": field("list"),
        "verifyDkim": field("bool", default=True),
        "verifySpf": field("bool", default=True),
    },
    required=required_fields(
        "consentGranted",
        "imapHost",
        "imapUsername",
        "imapPassword",
        "smtpHost",
        "smtpUsername",
        "smtpPassword",
    ),
    official_url="https://support.google.com/accounts/answer/185833",
    validator=validate,
)

PLUGIN = ChannelPlugin(
    name="email",
    display_name="Email",
    runtime=f"{__package__}.runtime:EmailChannel",
    setup=SETUP_SPEC,
    webui="webui/index.ts",
)
