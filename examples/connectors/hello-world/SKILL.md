---
name: hello-world-connector
description: Use when asked about current weather for a point, or when demonstrating what a connector is.
---

# Hello World connector

One operation, `current_weather`, against a public API that needs no credential. It exists to show
the shape of a connector, so read it as an example first and a weather tool second.

## Using it

Pass `latitude` and `longitude` in decimal degrees. Both have configured defaults, so a call with no
arguments answers for whatever point the operator set.

```
current_weather(latitude="48.8566", longitude="2.3522")
```

The answer is projected down to six fields. The API returns far more than that, and the projection
is the point: a declared `returns` caps what a call that *is* allowed can carry out, and keeps the
context window for the task rather than for hourly arrays.

## What it demonstrates, and what it does not

`current_weather` is declared `read` on a `GET`, so a policy that allows reads never asks about it.
There is no `mutate` operation here at all — this connector cannot change anything, anywhere.

Because `credential.kind` is `none`, no token is minted and no `Authorization` header is sent. That
is why it installs and runs with no setup. A real connector against a private API declares `oauth2`,
names the scopes each capability class needs, and names the hosts it may address — and none of those
are needed to see how the format works.
