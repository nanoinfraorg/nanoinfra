---
name: google-calendar
description: Read and write events on a Google Calendar through the google-calendar connector. Use when the user asks what is on their calendar, when they are free, or asks to schedule, move, or check an event.
---

# Google Calendar

Tools: `google_calendar_list_events`, `google_calendar_list_calendars`,
`google_calendar_get_event`, `google_calendar_create_event`, `google_calendar_delete_event`.

The reads and the writes are enforced apart, not merely labelled: `create_event` and
`delete_event` carry the `mutate.remote` capability class, so each asks a person in an
interactive turn and needs a standing grant naming this connector to run unattended. The
reads carry `read` and simply run.

## Answering "what is on my calendar"

Call `google_calendar_list_events` with `timeMin` and `timeMax` as RFC 3339 timestamps. Do not
try to search: this API filters by time, and a listing with a window is the whole answer.

- Omit `timeMin` and events return from now on. Say which window you used, because "today"
  ends at midnight in a timezone the user did not state.
- An all-day event carries `start.date`; a timed one carries `start.dateTime`. They are
  different fields, and a date has no time to compare against.
- A declined invitation is still an event on the calendar. If the user asks whether they are
  free, read `status` and the attendee list rather than assuming a row means a commitment.
- `maxResults` defaults to 10. A user asking about a whole week needs it raised, and a listing
  that hit the cap returns `nextPageToken` — say the answer is partial rather than presenting
  ten rows as the week.

## Creating an event

`google_calendar_create_event` writes to a calendar other people can see, and an invitation may
send mail to attendees. So:

1. State the summary, the exact start and end with their offset, and the attendees you are
   about to send, and wait for the user to confirm that text.
2. Times need an offset (`2026-09-01T10:00:00-06:00`) or they are ambiguous. If the user said
   "10am" and no timezone is established, ask rather than guessing from the server's clock.
3. An all-day event uses `{"date": "2026-09-01"}` and never `dateTime`.
4. If the call is refused, the refusal names what would permit it. Report that text — do not
   retry, and do not look for another route to the same write.

## Deleting an event

`google_calendar_delete_event` takes an `eventId` and carries the same `mutate.remote` class as
creating one. It answers with nothing on success — a `204`, no body — so the result confirms the
call returned, not what was removed.

1. Find the id first. The user names an event ("cancel my 3pm"), not an id; list or read to
   resolve it, and state the event's summary and time back to them before deleting.
2. Deleting a meeting with attendees notifies them per the calendar's own settings. Treat it
   like sending mail: confirm the specific event, then delete.
3. There is no undo. If you deleted the wrong one, say so — you cannot restore it, only create
   a new event, which is not the same event.

## Credentials

This connector resolves the credential named for it in config. There is no tool that reads,
lists, or sets it, and none that returns the token — the token is minted per action by the
process that holds the refresh token. If the connector reports that it needs re-authorising,
tell the user; the fix is an operator action in the WebUI, not something to work around.
