# How to Deploy a Long-Running nanoinfra AI Agent Gateway

The nanoinfra gateway is the long-running self-hosted AI agent process that keeps
WebUI sessions, chat apps, automations, local triggers, heartbeat jobs, Dream,
and WebSocket delivery online.

## What you will build

- a verified nanoinfra config
- a gateway process
- a service or container deployment path with Docker, systemd, or macOS
  LaunchAgent

## When to use this

Use this when nanoinfra should keep running after a single CLI turn. Chat apps,
browser sessions, background automations, local triggers, and server-side
integrations all depend on a live gateway.

## Install

```bash
python -m pip install nanoinfra
nanoinfra onboard --wizard
nanoinfra status
nanoinfra agent -m "Hello!"
```

## Minimal working example

Run the gateway in the foreground:

```bash
nanoinfra gateway
```

For WebUI background usage:

```bash
nanoinfra webui --background
nanoinfra gateway status
nanoinfra gateway logs
```

## Production notes

- Docker Compose is the most repeatable Linux container path.
- systemd user services are useful for Linux user-level gateway deployments.
- macOS LaunchAgent keeps the gateway alive after login.
- Persist config, workspace, sessions, memory files, channel login state, and
  generated artifacts.
- Restart the gateway after editing `config.json`.

## Security notes

- Plan ports before exposing services. Gateway health defaults to `18790`,
  WebUI/WebSocket defaults to `8765`, and `nanoinfra serve` defaults to `8900`.
- Bind externally only when you have configured tokens or API keys.
- Keep chat access control intentional before deploying.
- Use Docker or Linux sandboxing when shell tools are enabled for unattended
  work.

## Troubleshooting

- Use the same `--config` and `--workspace` flags for status checks and service
  startup.
- Check logs with `docker compose logs`, `journalctl`, LaunchAgent logs, or
  `nanoinfra gateway --verbose`.
- If Docker port publishing does not work, confirm the service is not bound only
  to container loopback.

## Related nanoinfra docs

- [Deployment](../deployment.md)
- [Multiple Instances](../multiple-instances.md)
- [Configuration](../configuration.md)
