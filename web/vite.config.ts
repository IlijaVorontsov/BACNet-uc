/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The hub serves the API on 127.0.0.1:8080 by default (hub.yaml `listen`).
const hub = process.env.UC_HUB_URL ?? "http://127.0.0.1:8080";
const proxy = { "/api": { target: hub, changeOrigin: false } };

export default defineConfig({
  plugins: [react()],
  server: { proxy },
  preview: { proxy },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "es2022",
    sourcemap: true,
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["src/test-setup.ts"],
    restoreMocks: true,
  },
});
