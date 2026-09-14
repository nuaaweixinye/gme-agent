const { app, BrowserWindow, dialog, ipcMain, shell } = require("electron");
const { spawn, spawnSync } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const BACKEND_HOST = "127.0.0.1";
const BACKEND_PORT = Number(process.env.GME_AGENT_BACKEND_PORT || "8765");
const BACKEND_URL = `http://${BACKEND_HOST}:${BACKEND_PORT}`;
const CONDA_ENV = process.env.GME_AGENT_CONDA_ENV || "";
const API_TOKEN = process.env.GME_AGENT_API_TOKEN || crypto.randomBytes(32).toString("hex");
process.env.GME_AGENT_API_TOKEN = API_TOKEN;

let backendProcess = null;
let backendProcessPid = 0;
let backendStartedByApp = false;
let backendStopped = false;

function isPackaged() {
  return app.isPackaged;
}

function resourcePath(...parts) {
  if (isPackaged()) {
    return path.join(process.resourcesPath, ...parts);
  }
  return path.resolve(__dirname, "..", "..", ...parts);
}

function packagedBackendExecutable() {
  const candidates = [
    process.env.GME_AGENT_BACKEND_EXE,
    resourcePath("backend-runtime", "gme-agent-backend", "gme-agent-backend.exe"),
    resourcePath("backend-runtime", "gme-agent-backend.exe"),
  ].filter(Boolean);
  return candidates.find((candidate) => fs.existsSync(candidate)) || "";
}

function userConfigPath() {
  if (!isPackaged()) {
    return resourcePath("config.local.json");
  }

  const configPath = path.join(app.getPath("userData"), "config.local.json");
  if (!fs.existsSync(configPath)) {
    const example = resourcePath("config.example.json");
    if (fs.existsSync(example)) {
      fs.copyFileSync(example, configPath);
      writePackagedDefaultConfig(configPath);
    }
  }
  return configPath;
}

function writePackagedDefaultConfig(configPath) {
  try {
    const data = JSON.parse(fs.readFileSync(configPath, "utf8"));
    const userData = app.getPath("userData");
    data.worktree_root = path.join(userData, "worktrees");
    data.artifact_root = path.join(userData, "artifacts");
    data.database_path = path.join(userData, "gme_agent.db");
    fs.writeFileSync(configPath, `${JSON.stringify(data, null, 2)}\n`, "utf8");
  } catch (error) {
    console.error(`Failed to rewrite packaged default config: ${error.message}`);
  }
}

function commandExists(command, args = ["--version"]) {
  return new Promise((resolve) => {
    const child = spawn(command, args, { windowsHide: true, stdio: "ignore" });
    child.on("error", () => resolve(false));
    child.on("exit", (code) => resolve(code === 0));
  });
}

async function findCondaCommand() {
  if (process.env.GME_AGENT_CONDA) return process.env.GME_AGENT_CONDA;
  const candidates = [
    process.env.CONDA_EXE,
    "D:\\anaconda\\Scripts\\conda.exe",
    "C:\\ProgramData\\anaconda3\\Scripts\\conda.exe",
    "C:\\ProgramData\\miniconda3\\Scripts\\conda.exe",
    "conda",
  ].filter(Boolean);

  for (const candidate of candidates) {
    if (path.isAbsolute(candidate) && fs.existsSync(candidate)) return candidate;
    if (!path.isAbsolute(candidate) && (await commandExists(candidate))) return candidate;
  }
  return "";
}

async function backendHealth() {
  return new Promise((resolve) => {
    const req = http.get(
      `${BACKEND_URL}/api/health`,
      {
        timeout: 1200,
        headers: { Authorization: `Bearer ${API_TOKEN}` },
      },
      (res) => {
        let body = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => { body += chunk; });
        res.on("end", () => {
          try {
            const data = JSON.parse(body || "{}");
            resolve(res.statusCode === 200 && data.authenticated === true);
          } catch {
            resolve(false);
          }
        });
      },
    );
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
    req.on("error", () => resolve(false));
  });
}

function waitForBackend(timeoutMs = 20000) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    const tick = async () => {
      if (await backendHealth()) {
        resolve();
        return;
      }
      if (Date.now() - start > timeoutMs) {
        reject(new Error(`Backend startup timed out: ${BACKEND_URL}`));
        return;
      }
      setTimeout(tick, 350);
    };
    tick();
  });
}

function logFile(name) {
  const dir = path.join(app.getPath("userData"), "logs");
  fs.mkdirSync(dir, { recursive: true });
  return fs.openSync(path.join(dir, name), "a");
}

async function startBackend() {
  if (await backendHealth()) return;

  const backendScript = resourcePath("backend", "run_backend.py");
  const configPath = userConfigPath();
  const backendArgs = ["--config", configPath, "--host", BACKEND_HOST, "--port", String(BACKEND_PORT)];
  const backendExe = packagedBackendExecutable();
  let command = process.env.GME_AGENT_PYTHON || "";
  let commandArgs = [backendScript, ...backendArgs];
  let cwd = path.dirname(backendScript);

  if (backendExe) {
    command = backendExe;
    commandArgs = backendArgs;
    cwd = path.dirname(backendExe);
  } else if (!command && CONDA_ENV) {
    const conda = await findCondaCommand();
    if (conda) {
      command = conda;
      commandArgs = ["run", "-n", CONDA_ENV, "python", backendScript, ...backendArgs];
    }
  }
  if (!command) command = "python";

  backendProcess = spawn(command, commandArgs, {
    cwd,
    windowsHide: true,
    stdio: ["ignore", logFile("backend.out.log"), logFile("backend.err.log")],
    env: {
      ...process.env,
      GME_AGENT_RESOURCE_ROOT: resourcePath(),
    },
  });
  backendProcessPid = backendProcess.pid || 0;
  backendStartedByApp = true;

  backendProcess.on("exit", () => {
    backendProcess = null;
  });

  await waitForBackend();
}

async function createWindow() {
  ipcMain.handle("gme-agent:select-directory", async (_event, currentPath) => {
    const result = await dialog.showOpenDialog({
      defaultPath: currentPath && fs.existsSync(currentPath) ? currentPath : undefined,
      properties: ["openDirectory", "createDirectory"],
    });
    return result.canceled ? "" : result.filePaths[0] || "";
  });

  ipcMain.handle("gme-agent:select-file", async (_event, currentPath, filters) => {
    const result = await dialog.showOpenDialog({
      defaultPath: currentPath && fs.existsSync(currentPath) ? currentPath : undefined,
      filters: Array.isArray(filters) && filters.length ? filters : undefined,
      properties: ["openFile"],
    });
    return result.canceled ? "" : result.filePaths[0] || "";
  });

  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1080,
    minHeight: 720,
    title: "GME Test Agent",
    backgroundColor: "#edf2f7",
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.cjs"),
      sandbox: false,
    },
  });

  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  await startBackend();
  win.on("close", () => {
    stopBackend();
  });
  await win.loadFile(path.join(__dirname, "..", "dist", "index.html"));
}

app.whenReady().then(createWindow);

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  stopBackend();
});

function stopBackend() {
  if (!backendStartedByApp || backendStopped) return;
  backendStopped = true;
  if (process.platform === "win32") {
    if (backendProcessPid) {
      spawnSync("taskkill", ["/pid", String(backendProcessPid), "/t", "/f"], {
        windowsHide: true,
        stdio: "ignore",
      });
    }

    const script = [
      `$connections = Get-NetTCPConnection -LocalPort ${BACKEND_PORT} -State Listen -ErrorAction SilentlyContinue`,
      "foreach ($connection in $connections) {",
      "  $process = Get-CimInstance Win32_Process -Filter \"ProcessId = $($connection.OwningProcess)\"",
      "  if ($process.CommandLine -match 'run_backend\\.py|gme-agent-backend\\.exe') { Stop-Process -Id $process.ProcessId -Force }",
      "}",
    ].join("; ");
    spawnSync("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], {
      windowsHide: true,
      stdio: "ignore",
    });
  } else if (backendProcess && !backendProcess.killed) {
    backendProcess.kill("SIGTERM");
  }
  backendProcessPid = 0;
}
