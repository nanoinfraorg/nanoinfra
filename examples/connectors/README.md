# Example connectors

A connector declares the requests it may make and the capability class of each one. Nothing here is
a runtime: there is no code in these directories, and the loader refuses a package that ships any.

## `hello-world/`

The smallest connector that actually runs: one `read` against a public weather API, no credential.

```bash
mkdir -p ~/.nanoinfra/workspaces/default/connectors
cp -r examples/connectors/hello-world ~/.nanoinfra/workspaces/default/connectors/
```

Then enable it, which is a config action and stays one — installing a package and granting it a
capability are two decisions:

```json
{
  "tools": { "connectors": true },
  "connectors": {
    "active": ["hello-world"],
    "connectors": {
      "hello-world": {
        "settings": { "latitude": "19.4326", "longitude": "-99.1332" }
      }
    }
  }
}
```

`nanoinfra connectors list` then shows it, and the agent gets one tool,
`connector_hello_world_current_weather`, carrying the class `read`.

No `credential` key is needed because the package declares `credential.kind: "none"`. A connector
against a private API needs one, and the WebUI's **Connect** button is the shortest way to get it —
see [Data connectors](https://docs.nanoinfra.org/data-connectors).

## Writing your own

Copy `hello-world/connector.json` and change five things:

1. `name`, which must match the directory name, because that is what config refers to.
2. `baseUrl`, which must be `https`. A token travels on it.
3. `operations`, one per call, each with a `class`. A `read` class on a writing method is refused,
   and that refusal is what makes the declaration worth trusting.
4. `returns`, the fields to keep. Everything else in the response is dropped before the model sees
   it.
5. `credential`, if the API needs one. Declare the scopes each class needs, and **name the hosts the
   package may address** in `allowedHosts` — a manifest declares where a token goes, so a package
   naming scopes and a `baseUrl` nobody reviewed would otherwise receive a live token.
