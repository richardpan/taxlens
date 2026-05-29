// TaxLens Electron shell.
//
// Launches the local FastAPI sidecar (`taxlens serve --no-open --port 8765`)
// as a child process, waits for the HTTP endpoint to come up, then loads the
// web UI into a BrowserWindow. The Python `taxlens` CLI must be on PATH —
// v1 keeps things simple by not bundling the Python interpreter.

const { app, BrowserWindow, Menu, dialog, shell } = require("electron");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const PORT = parseInt(process.env.TAXLENS_PORT || "8765", 10);
const URL = `http://127.0.0.1:${PORT}/`;
let backend = null;
let win = null;

function pingOnce() {
  return new Promise((resolve) => {
    const req = http.get(URL, (res) => {
      res.resume();
      resolve(res.statusCode && res.statusCode < 500);
    });
    req.on("error", () => resolve(false));
    req.setTimeout(1000, () => { req.destroy(); resolve(false); });
  });
}

async function waitForBackend(timeoutMs = 15000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (await pingOnce()) return true;
    await new Promise((r) => setTimeout(r, 250));
  }
  return false;
}

function startBackend() {
  // Prefer the PyInstaller-bundled backend (no Python install required); fall
  // back to a globally-installed `taxlens` CLI on PATH (dev mode).
  const path = require("path");
  const fs = require("fs");
  const bundledName = process.platform === "win32" ? "taxlens-backend.exe" : "taxlens-backend";
  const bundledCandidates = [
    path.join(process.resourcesPath || "", "bin", bundledName),
    path.join(__dirname, "bin", bundledName),
  ];
  const bundled = bundledCandidates.find(p => p && fs.existsSync(p));
  const cmd = bundled || (process.platform === "win32" ? "taxlens.exe" : "taxlens");
  const args = ["serve", "--no-open", "--port", String(PORT)];
  backend = spawn(cmd, args, { stdio: "inherit", shell: false });
  backend.on("error", (err) => {
    dialog.showErrorBox(
      "TaxLens backend failed to start",
      `Could not launch \`${cmd}\`.\n\n` +
      "If you are running from source, make sure the TaxLens Python CLI is " +
      "installed and on your PATH (e.g. `pip install -e .` in the project root). " +
      "Production builds should include desktop/bin/" + bundledName + ".\n\n" +
      `Underlying error: ${err.message}`
    );
    app.quit();
  });
  backend.on("exit", (code) => {
    if (code !== 0 && code !== null) {
      console.error(`taxlens backend exited with code ${code}`);
    }
    backend = null;
  });
}

function stopBackend() {
  if (backend && !backend.killed) {
    try { backend.kill(); } catch (_) {}
  }
}

async function createWindow() {
  win = new BrowserWindow({
    width: 1280,
    height: 860,
    title: "TaxLens",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.js"),
    },
  });

  // Open external links in the user's default browser.
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  const ok = await waitForBackend();
  if (!ok) {
    dialog.showErrorBox(
      "TaxLens backend did not respond",
      `No HTTP response at ${URL} after 15s. Is another taxlens server already running?`
    );
    app.quit();
    return;
  }
  await win.loadURL(URL);
}

app.whenReady().then(() => {
  startBackend();
  createWindow();
  Menu.setApplicationMenu(null);
});

app.on("window-all-closed", () => {
  stopBackend();
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", stopBackend);
