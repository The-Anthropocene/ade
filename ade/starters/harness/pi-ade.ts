// id: pi.extensions.ade
// what: ADE's Claude Code hooks for Pi (guard, protected paths, first-edit briefing, grep notes, turn-end hold, start handover), plus a flat edit tool
// cmd: pi (auto-discovered from .pi/extensions/)
// reads: stdout of `ade hook <name>`
// writes: nothing itself; ade hook writes .ade/seen/ and .ade/records.jsonl
//
// Each Pi event is handed to `ade hook <name>` in the JSON shape Claude Code sends, and its answer is mapped back:
// permissionDecision "deny" blocks the tool, additionalContext is appended to the tool result (or system prompt at start).
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { Type } from "typebox";
import { createEditTool, type ExtensionAPI } from "@mariozechner/pi-coding-agent";

const ADE = process.env.ADE_BIN || "ade";
const session = process.env.ADE_SESSION || randomUUID();

function hook(name: string, cwd: string, data: Record<string, unknown>) {
  const r = spawnSync(ADE, ["hook", name], {
    cwd, input: JSON.stringify({ session_id: session, cwd, ...data }), encoding: "utf8",
    shell: process.platform === "win32" && /\.(cmd|bat)$/i.test(ADE), // a Windows .cmd launcher needs a shell
  });
  try { return (JSON.parse(r.stdout || "{}").hookSpecificOutput || {}) as Record<string, string>; } catch { return {}; }
}

// Which ade hook a Pi tool call goes to, and the Claude Code tool_input it gets.
function route(tool: string, input: any): [string, Record<string, unknown>] | null {
  if (tool === "bash") return ["bash", { tool_name: "Bash", tool_input: { command: input.command } }];
  if (tool === "edit" || tool === "write") return ["brief", { tool_name: tool === "edit" ? "Edit" : "Write", tool_input: { file_path: input.path } }];
  return null;
}

export default function (pi: ExtensionAPI) {
  const notes = new Map<string, string>(); // toolCallId -> briefing to append to that call's result

  pi.on("before_agent_start", async (event, ctx) => {
    const out = hook("start", ctx.cwd, { hook_event_name: "SessionStart", source: "startup" });
    if (out.additionalContext) return { systemPrompt: `${event.systemPrompt}\n\n${out.additionalContext}` };
  });

  pi.on("tool_call", async (event, ctx) => {
    const r = route(event.toolName, event.input);
    if (!r) return;
    const out = hook(r[0], ctx.cwd, { hook_event_name: "PreToolUse", ...r[1] });
    if (out.permissionDecision === "deny") return { block: true, reason: out.permissionDecisionReason || "refused by ade" };
    if (out.additionalContext) notes.set(event.toolCallId, out.additionalContext);
  });

  pi.on("tool_result", async (event, ctx) => {
    let extra = notes.get(event.toolCallId) || "";
    notes.delete(event.toolCallId);
    if (event.toolName === "bash" || event.toolName === "grep") {
      const text = event.content.map((c: any) => c.text || "").join("");
      const command = event.toolName === "bash" ? event.input.command : `grep ${event.input.pattern}`;
      const out = hook("grep", ctx.cwd, { hook_event_name: "PostToolUse", tool_name: "Bash", tool_input: { command }, tool_response: { stdout: text } });
      if (out.additionalContext) extra += (extra ? "\n\n" : "") + out.additionalContext;
    }
    if (extra) return { content: [...event.content, { type: "text", text: `\n\n${extra}` }] };
  });

  pi.on("agent_end", async (_event, ctx) => { hook("turn-end", ctx.cwd, { hook_event_name: "Stop" }); });

  // A flat edit tool: some local models (Qwen3-Coder via Ollama) cannot emit the built-in's edits[] array, and Ollama
  // then ends the stream with no tool call. One replacement per call keeps every argument a plain string.
  pi.registerTool({
    name: "edit",
    label: "Edit",
    description: "Edit a file by replacing one exact, unique piece of text. Call it again for each further change.",
    promptSnippet: "Replace one exact, unique text span in a file",
    parameters: Type.Object({
      path: Type.String({ description: "Path to the file to edit (relative or absolute)" }),
      oldText: Type.String({ description: "Exact text to replace; must occur once in the file" }),
      newText: Type.String({ description: "Replacement text" }),
    }),
    prepareArguments(args: any) {
      // a model that still sends the built-in shape gets its first edit
      if (Array.isArray(args?.edits) && args.edits.length) return { path: args.path, ...args.edits[0] };
      return args;
    },
    async execute(id, params: any, signal, onUpdate, ctx) {
      return createEditTool(ctx.cwd).execute(id, { path: params.path, edits: [{ oldText: params.oldText, newText: params.newText }] }, signal, onUpdate);
    },
  });
}
