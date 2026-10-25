// Publish a TeleFi envelope from Node (no TeleFi install needed): write one JSON file into the
// spool's out/ folder atomically (temp file, then rename). Run: node examples/publish.mjs
import { mkdirSync, renameSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";

const spool = process.env.TELEFI_SPOOL || join(process.env.TELEFI_HOME || join(homedir(), ".telefi"), "spool");
const envelope = {
  schema: "telefi.envelope.v1", id: `node-${randomUUID().slice(0, 12)}`, source: "node", kind: "info", severity: "info",
  title: "hello from Node", insight: "Any language can publish: it is just a JSON file.", created_at: new Date().toISOString(),
};
const out = join(spool, "out");
mkdirSync(out, { recursive: true });
writeFileSync(join(out, `.${envelope.id}.tmp`), JSON.stringify(envelope));
renameSync(join(out, `.${envelope.id}.tmp`), join(out, `${envelope.id}.json`));
console.log(`published ${envelope.id} -> ${out}`);
