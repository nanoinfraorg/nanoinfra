# An identity in front of the gateway

This directory is an example. It is not a deployment. It runs on one host, it publishes every
port to loopback, and every secret in it is a placeholder that names itself as one. Change three
things before this stack faces anything other than your own loopback interface. **One**, the four
placeholders: nothing here works until you replace them. **Two**, the person:
`alberto@example.com` is an example and it is not you, and the address appears in three files.
**Three**, the transport: the example speaks HTTP and marks no cookie secure, so a deployment
terminates TLS, sets `cookie_secure = true` in `oauth2-proxy.cfg`, and writes `https://` in every
address that carries a scheme: the issuer in three files, the redirect address in two, and the
WebSocket URL in `config.json`, which becomes `wss://`.

### The four placeholders

| Placeholder | Where it is | What to put there |
|---|---|---|
| `REPLACE-ME-dex-client-secret` | `dex.yaml`, `staticClients[0].secret`, and `oauth2-proxy.alpha.yaml`, `providers[0].clientSecret` | One random string. The same value in both files. |
| `REPLACE-ME-cookie-secret-32-bytes` | `compose.yaml`, `OAUTH2_PROXY_COOKIE_SECRET` | 32 random bytes: `openssl rand -base64 32 \| tr -- '+/' '-_'` |
| `REPLACE-ME-provider-api-key` | `compose.yaml`, `ANTHROPIC_API_KEY` | Your model provider key. |
| `$2y$10$REPLACEME...` | `dex.yaml`, `staticPasswords[0].hash` | A bcrypt hash. The command is under [Add a person](#add-a-person). |

The two values in `compose.yaml` read an environment variable first. Write them into
`examples/auth/.env`, which git ignores, and leave the tracked file alone. `config.json` names no
secret at all, because the provider key reaches the gateway as an environment variable.

Two placeholders refuse rather than work. The cookie secret is 33 bytes, and oauth2-proxy accepts
16, 24 or 32, so the proxy stops at startup and says so. No password produces the bcrypt hash, so
Dex starts and refuses every login. A placeholder that is easy to leave in place is a placeholder
that reaches a deployment.

### The person

| File | Key | What it decides |
|---|---|---|
| `dex.yaml` | `staticPasswords[0]` | Who can log in, and with which password. |
| `oauth2-proxy.emails` | the one line | Who reaches the agent at all. |
| `config.json` | `trustedProxyAuth.allowedIdentities` | Who the gateway admits, after it verifies the token. |
| `config.json` | `gates.approvers[0].sender` | Whose approval counts. |

The last two lists are not the same list, and they do not protect the same thing. A person in
`allowedIdentities` holds a chat session with the agent. A person in `gates.approvers` can answer
an approval. A stranger who completes the flow at a public provider holds a token that verifies
correctly, so `allowedIdentities` is what keeps that stranger out. The WebSocket channel's own
`allowFrom` list is a third thing again: it matches a client id, and a client id carries
reachability and no authority.

## The three values that point the gateway at any provider

The gateway runs no OIDC flow. It holds no client secret, and it knows no realm. It reads a
header, and it fetches a JWKS. So the provider is interchangeable, and three values in
`config.json` are the whole choice:

```json
"issuer": "http://dex.localhost:5556/dex",
"audience": "nanoinfra-webui",
"jwksUrl": "http://dex:5556/dex/keys"
```

| Provider | `issuer` | `audience` | `jwksUrl` |
|---|---|---|---|
| Keycloak | `https://<host>/realms/<realm>` | the client id | `<issuer>/protocol/openid-connect/certs` |
| Google | `https://accounts.google.com` | the OAuth client id | `https://www.googleapis.com/oauth2/v3/certs` |
| Cloudflare Access | `https://<team>.cloudflareaccess.com` | the Access application AUD tag | `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs` |

Keycloak and Google replace Dex, and oauth2-proxy stays. Cloudflare Access replaces both, because
it runs the flow itself and asserts `Cf-Access-Jwt-Assertion`. Nothing else in this stack changes,
and nothing in the repository changes.

The `issuer` and the `jwksUrl` name different hosts in this example, and that is deliberate. The
issuer is a string inside a token: a browser follows it, and the gateway compares it against the
`iss` claim. The JWKS URL is an address the gateway dials, and the gateway's address guard refuses
loopback. A browser resolves `dex.localhost` to loopback by rule, and Docker resolves it to the
container. So the dialed name is the plain service name `dex`, which resolves to a private address
under every resolver and needs no rule about `.localhost` to be true.

## Why two pieces is the floor

Signing a token and running a browser flow are different jobs.

Dex signs an ID token and serves a JWKS. It proxies nothing, so it cannot put a header on a
request that travels to the gateway. oauth2-proxy runs the flow and sets that header, and it signs
nothing. Neither program does the other one's job.

Authelia does both jobs in one program, and its forward-auth mode asserts plain headers. A plain
header carries no signature, so `assertionFormat: "jwt"` needs a proxy in front of Authelia as
well. The count stays at two, whichever product you choose.

A reader who counts three containers deserves the reason. The example is not padded.

## Add a person

One command generates the hash:

```bash
docker run --rm -i httpd:2.4-alpine htpasswd -niBC 10 "" | tr -d ':\n'
```

Type the password and press Enter. The command prints one bcrypt hash at cost 10. The password
reaches the program on standard input, so it enters no shell history and appears in no process
list. Your terminal does echo it while you type, because `-i` reads a stream rather than a prompt.

Then three edits and one restart:

1. `dex.yaml`: one more entry under `staticPasswords`, with the hash from the command.
2. `oauth2-proxy.emails`: one more line.
3. `config.json`: one more entry in `allowedIdentities`, and one more entry in `gates.approvers`
   if that person may answer an approval.
4. `docker compose restart dex nanoinfra-gateway`. oauth2-proxy watches its allowlist file, and it
   says so in its log, so that one needs no restart.

## What this does not give

**No screen for accounts.** Adding a person is a hash, an edit and a restart, as above. There is
no password reset, no invitation, and no lockout after a bad password.

A deployment that wants accounts from a user interface runs Authentik, Zitadel or Keycloak. The
three values in the table above point at any of them, and nothing in this repository changes.

## What this repository will not build, and why

**An authentication server of its own.**

The idea returns in a friendly shape: a small login server, with a simple user list, beside the
agent rather than inside it. The location was never the problem. Whoever ships that server owns
password hashing, session handling and token signing, and owns all three for as long as the
project lives.

Dex is already that program, and it is already reviewed. Its user list is a config file, which is
the whole feature the idea reaches for. So the deliverable here is a compose file, and the
authentication code in this repository stays at zero.

## Run it

Write `examples/auth/.env` first. The placeholder cookie secret is the wrong length on purpose, so
the proxy stops until a real one is there.

```bash
cd examples/auth
docker compose up -d --build
```

The build is the repository's own `Dockerfile`, so the first start is slow and the two other
services start at once.

Open `http://127.0.0.1:4180/`. oauth2-proxy sends the browser to Dex, Dex asks for the password,
and the WebUI opens behind the proxy. Add `127.0.0.1 dex.localhost` to `/etc/hosts` if your
browser does not resolve a `.localhost` name by itself.

```bash
docker compose logs -f oauth2-proxy nanoinfra-gateway
docker compose down -v
```

The `-v` removes the gateway's named volume, and with it the sessions and the audit log of the
example.

## The files

| File | What it holds |
|---|---|
| `compose.yaml` | Three services, one network with fixed addresses, and every published port on loopback. |
| `dex.yaml` | The issuer, the client, and the user list as bcrypt hashes. |
| `oauth2-proxy.cfg` | The redirect address, the cookie, and the allowlist file. |
| `oauth2-proxy.alpha.yaml` | The listen address, the upstream, the provider, and the header that carries the ID token. |
| `oauth2-proxy.emails` | The people who reach the agent at all. |
| `config.json` | The gateway: what it verifies, which identity it reads, and whose approval counts. |

oauth2-proxy reads two files, and that is not a preference. It refuses an option in one file that
the other file replaces, and only the second file can name a request header of our own choice. Its
own log calls that format alpha, so a version bump of the image needs a read of that file against
the new version.

`config.json` is JSON, so it carries no comment. Each key it sets:

| Key | Why it is there |
|---|---|
| `channels.websocket.host: "0.0.0.0"` | oauth2-proxy runs in another container, and a container's own loopback answers nothing from outside it. Compose publishes this port nowhere, so the network is the only way in. |
| `channels.websocket.publicWsUrl` | The browser reaches the gateway through the proxy, so the WebUI must open its WebSocket at the proxy address rather than at the address the gateway sees. |
| `trustedProxyAuth.trustedPeerCidrs` | One address, and it is oauth2-proxy. The subnet in `compose.yaml` is fixed for this reason. Change one, and change the other. |
| `trustedProxyAuth.assertionHeader` | The header oauth2-proxy sets. It is not `Authorization`, because the gateway's routes already read an API token there. |
| `trustedProxyAuth.assertionFormat: "jwt"` | The assertion is a signed token, and the gateway verifies the signature. `plain` would trust a bare string, and a bare string carries no signature. |
| `trustedProxyAuth.issuer`, `audience`, `jwksUrl` | The three values above. A token from another realm, or for another application of the same realm, is not an authentication for this gateway. |
| `trustedProxyAuth.identityClaim: "email"` | The claim that names the person. An operator reads `gates.approvers` in git and has to know who it names, so the readable claim is the right default. |
| `trustedProxyAuth.allowedIdentities` | Who may enter. A verified token is not an invitation. |
| `gates.approvers` | Whose approval counts, as `webui:<the email claim>`. |
| `gates.approvalPaths` | Which paths authenticate an approver. |

## One approval end to end

`gates.approvers` names one person, `webui:alberto@example.com`. The `webui:` prefix is the path
that authenticated the person, and the rest is the `email` claim out of the token. So an audit
record names the person. A deployment with a shared token records the string `webui` instead, and
two operators behind one token are one credential.

Path independence refuses an approval that arrives on the same path as the request. This example
configures one path, `webui`, so the request you approve must start somewhere else: a cron job,
the heartbeat, or a chat channel. That is not a gap in the example. It is the rule that stops one
compromised account from holding both halves of an approval.

Read the record:

```bash
docker compose exec nanoinfra-gateway \
  sh -c 'cat /home/nanoinfra/.nanoinfra/gates/gate-*.jsonl'
```

## The test that keeps this example honest

`tests/test_auth_example.py` reads these files. It does not run them. It asserts that no service
publishes a port outside loopback, that `allowAnyVerifiedIdentity` is absent or false, that
`allowedIdentities` names somebody, that the oauth2-proxy allowlist is not `*`, and that every
secret is a recognisable placeholder.

A reader can break this example silently. An edit that opens it fails that test, so a copy of
these files cannot become an open agent by accident.
