const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("gmeAgent", {
  apiToken: process.env.GME_AGENT_API_TOKEN || "",
  selectDirectory: (currentPath) => ipcRenderer.invoke("gme-agent:select-directory", currentPath || ""),
  selectFile: (currentPath, filters) => ipcRenderer.invoke("gme-agent:select-file", currentPath || "", filters || []),
});
