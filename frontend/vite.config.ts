import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Backend dev server, per config.toml [server].port (§3.10).
const BACKEND_ORIGIN = "http://127.0.0.1:8756";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    proxy: {
      "/api": {
        target: BACKEND_ORIGIN,
        changeOrigin: true,
      },
    },
  },
  build: {
    // FastAPI serves this directory as static files in production (§4), so the whole
    // app is one process on one port. Never git-ignored source, only build output.
    outDir: "../src/personal_finance_analyzer/web",
    emptyOutDir: true,
  },
});
