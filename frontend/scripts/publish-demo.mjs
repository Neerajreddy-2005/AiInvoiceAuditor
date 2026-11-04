// Runs once each time a public demo starts: asks the tunnel for its new address,
// then publishes the already-built site with that address. Nothing is saved.
import { spawnSync } from "node:child_process";
import { setTimeout as sleep } from "node:timers/promises";

const { NETLIFY_AUTH_TOKEN: token, NETLIFY_SITE_ID: site } = process.env;
if (!token || !site) {
  console.log("Not publishing: NETLIFY_AUTH_TOKEN and NETLIFY_SITE_ID aren't both set in .env.");
  process.exit(0);
}

async function tunnelHost() {
  for (let attempt = 0; attempt < 60; attempt++) {
    try {
      const { hostname } = await (await fetch("http://tunnel:20241/quicktunnel")).json();
      if (hostname) return hostname;
    } catch {
      // The tunnel is still starting.
    }
    await sleep(2000);
  }
  throw new Error("The tunnel gave no address.");
}

function run(command, args, env = {}) {
  return spawnSync(command, args, { stdio: "inherit", env: { ...process.env, ...env } }).status === 0;
}

const backend = `https://${await tunnelHost()}`;
if (!run("node", ["scripts/write-redirects.mjs"], { BACKEND_URL: backend })) process.exit(1);

for (let attempt = 0; attempt < 3; attempt++) {
  if (run("netlify", ["deploy", "--no-build", "--prod", "--dir=dist", `--site=${site}`])) process.exit(0);
  await sleep(10000);
}
console.error("Publishing failed: the site can't reach the app until the project is started again.");
process.exit(1);
