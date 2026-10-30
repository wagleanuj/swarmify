// Runs one connector process or install step inside Anthropic's sandbox-runtime (ADR 0021):
//
//   node launcher.mjs <plan.json> -- <command> [args...]
//
// The plan is what Swarmify wrote for this process (swarmify_connectors/sandbox.py): `sandbox`,
// the sandbox-runtime config; `cwd`, the folder it runs in; `env`, variables set for the command
// (its own HOME and TMPDIR); `pass`, the names of the variables of ours it may have (its own
// Setup values), besides a few every process needs; `blocked`, the file each attempt the sandbox
// blocks is appended to as one JSON line, for Swarmify to log; `observe`, on Linux, the read
// tripwire the command runs under (observe.py), whose reports arrive on stderr after `nonce`;
// and `session`, true for an MCP server over stdio.
//
// The command's stdin and stdout are relayed and its exit code is ours. With `session` the
// launcher also ends once its stdin and the command's stdout have both closed: the MCP session is
// over, and a process the command left running for a long job stays in its sandbox, without
// network, until it ends. The command runs in a process group of its own; a signal to the
// launcher stops that whole group.

import { execFileSync, spawn } from "node:child_process";
import { appendFileSync, mkdtempSync, readFileSync, realpathSync, rmSync } from "node:fs";
import { createInterface } from "node:readline";
import { SandboxManager } from "@anthropic-ai/sandbox-runtime";

// macOS reports a Seatbelt denial through its log a beat after it happens.
const LATE_MS = process.platform === "darwin" ? 250 : 50;

const [planPath, dashes, ...argv] = process.argv.slice(2);
if (!planPath || dashes !== "--" || argv.length === 0) {
  console.error("usage: launcher.mjs <plan.json> -- <command> [args...]");
  process.exit(2);
}
const plan = JSON.parse(readFileSync(planPath, "utf8"));
process.chdir(plan.cwd);
// sandbox-runtime hands the command this TMPDIR, its own scratch folder, instead of /tmp/claude,
// which every sandboxed process shares.
process.env.CLAUDE_CODE_TMPDIR = plan.env.TMPDIR;
// On Linux the proxy reaches the sandbox through sockets sandbox-runtime makes in our temp folder
// and binds in before it hides the denied folders, /tmp among them: a short folder of our own
// there, granted again, keeps them in sight (and under the 108 bytes a socket path may have).
let sockets;
if (process.platform === "linux") {
  sockets = mkdtempSync("/tmp/swarmify-sandbox-");
  process.env.TMPDIR = sockets;
  plan.sandbox.filesystem.allowRead.push(sockets);
}
// What the MCP SDK passes a stdio server by default; HOME and TMPDIR come from the plan.
const BASE = ["LOGNAME", "PATH", "SHELL", "TERM", "USER", "LANG", "LC_ALL"];
// On macOS, Chromium (a render's browser) needs Mach services, IOKit and sysctls that
// sandbox-runtime's deny-default Seatbelt profile refuses, and keeps its shared memory in the
// user's temp folder whatever TMPDIR says. A command granted its own loopback (as a browser is)
// gets those: Seatbelt still holds its files, writes and network as granted.
const CHROMIUM = [
  "(allow mach-lookup)",
  "(allow mach-register)",
  "(allow iokit-open)",
  "(allow system-socket)",
  "(allow sysctl-read)",
  '(deny mach-lookup (global-name-prefix "com.apple.pasteboard"))', // never the clipboard
].join("\n");
const browser = process.platform === "darwin" && plan.sandbox.network.allowLocalBinding;
if (browser) {
  const temp = realpathSync(execFileSync("getconf", ["DARWIN_USER_TEMP_DIR"]).toString().trim());
  const own = [`${temp}/.org.chromium.Chromium.*`, `${temp}/.org.chromium.Chromium.*/**`];
  plan.sandbox.filesystem.allowRead.push(...own);
  plan.sandbox.filesystem.allowWrite.push(...own);
}
const quote = (s) => `'${s.replaceAll("'", "'\\''")}'`;
const blocked = (line) => appendFileSync(plan.blocked, JSON.stringify({ line }) + "\n");

let child;
let seatbelt; // macOS's log, followed for the sandbox's denials
let ending = false;
async function end(code) {
  if (ending) return;
  ending = true;
  await new Promise((r) => setTimeout(r, LATE_MS));
  seatbelt?.kill();
  if (sockets) rmSync(sockets, { recursive: true, force: true });
  process.exit(code);
}

function stop(signal) {
  try {
    if (child?.pid) process.kill(-child.pid, signal); // the sandbox's whole process group
  } catch {
    // gone already
  }
  void end(1);
}

/** Seatbelt's denials of the profile tagged `tag`, from macOS's log, every one: sandbox-runtime's
 * own monitor keeps only the first of the lines that arrive together. Resolves once listening. */
function watchSeatbelt(tag) {
  const log = spawn("log", ["stream", "--style", "compact", "--predicate", `eventMessage CONTAINS "${tag}"`]);
  seatbelt = log;
  return new Promise((ready) => {
    const lines = createInterface({ input: log.stdout, crlfDelay: Infinity });
    lines.on("line", (line) => {
      if (line.startsWith("Filtering the log data")) ready();
      const said = /Sandbox:\s+(.+\bdeny\(\d+\).+)$/.exec(line)?.[1];
      if (said) blocked(said);
    });
    log.on("error", () => ready());
    setTimeout(ready, 3000).unref();
  });
}

try {
  // sandbox-runtime's own log monitors stay off: on macOS it drops denials that arrive together
  // (watchSeatbelt reads them all); on Linux it would hold the one seccomp listener a process may
  // have, which observe.py needs. Proxy denials are reported either way.
  await SandboxManager.initialize(plan.sandbox, undefined, false);
  // The store calls back once on subscribing, then once per violation, the newest last.
  let subscribed = false;
  SandboxManager.getSandboxViolationStore().subscribe((all) => {
    if (subscribed && all.length) blocked(all.at(-1).line);
    subscribed = true;
  });
  // The proxy decides every connection: sandbox-runtime exempts localhost and private networks
  // from it, which inside the sandbox reach nothing, so a declared Service there would not work.
  // A command allowed its own loopback (a render's browser and server) keeps that one exemption.
  const own = "localhost,127.0.0.1,::1";
  const proxied = plan.sandbox.network.allowLocalBinding
    ? [`NO_PROXY=${own}`, `no_proxy=${own}`]
    : ["-u", "NO_PROXY", "-u", "no_proxy"];
  const command = ["env", ...proxied, ...(plan.observe ?? []), ...argv];
  let wrapped = await SandboxManager.wrapWithSandbox(command.map(quote).join(" "));
  // at the profile's end, where Seatbelt's later rules win
  if (browser) wrapped = wrapped.replace(/' \/bin\/(ba|z)?sh -c /, (rest) => `\n${CHROMIUM}${rest}`);
  const tag = /\(with message "([^"]+)"\)/.exec(wrapped)?.[1];
  if (process.platform === "darwin" && tag) await watchSeatbelt(tag);
  // `; exit` keeps the shell as the sandbox's parent (bubblewrap dies with its parent), and
  // `detached` puts it in a process group of its own, out of reach of a kill meant for us.
  child = spawn("/bin/sh", ["-c", `${wrapped}; exit $?`], {
    detached: true,
    env: {
      ...Object.fromEntries([...BASE, ...plan.pass].filter((k) => k in process.env).map((k) => [k, process.env[k]])),
      ...plan.env,
    },
    stdio: ["pipe", "pipe", plan.observe ? "pipe" : "inherit"],
  });
} catch (error) {
  console.error(`the connector sandbox did not start: ${error instanceof Error ? error.message : error}`);
  process.exit(1);
}

process.on("SIGTERM", () => stop("SIGTERM"));
process.on("SIGINT", () => stop("SIGINT"));
child.on("error", (error) => {
  console.error(`the sandboxed command did not start: ${error.message}`);
  void end(1);
});
child.on("exit", (code, signal) => {
  SandboxManager.cleanupAfterCommand();
  void end(code ?? (signal ? 1 : 0));
});
child.stdin.on("error", () => {}); // the command may close its stdin first
process.stdin.pipe(child.stdin);
child.stdout.pipe(process.stdout);
if (plan.observe) {
  const mark = `\x1fswarmify-blocked ${plan.nonce} `;
  createInterface({ input: child.stderr, crlfDelay: Infinity }).on("line", (line) => {
    if (line.startsWith(mark)) blocked(line.slice(mark.length));
    else process.stderr.write(line + "\n");
  });
}

let stdinDone = false;
let stdoutDone = false;
const sessionOver = () => plan.session && stdinDone && stdoutDone && void end(0);
process.stdin.on("end", () => {
  stdinDone = true;
  sessionOver();
});
child.stdout.on("end", () => {
  stdoutDone = true;
  sessionOver();
});
