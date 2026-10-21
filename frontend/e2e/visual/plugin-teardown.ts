import { spawnSync } from "node:child_process";

/**
 * Removes the Playwright browser container even when its `docker run` client was killed before
 * it could stop it (Playwright ends web servers with SIGKILL after the grace period).
 */
export default function teardown(): void {
  if (process.env.AUTOAIR_VISUAL_BROWSER === "local") return;
  const port = process.env.AUTOAIR_VISUAL_PW_PORT ?? "8855";
  spawnSync("docker", ["rm", "-f", `autoair-visual-pw-${port}`], { stdio: "ignore" });
}
