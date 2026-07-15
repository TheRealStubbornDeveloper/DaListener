import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { realpathSync } from "node:fs";

export default defineConfig({
  root: realpathSync(process.cwd()),
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8765"
    }
  }
});
