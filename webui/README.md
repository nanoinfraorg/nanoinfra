# nanobot webui

The browser front-end for `nanobot web`. Built with Vite + React 18 +
TypeScript + Tailwind 3 + shadcn/ui. Talks to the gateway over the WebSocket
multiplex protocol; session metadata comes from the embedded REST surface on
the same port.

## Layout

```
webui/                 source tree (this directory)
nanobot/web/dist/      build output, shipped in the Python wheel
```

## Develop

```bash
cd webui
bun install            # npm install also works
bun run dev            # http://127.0.0.1:5173 (proxies /api /webui /auth -> 8765)
```

In a separate shell, start the gateway with the WebSocket channel:

```bash
uv run nanobot gateway        # or `nanobot web` once you've built once
```

If the gateway listens on a non-default port, point the dev server at it:

```bash
NANOBOT_API_URL=http://127.0.0.1:9000 bun run dev
```

## Build

```bash
bun run build   # writes ../nanobot/web/dist (consumed by `nanobot web`)
```

## Test

```bash
bun run test    # vitest, jsdom-style happy-dom environment
```
