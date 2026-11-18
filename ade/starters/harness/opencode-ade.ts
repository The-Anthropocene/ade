// id: opencode.plugin.ade
// what: ADE's Claude Code hooks for opencode (guard, protected paths, first-edit briefing, grep notes, turn-end hold, start handover)
// cmd: opencode (auto-loaded from .opencode/plugin/)
// reads: stdout of `ade hook <name>`
// writes: nothing itself; ade hook writes .ade/seen/ and .ade/records.jsonl
//
// Each opencode hook is handed to `ade hook <name>` in the JSON shape Claude Code sends, and its answer is mapped back:
// permissionDecision "deny" throws (opencode reports the tool as failed with that reason), additionalContext is
// appended to the tool's output, or to the system prompt at start.
import { spawnSync } from "node:child_process"

const ADE = process.env.ADE_BIN || "ade"

function hook(name: string, cwd: string, session: string, data: Record<string, unknown>) {
  const r = spawnSync(ADE, ["hook", name], {
    cwd, input: JSON.stringify({ session_id: session, cwd, ...data }), encoding: "utf8",
    shell: process.platform === "win32" && /\.(cmd|bat)$/i.test(ADE), // a Windows .cmd launcher needs a shell
  })
  try { return (JSON.parse(r.stdout || "{}").hookSpecificOutput || {}) as Record<string, string> } catch { return {} }
}

const SHELL = (tool: string) => tool === "bash" || tool === "shell" // opencode 1.x names the shell tool bash, 2.x shell

function route(tool: string, args: any): [string, Record<string, unknown>] | null {
  if (SHELL(tool)) return ["bash", { tool_name: "Bash", tool_input: { command: args.command } }]
  if (tool === "edit" || tool === "write") return ["brief", { tool_name: tool === "edit" ? "Edit" : "Write", tool_input: { file_path: args.filePath } }]
  return null
}

const AdeHooks = async ({ directory }: { directory: string }) => {
  const notes = new Map<string, string>() // callID -> briefing to append to that call's output
  let handover: string | undefined
  return {
    "experimental.chat.system.transform": async (input: any, output: { system: string[] }) => {
      // This transform is per request, not persisted, so the handover goes on every call.
      // opencode's title call shares the session and gets it too, which costs tokens but changes nothing.
      if (handover === undefined)
        handover = hook("start", directory, input?.sessionID || "opencode", { hook_event_name: "SessionStart", source: "startup" }).additionalContext || ""
      if (handover) output.system.push(handover)
    },
    "tool.execute.before": async (input: any, output: any) => {
      const r = route(input.tool, output.args || {})
      if (!r) return
      const out = hook(r[0], directory, input.sessionID, { hook_event_name: "PreToolUse", ...r[1] })
      if (out.permissionDecision === "deny") throw new Error(out.permissionDecisionReason || "refused by ade")
      if (out.additionalContext) notes.set(input.callID, out.additionalContext)
    },
    "tool.execute.after": async (input: any, output: any) => {
      let extra = notes.get(input.callID) || ""
      notes.delete(input.callID)
      if (SHELL(input.tool) || input.tool === "grep") {
        const args = input.args || {}
        const command = SHELL(input.tool) ? args.command : `grep ${args.pattern}`
        const out = hook("grep", directory, input.sessionID, { hook_event_name: "PostToolUse", tool_name: "Bash", tool_input: { command }, tool_response: { stdout: String(output.output ?? "") } })
        if (out.additionalContext) extra += (extra ? "\n\n" : "") + out.additionalContext
      }
      if (extra) output.output = `${output.output ?? ""}\n\n${extra}`
    },
    event: async ({ event }: any) => {
      if (event?.type === "session.idle") hook("turn-end", directory, event.properties?.sessionID || "opencode", { hook_event_name: "Stop" })
    },
  }
}

// 1.19 calls server(); 2.x calls setup() and passes one mutable event per hook.
// Keep the hooks above as the shared ADE protocol, without importing a version-specific SDK.
export default {
  id: "ade",
  server: AdeHooks,
  async setup(ctx: any) {
    const hooks = await AdeHooks({ directory: ctx.location.directory })
    for (const name of ["context", "title"])
      await ctx.session.hook(name, async (event: any) => {
        const output = { system: [] as string[] }
        await hooks["experimental.chat.system.transform"](event, output)
        for (const text of output.system) event.system.push({ type: "text", text })
      })
    await ctx.tool.hook("execute.before", async (event: any) => {
      await hooks["tool.execute.before"]({ ...event, callID: event.id }, { args: event.input })
    })
    await ctx.tool.hook("execute.after", async (event: any) => {
      const input = { ...event, callID: event.id, args: event.input }
      const content = event.status === "completed" ? event.result.content : []
      const text = content.filter((part: any) => part.type === "text").map((part: any) => part.text).join("\n")
      const output = { output: text }
      await hooks["tool.execute.after"](input, output)
      if (event.status === "completed" && output.output !== text)
        content.push({ type: "text", text: output.output.slice(text.length).trimStart() })
    })
    await ctx.session.hook("experimental.idle", async (event: any) => {
      await hooks.event({ event: { type: "session.idle", properties: { sessionID: event.sessionID } } })
    })
  },
}
