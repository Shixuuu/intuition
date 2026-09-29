/**
 * Intuition — Pi agent extension (plan §8.2).
 *
 * Spawns `intuition rpc` once per session (JSON lines over stdio), registers
 * the memory tools, builds the stable prompt prefix at session start, captures
 * turns, checkpoints on compaction, and runs the Steward at session shutdown.
 *
 * Install: `intuition install pi` → ~/.pi/agent/extensions/intuition/
 * Children spawned as separate `pi` processes load user-scope extensions too;
 * they are detected via CAIRN_ROLE/CAIRN_AGENT env (set by the spawner) and
 * get search/read only. Main gets the full toolset.
 */

import { spawn, type ChildProcess } from "node:child_process";
import * as os from "node:os";
import * as path from "node:path";

interface Pending {
  resolve: (value: any) => void;
  reject: (reason: any) => void;
}

class IntuitionRpc {
  private proc: ChildProcess | null = null;
  private nextId = 1;
  private pending = new Map<number, Pending>();
  private buffer = "";

  start(): void {
    if (this.proc) return;
    this.proc = spawn("intuition", ["rpc"], {
      env: { ...process.env },
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.proc.stdout!.setEncoding("utf8");
    this.proc.stdout!.on("data", (chunk: string) => {
      this.buffer += chunk;
      let nl: number;
      while ((nl = this.buffer.indexOf("\n")) >= 0) {
        const line = this.buffer.slice(0, nl).trim();
        this.buffer = this.buffer.slice(nl + 1);
        if (!line) continue;
        try {
          const msg = JSON.parse(line);
          const p = this.pending.get(msg.id);
          if (p) {
            this.pending.delete(msg.id);
            if (msg.error) p.reject(new Error(msg.error));
            else p.resolve(msg.result);
          }
        } catch {
          /* ignore malformed line */
        }
      }
    });
    this.proc.stderr!.on("data", (d: Buffer) =>
      console.error("[intuition]", d.toString().trim()));
    this.proc.on("exit", () => {
      this.proc = null;
      for (const p of this.pending.values()) p.reject(new Error("rpc exited"));
      this.pending.clear();
    });
  }

  call(method: string, params: Record<string, unknown> = {}): Promise<any> {
    this.start();
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.proc!.stdin!.write(JSON.stringify({ id, method, params }) + "\n");
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`rpc timeout: ${method}`));
        }
      }, 10_000);
    });
  }

  close(): void {
    if (this.proc) {
      this.proc.stdin!.end();
      this.proc = null;
    }
  }
}

const rpc = new IntuitionRpc();
const isChild = process.env.CAIRN_ROLE === "subagent";
const agentName = process.env.CAIRN_AGENT || "child";
let prefixSent = false;

const SEARCH_SCHEMA = {
  type: "object",
  properties: {
    queries: { type: "array", items: { type: "string" }, minItems: 1, maxItems: 3 },
  },
  required: ["queries"],
} as const;

const READ_SCHEMA = {
  type: "object",
  properties: { id_or_name: { type: "string" } },
  required: ["id_or_name"],
} as const;

export default function (pi: any) {
  const tools: any[] = [
    {
      name: "memory_search",
      description:
        "Search memory. Give 1-3 phrasings in the user's own words. " +
        "Results marked (pending) are not confirmed yet.",
      parameters: SEARCH_SCHEMA,
      execute: async (_id: string, args: any) => {
        const r = await rpc.call("search", { queries: args.queries });
        return r.rendered ?? JSON.stringify(r);
      },
    },
    {
      name: "memory_read",
      description: "Read one full memory record by id or name.",
      parameters: READ_SCHEMA,
      execute: async (_id: string, args: any) => {
        const r = await rpc.call("read", { id_or_name: args.id_or_name });
        return r.error ? `error: ${r.error}` : JSON.stringify(r, null, 1);
      },
    },
  ];

  if (!isChild) {
    tools.push(
      {
        name: "memory_note",
        description:
          "Save a memory proposal. evidence MUST be the user's exact words.",
        parameters: {
          type: "object",
          properties: {
            text: { type: "string" },
            kind: { type: "string" },
            about: { type: "string" },
            evidence: { type: "string" },
          },
          required: ["text", "evidence"],
        },
        execute: async (_id: string, args: any) => {
          const r = await rpc.call("note", { ...args, host: "pi" });
          return r.ok ? `queued (${r.inbox_id}) — pending until the Steward confirms` : `error: ${r.error}`;
        },
      },
      {
        name: "memory_brief",
        description:
          "Build a delegation brief (goal, output, tools, boundaries, decisions, memory) for a child pi process.",
        parameters: {
          type: "object",
          properties: {
            agent: { type: "string" },
            goal: { type: "string" },
            output: { type: "string" },
            boundaries: { type: "string" },
          },
          required: ["agent", "goal"],
        },
        execute: async (_id: string, args: any) => {
          const r = await rpc.call("brief", { ...args, agent: args.agent });
          if (r.error) return `error: ${r.error}`;
          return [
            r.brief,
            "",
            "## Memory prefix (give the child this context)",
            r.memory_prefix,
            "",
            "Spawn with: `pi -p --agent <agent>` and pass the brief + memory prefix",
            "as context. Set CAIRN_ROLE=subagent CAIRN_AGENT=<agent> in its env.",
          ].join("\n");
        },
      },
      {
        name: "memory_learn",
        description: "Record a child's learnings block after reviewing it.",
        parameters: {
          type: "object",
          properties: {
            task_id: { type: "string" },
            learnings: { type: "array", items: { type: "object" } },
          },
          required: ["task_id", "learnings"],
        },
        execute: async (_id: string, args: any) => {
          const r = await rpc.call("learn", { ...args, agent: agentName });
          return r.ok ? "learnings queued to inbox" : `error: ${r.error}`;
        },
      },
      {
        name: "memory_now",
        description: "Replace working/NOW.md — your plan, open threads, task ids.",
        parameters: {
          type: "object",
          properties: { content: { type: "string" } },
          required: ["content"],
        },
        execute: async (_id: string, args: any) => {
          const r = await rpc.call("now", args);
          return r.ok ? "NOW.md updated" : `error: ${r.error}`;
        },
      },
    );
  }

  for (const t of tools) pi.registerTool(t);

  pi.on("session_start", async (_e: unknown, ctx: any) => {
    prefixSent = false;
    rpc.start();
  });

  pi.on("before_agent_start", async (e: any, _ctx: any) => {
    if (prefixSent) return;                       // stable prefix: once per session
    try {
      const r = await rpc.call("prefix", {
        role: isChild ? "subagent" : "main",
        agent: agentName,
      });
      e.systemPrompt = r.text + (e.systemPrompt ? "\n\n" + e.systemPrompt : "");
      prefixSent = true;
    } catch (err) {
      console.error("[intuition] prefix failed:", err);
    }
  });

  pi.on("turn_end", async (e: any, _ctx: any) => {
    if (isChild) return;                          // children never write raw/
    try {
      await rpc.call("capture_turn", {
        host: "pi",
        role: "user",
        text: e?.message?.content ?? "",
      });
    } catch {
      /* capture is best-effort */
    }
  });

  pi.on("session_before_compact", async (e: any, _ctx: any) => {
    try {
      const r = await rpc.call("checkpoint", { summary_text: JSON.stringify(e).slice(0, 4000) });
      if (e && typeof e === "object") (e as any).checkpoint = r.checkpoint;
      prefixSent = false;                           // rebuild prefix after compaction
    } catch {
      /* best-effort */
    }
  });

  pi.on("session_shutdown", async (_e: unknown, _ctx: any) => {
    try {
      await rpc.call("tick", { reason: "session_end" });
    } catch {
      /* steward runs on its timer anyway */
    }
    rpc.close();
  });
}
