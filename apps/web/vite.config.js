import fs from "node:fs";
import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

function loadRootEnvApiPort() {
  const rootEnvPath = path.resolve(__dirname, "../../.env");
  if (!fs.existsSync(rootEnvPath)) {
    return "";
  }
  const raw = fs.readFileSync(rootEnvPath, "utf8");
  for (const line of raw.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#") || !trimmed.includes("=")) {
      continue;
    }
    const [key, value] = trimmed.split("=", 2);
    if (key.trim() === "API_PORT") {
      return value.trim().replace(/^['"]|['"]$/g, "");
    }
  }
  return "";
}

const apiPort = process.env.API_PORT || loadRootEnvApiPort() || "8001";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: `http://localhost:${apiPort}`,
        changeOrigin: true
      }
    }
  }
});
