/**
 * Intuition — Pi agent extension.
 *
 * Spawns `intuition rpc` once per session (JSON lines over stdio), registers the
 * memory tools from the tools.json manifest that `intuition install pi` writes
 * beside this file, builds the stable prompt prefix once per session, captures
 * turns, checkpoints on compaction, and asks the Steward for a session-end pass.
 *
 * Children spawned as separate `pi` processes load user-scope extensions too.
 * They are detected through INTUITION_ROLE/INTUITION_AGENT (set by whoever
 * spawns them) and get the read-only tools.
 */

import { spawn, type ChildProcess } from "node:child_process";
import { readFileSync } from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

declare const __dirname: string | undefined;

const CALL_TIMEOUT_MS = 10_000;
const TICK_TIMEOUT_MS = 120_000;

interface Pending {
  resolve: (value: any) => void;
  reject: (reason: any) => void;
  timer: NodeJS.Timeout | undefined;
}

interface ToolSpec {
  name: string;
  description: string;
  parameters: unknown;
}

interface Runtime {
  command: string;
  args: string[];
}

interface Setting {
  key: string;
  value: unknown;
  default: unknown;
  set: boolean;
  overridden: boolean;
  help: string;
  kind: string;
}

interface Route {
  method: string;
  extra?: Record<string, unknown>;
  render?: (result: any) => string;
}

/** Tool name to RPC method, plus any params the host owns and the model must not. */
const ROUTES: Record<string, Route> = {
  memory_search: { method: "search", render: (r) => r.rendered ?? JSON.stringify(r) },
  memory_read: {
    method: "read",
    render: (r) => (r.error ? `error: ${r.error}` : JSON.stringify(r, null, 1)),
  },
  memory_timeline: { method: "timeline" },
  memory_note: { method: "note", extra: { host: "pi", source: "user" } },
  memory_forget: { method: "forget" },
  memory_now: { method: "now" },
  memory_brief: { method: "brief" },
  memory_learn: { method: "learn" },
  memory_secure_get: { method: "secure_get" },
  memory_status: { method: "status" },
};

class IntuitionRpc {
  private proc: ChildProcess | null = null;
  private nextId = 1;
  private pending = new Map<number, Pending>();
  private buffer = "";
  private startError: Error | null = null;
  private spawnFailed = false;
  private lastStderr = "";

  start(): void {
    if (this.proc) return;
    const runtime = loadRuntime();
    this.proc = spawn(runtime.command, runtime.args, {
      env: { ...process.env },
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.proc.stdout!.setEncoding("utf8");
    this.proc.stdout!.on("data", (chunk: string) => this.onData(chunk));
    this.proc.stderr!.on("data", (data: Buffer) => {
      const text = data.toString().trim();
      // the server explains why it refused to start (missing store, bad config),
      // so keep its last line to carry into the notice the user sees
      const last = text.split("\n").map((line) => line.trim()).filter(Boolean).pop();
      if (last) this.lastStderr = last;
      console.error("[intuition]", text);
    });
    this.proc.on("error", (error: Error) => {
      // a failed spawn never emits "exit", so clear the handle or every later
      // call writes into a dead pipe and reports a timeout instead of the cause
      console.error("[intuition] rpc failed to start:", error.message);
      this.startError = error;
      this.spawnFailed = true;
      this.proc = null;
      this.failAll(this.unavailable("rpc"));
    });
    this.proc.on("exit", (code: number | null) => {
      this.proc = null;
      if (code !== 0) {
        this.startError = new Error(this.lastStderr || `intuition rpc exited (${code})`);
      }
      this.failAll(this.unavailable("rpc"));
    });
  }

  private unavailable(method: string): Error {
    if (this.spawnFailed) {
      return new Error(
        `cannot run the intuition CLI (${this.startError?.message}); ` +
        "install it, then run `intuition install pi`");
    }
    if (this.startError) {
      return new Error(this.startError.message);
    }
    return new Error(`rpc timeout: ${method}`);
  }

  private onData(chunk: string): void {
    this.buffer += chunk;
    let newline: number;
    while ((newline = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, newline).trim();
      this.buffer = this.buffer.slice(newline + 1);
      if (!line) continue;
      try {
        const message = JSON.parse(line);
        const pending = this.pending.get(message.id);
        if (!pending) continue;
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(message.error));
        else pending.resolve(message.result);
      } catch {
        console.error("[intuition] unreadable rpc line");
      }
    }
  }

  private failAll(error: Error): void {
    for (const pending of this.pending.values()) pending.reject(error);
    this.pending.clear();
  }

  call(method: string, params: Record<string, unknown> = {},
       timeoutMs: number = CALL_TIMEOUT_MS): Promise<any> {
    this.start();
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const entry: Pending = {
        timer: undefined,
        resolve: (value) => { clearTimeout(entry.timer); resolve(value); },
        reject: (reason) => { clearTimeout(entry.timer); reject(reason); },
      };
      entry.timer = setTimeout(() => {
        if (this.pending.delete(id)) entry.reject(this.unavailable(method));
      }, timeoutMs);
      this.pending.set(id, entry);
      this.proc!.stdin!.write(JSON.stringify({ id, method, params }) + "\n");
    });
  }

  close(): void {
    const proc = this.proc;
    if (!proc) return;
    this.proc = null;
    proc.stdin!.end();
    const kill = setTimeout(() => proc.kill("SIGTERM"), 2_000);
    proc.on("exit", () => clearTimeout(kill));
  }
}

const rpc = new IntuitionRpc();
const isChild = process.env.INTUITION_ROLE === "subagent";
const agentName = process.env.INTUITION_AGENT || (isChild ? "child" : "main");
let prefixSent = false;
let lastPrompt = "";
let settings: Setting[] = [];

const here = typeof __dirname === "string"
  ? __dirname
  : path.dirname(fileURLToPath(import.meta.url));

function loadManifest(): { main: ToolSpec[]; subagent: ToolSpec[] } {
  const manifest = path.join(here, "tools.json");
  try {
    return JSON.parse(readFileSync(manifest, "utf8"));
  } catch (error) {
    throw new Error(
      `intuition: cannot read ${manifest}; reinstall with \`intuition install pi\` (${error})`,
    );
  }
}

/** The RPC command the installer recorded, so the CLI needs no PATH entry. */
function loadRuntime(): Runtime {
  try {
    return JSON.parse(readFileSync(path.join(here, "runtime.json"), "utf8"));
  } catch {
    return { command: "intuition", args: ["rpc"] };
  }
}

function messageText(message: any): string {
  const content = message?.content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => (typeof part === "string" ? part : part?.text ?? ""))
      .join("");
  }
  return "";
}

function checkpointText(preparation: any): string {
  const lines: string[] = [];
  const previous = preparation?.previousSummary;
  if (typeof previous === "string" && previous.trim()) lines.push(previous.trim());
  for (const message of preparation?.messagesToSummarize ?? []) {
    const text = messageText(message).trim();
    if (text) lines.push(`${message?.role ?? "message"}: ${text}`);
  }
  for (const message of preparation?.turnPrefixMessages ?? []) {
    const text = messageText(message).trim();
    if (text) lines.push(`${message?.role ?? "message"}: ${text}`);
  }
  return lines.join("\n").slice(0, 4000);
}

function buildTool(spec: ToolSpec): any {
  const route = ROUTES[spec.name];
  if (!route) throw new Error(`intuition: no rpc route for tool ${spec.name}`);
  return defineTool({
    name: spec.name,
    label: spec.name.replace(/^memory_/, "").replace(/_/g, " "),
    description: spec.description,
    parameters: Type.Unsafe(spec.parameters),
    async execute(_toolCallId: string, params: any) {
      const result = await rpc.call(route.method, { ...params, ...(route.extra ?? {}) });
      const text = route.render ? route.render(result) : JSON.stringify(result, null, 1);
      return { content: [{ type: "text" as const, text }], details: { method: route.method } };
    },
  });
}

function formatValue(value: unknown): string {
  if (typeof value === "string") return value === "" ? '""' : value;
  return String(value);
}

async function loadSettings(): Promise<Setting[]> {
  const result = await rpc.call("config_show");
  settings = Array.isArray(result?.settings) ? result.settings : [];
  return settings;
}

function settingsSummary(rows: Setting[]): string {
  if (!rows.length) return "Intuition: no settings reported";
  const width = Math.max(...rows.map((row) => row.key.length));
  const lines = rows.map((row) =>
    `${row.overridden ? "*" : " "} ${row.key.padEnd(width)}  ${formatValue(row.value)}`);
  return [
    `Intuition settings (${rows.length}); * changed from the default`,
    ...lines,
    "",
    "/intuition <key>            read one setting",
    "/intuition <key> <value>    write one setting",
    "/intuition help             the same list with each setting's help line",
  ].join("\n");
}

function settingsHelp(rows: Setting[]): string {
  return rows
    .map((row) => `${row.key} = ${formatValue(row.value)}\n  ${row.help}`)
    .join("\n");
}

/**
 * Registered as a command and deliberately not as a tool: these keys include the
 * safety knobs, so only a person at the keyboard may change them.
 */
function registerConfigCommand(pi: ExtensionAPI): void {
  pi.registerCommand("intuition", {
    description: "Show or change an Intuition setting",
    getArgumentCompletions: (prefix: string) => {
      const items = settings.map((row) => ({
        value: row.key,
        label: `${row.key} = ${formatValue(row.value)}`,
      }));
      const matching = items.filter((item) => item.value.startsWith(prefix.trim()));
      return matching.length ? matching : null;
    },
    handler: async (args: string, ctx: any) => {
      const parts = args.trim().split(/\s+/).filter(Boolean);
      try {
        if (!parts.length) {
          const rows = settings.length ? settings : await loadSettings();
          ctx.ui.notify(settingsSummary(rows), "info");
          return;
        }
        const [key, ...rest] = parts;
        if (key === "help") {
          ctx.ui.notify(settingsHelp(settings.length ? settings : await loadSettings()),
                        "info");
          return;
        }
        if (!rest.length) {
          const rows = settings.length ? settings : await loadSettings();
          const row = rows.find((candidate) => candidate.key === key);
          if (!row) throw new Error(`unknown setting ${key}; run /intuition for the list`);
          ctx.ui.notify(
            `${row.key} = ${formatValue(row.value)} (default ${formatValue(row.default)})\n` +
            `${row.help}`, "info");
          return;
        }
        const written = await rpc.call("config_set", { key, value: rest.join(" ") });
        await loadSettings();
        ctx.ui.notify(`${written.key} = ${formatValue(written.value)}`, "info");
      } catch (error) {
        ctx.ui.notify(`intuition: ${(error as Error)?.message ?? error}`, "error");
      }
    },
  });
}

export default function (pi: ExtensionAPI) {
  const manifest = loadManifest();
  for (const spec of isChild ? manifest.subagent : manifest.main) {
    pi.registerTool(buildTool(spec));
  }
  registerConfigCommand(pi);

  pi.on("session_start", async () => {
    prefixSent = false;
    rpc.start();
    await loadSettings().catch(() => undefined);
  });

  pi.on("before_agent_start", async (event: any) => {
    lastPrompt = typeof event?.prompt === "string" ? event.prompt : "";
    if (prefixSent) return undefined;
    try {
      const result = await rpc.call("prefix", {
        role: isChild ? "subagent" : "main",
        agent: agentName,
      });
      prefixSent = true;
      const existing = event?.systemPrompt ?? "";
      return { systemPrompt: existing ? `${result.text}\n\n${existing}` : result.text };
    } catch (error) {
      console.error("[intuition] prefix failed:", error);
      return undefined;
    }
  });

  pi.on("turn_end", async (event: any) => {
    if (isChild) return;                        // children never write raw/
    const turns: Array<[string, string]> = [
      ["user", lastPrompt],
      ["assistant", messageText(event?.message)],
    ];
    for (const [role, text] of turns) {
      if (!text.trim()) continue;
      try {
        await rpc.call("capture_turn", { host: "pi", role, text });
      } catch {
        /* capture is best-effort */
      }
    }
  });

  pi.on("session_before_compact", async (event: any) => {
    try {
      await rpc.call("checkpoint", { summary_text: checkpointText(event?.preparation) });
      prefixSent = false;                       // rebuild the prefix after compaction
    } catch {
      /* best-effort */
    }
  });

  pi.on("session_shutdown", async () => {
    try {
      await rpc.call("tick", { reason: "session_end", session_end: true }, TICK_TIMEOUT_MS);
    } catch (error) {
      console.error("[intuition] session-end tick failed:", error);
    }
    rpc.close();
  });
}
