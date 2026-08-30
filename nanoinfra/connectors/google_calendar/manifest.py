"""Google Calendar: the first connector, and the one that proves the path.

Calendar rather than Gmail first, deliberately: it is the smallest surface with a
real read case, and one scope (`calendar.events`) gates a whole class, so enabling
it exercises consent, refresh, scope subsetting, the projection and the gate
answering two different things — without the volume of a mailbox.

No SDK. `httpx` is already a base dependency, the Workspace APIs are documented as
REST, and an explicit method and path are what make a declared capability class
checkable: a reviewer or the loader can see that a `read` operation is a `GET`.
`google-api-python-client` is synchronous, discovery-based and hides both.
"""

from nanoinfra.channels._manifest import field, required_fields
from nanoinfra.connectors.contracts import (
    ConnectorCredentialSpec,
    ConnectorMentionSpec,
    ConnectorPlugin,
    ConnectorSetupSpec,
    operation,
)

# One OAuth client per deployment, never one shipped in this package: a shared client
# id would make every deployment share one app's quota, verification status and
# revocation. On a Workspace domain an operator marks the consent screen Internal,
# which is what keeps restricted scopes out of Google's verification queue.
SETUP = ConnectorSetupSpec(
    fields={
        "clientId": field("string"),
        "clientSecret": field("secret"),
        "calendarId": field("string", default="primary"),
    },
    required=required_fields("clientId", "clientSecret"),
    official_url="https://console.cloud.google.com/apis/credentials",
)

CREDENTIAL = ConnectorCredentialSpec(
    kind="oauth2",
    # The scopes each class needs. The executor mints an access token for the
    # intersection of what the credential was granted and what the class asks for, so
    # a read cannot borrow the write scope even though one credential holds both.
    scopes={
        "read": ("https://www.googleapis.com/auth/calendar.readonly",),
        "mutate.remote": ("https://www.googleapis.com/auth/calendar.events",),
    },
    token_url="https://oauth2.googleapis.com/token",
)

PLUGIN = ConnectorPlugin(
    name="google-calendar",
    display_name="Google Calendar",
    description="Read and write events on a Google Calendar.",
    base_url="https://www.googleapis.com",
    credential=CREDENTIAL,
    setup=SETUP,
    skill="SKILL.md",
    # A calendar id is stable and its name is not, which is exactly when pinning is worth
    # having: an automation that says "the team calendar" re-matches on every run.
    mentions=(
        ConnectorMentionSpec(
            kind="calendar",
            operation="list_calendars",
            id_field="id",
            label_field="summary",
            detail_fields=("accessRole", "timeZone"),
            argument="calendarId",
        ),
    ),
    operations=(
        operation(
            "list_events",
            "read",
            "GET",
            "/calendar/v3/calendars/{calendarId}/events",
            returns=("id", "summary", "start", "end", "status", "attendees.email"),
            collection="items",
            description=(
                "Events on a calendar in a time range. Pass timeMin and timeMax as RFC 3339 "
                "timestamps; omit them and it returns from now on."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "calendarId": {
                        "type": "string",
                        "description": "Calendar id. Defaults to the configured one.",
                    },
                    "timeMin": {"type": "string", "description": "RFC 3339 lower bound."},
                    "timeMax": {"type": "string", "description": "RFC 3339 upper bound."},
                    "maxResults": {"type": "integer", "description": "1-250, default 10."},
                },
            },
        ),
        operation(
            "list_calendars",
            "read",
            "GET",
            "/calendar/v3/users/me/calendarList",
            returns=("id", "summary", "primary", "accessRole", "timeZone"),
            collection="items",
            description=(
                "The calendars this account can see, with their ids. Use it to find the id of a "
                "calendar the user named; the primary one carries primary=true and its id is the "
                "account's own address."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "maxResults": {"type": "integer", "description": "1-250, default 100."},
                },
            },
        ),
        operation(
            "get_event",
            "read",
            "GET",
            "/calendar/v3/calendars/{calendarId}/events/{eventId}",
            returns=("id", "summary", "description", "start", "end", "status", "attendees.email"),
            description="One event, by id.",
            parameters={
                "type": "object",
                "properties": {
                    "calendarId": {"type": "string"},
                    "eventId": {"type": "string"},
                },
                "required": ["eventId"],
            },
        ),
        operation(
            "create_event",
            "mutate.remote",
            "POST",
            "/calendar/v3/calendars/{calendarId}/events",
            returns=("id", "summary", "start", "end", "htmlLink"),
            description=(
                "Create an event. Times are RFC 3339 with an offset for a timed event, or a "
                "date for an all-day one."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "calendarId": {"type": "string"},
                    "summary": {"type": "string"},
                    "description": {"type": "string"},
                    "start": {"type": "object", "description": '{"dateTime": "..."} or {"date": "..."}'},
                    "end": {"type": "object"},
                    "attendees": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["summary", "start", "end"],
            },
        ),
    ),
)
