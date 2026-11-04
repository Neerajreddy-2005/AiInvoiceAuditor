// Comes from the build's environment because a tunnel address changes on every
// restart and doesn't belong in the public repository.
import { writeFileSync } from "node:fs";

const backend = process.env.BACKEND_URL ?? "";
if (!/^https:\/\/[a-z0-9.-]+$/i.test(backend)) {
  console.error("BACKEND_URL must be the backend's address, like https://example.com, with no path.");
  process.exit(1);
}
writeFileSync("dist/_redirects", `/api/* ${backend}/:splat 200!\n`);
