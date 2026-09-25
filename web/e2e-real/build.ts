import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

/** The hubs serve dist/: build it once per run, so no test sees a stale build. */
export default function build(): void {
  execFileSync("pnpm", ["exec", "vite", "build", "--logLevel", "warn"], {
    cwd: fileURLToPath(new URL("..", import.meta.url)),
    stdio: "inherit",
  });
}
