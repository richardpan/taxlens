# TaxLens Desktop (Electron shell)

Wraps the local TaxLens Python sidecar in a native window so it feels like
a regular app instead of a browser tab.

## Requirements

- Node.js 18+
- The `taxlens` Python CLI on your `PATH` (e.g. `pip install -e ..` from this
  folder, or `pipx install taxlens` once it's published).

## Run in dev

```sh
cd desktop
npm install
npm start
```

This spawns `taxlens serve --no-open --port 8765` as a child process and
loads the UI in an Electron window. Closing the window kills the backend.

## Build a distributable

```sh
npm run dist
```

Outputs unsigned installers under `dist/`. Code-signing certs are a v1.x
follow-up.

## Notes

- The Python interpreter is **not** bundled in v0.2. We require `taxlens` on
  PATH so the binary stays tiny. v1.x will optionally bundle via PyInstaller.
- The renderer has `nodeIntegration: false` and `contextIsolation: true`; it
  only ever talks to `127.0.0.1:8765` over HTTP, identical to the browser flow.
