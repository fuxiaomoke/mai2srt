// Dev launcher: starts Vite and (when idle) the mai2srt backend together,
// so one command brings up the whole app.
//
// NOTE: everything here is spawned via `process.execPath` (the real node)
// and NEVER via pnpm -- under the DSH host, `pnpm` on PATH is a shim whose
// node is the host Electron binary (execPath contains spaces, which breaks
// spawn argument quoting in the tauri CLI chain).
import { spawn } from "node:child_process";
import net from "node:net";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const BACKEND_PORT = 47613;

function portOpen(port, host) {
  return new Promise((resolve) => {
    const s = net.connect({ port, host }, () => {
      s.destroy();
      resolve(true);
    });
    s.on("error", () => resolve(false));
  });
}

// A listening port is not the same as a working API (and uvicorn needs
// ~1.4s measured to bind, longer on a cold start). Vite is ready in well
// under a second, so without this gate the webview could load first, fail
// its initial fetch, and strand the page behind a "backend offline" toast.
async function backendReady(port, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const ac = new AbortController();
    const t = setTimeout(() => ac.abort(), 2000);
    try {
      const r = await fetch(`http://127.0.0.1:${port}/api/system`, { signal: ac.signal });
      if (r.ok) return true;
    } catch {
      /* not up yet */
    } finally {
      clearTimeout(t);
    }
    await new Promise((r) => setTimeout(r, 300));
  }
  return false;
}

async function main() {
  const procs = [];

  // backend: spawn only when the port is free (hot-restart friendliness)
  if (!(await portOpen(BACKEND_PORT, "127.0.0.1"))) {
    procs.push(
      spawn("python", ["-m", "mai2srt.cli", "serve", "--port", String(BACKEND_PORT)], {
        stdio: "inherit",
        cwd: path.resolve(here, ".."),
        shell: true,
      })
    );
  } else {
    console.log(`[dev] backend already listening on :${BACKEND_PORT}`);
  }

  if (await backendReady(BACKEND_PORT)) {
    console.log(`[dev] backend healthy on :${BACKEND_PORT}`);
  } else {
    console.warn(
      `[dev] backend not answering on :${BACKEND_PORT} after 30s; ` +
        "starting vite anyway (the UI will report backend offline)"
    );
  }

  procs.push(
    spawn(process.execPath, [path.join(here, "node_modules", "vite", "bin", "vite.js")], {
      stdio: "inherit",
      cwd: here,
    })
  );

  const bye = () => procs.forEach((p) => {
    try { p.kill(); } catch {}
  });
  process.on("exit", bye);
  process.on("SIGINT", () => process.exit(0));
  process.on("SIGTERM", () => process.exit(0));
}

main();

