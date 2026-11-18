// Runs once each time a public demo starts: waits until the Tailscale Funnel
// address answers, then publishes the already-built site pointing at it. The
// address is fixed, so this republish is the same each time. Nothing is saved.
import { spawnSync } from "node:child_process";
import { setTimeout as sleep } from "node:timers/promises";

const { NETLIFY_AUTH_TOKEN: token, NETLIFY_SITE_ID: site, DEMO_BACKEND_URL: address } = process.env;
if (!token || !site) {
  console.log("Not publishing: NETLIFY_AUTH_TOKEN and NETLIFY_SITE_ID aren't both set in .env.");
  process.exit(0);
}
if (!/^https:\/\/[a-z0-9-]+\.[a-z0-9-]+\.ts\.net$/.test(address ?? "")) {
  console.error("DEMO_BACKEND_URL must be the Funnel address, like https://invoice-auditor.tail1234.ts.net");
  process.exit(1);
}

async function waitUntilReachable() {
  // A new Funnel address can take a minute to get its certificate and DNS.
  for (let attempt = 0; attempt < 60; attempt++) {
    try {
      if ((await fetch(`${address}/health`)).ok) return;
    } catch {
      // Still starting.
    }
    await sleep(5000);
  }
  console.log("The Funnel address isn't answering yet; publishing anyway.");
}

function run(command, args, env = {}) {
  return spawnSync(command, args, { stdio: "inherit", env: { ...process.env, ...env } }).status === 0;
}

await waitUntilReachable();
const backend = address;
if (!run("node", ["scripts/write-redirects.mjs"], { BACKEND_URL: backend })) process.exit(1);

for (let attempt = 0; attempt < 3; attempt++) {
  if (run("netlify", ["deploy", "--no-build", "--prod", "--dir=dist", `--site=${site}`])) process.exit(0);
  await sleep(10000);
}
console.error("Publishing failed: the site can't reach the app until the project is started again.");
process.exit(1);
