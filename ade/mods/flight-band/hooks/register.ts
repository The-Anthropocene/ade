// id: ade.mods.flight-band
// what: Claude Code mod: every 3 s reads ADE's records and draws the flying card's five stages (lane, build, review, test, land) above the prompt, keeps a card that lands on screen with all five ticked for the next two polls (or until another card's journal row appears) so a test and land finishing between polls still show, with a toast on land or park and a pane on vscode and Claude Desktop (terminal gets the band only); it writes nothing to the transcript, and a Remote Control viewer, which gets only the transcript stream and no ui.render or toasts, sees none of it
// cmd: -
// reads: .ade/records.jsonl
// writes: -
import type { Register } from 'claude-code'

const STAGES = ['lane', 'build', 'review', 'test', 'land']
const PANE = 'ade-flight'

type Flight = { id: string; title: string; marks: string[] }
type Row = Record<string, unknown>

let flight: Flight | null = null
let paneOpened = false
let seen: number | null = null
let hold = 0 // polls left to keep a landed card's ticks on screen
let held: string | null = null // the card whose ticks are being held

const text = (v: unknown) => (typeof v === 'string' ? v : '')

export function parse(raw: string): Row[] {
  const rows: Row[] = []
  for (const line of raw.split('\n')) {
    try {
      rows.push(JSON.parse(line))
    } catch {}
  }
  return rows
}

// One toast per card whose landed status or parked decision row sits at or after index `seen` (rows read at the last poll); null = first poll, no history.
export function toasts(rows: Row[], seen: number | null): string[] {
  if (seen === null) return []
  const out = new Map<string, string>()
  rows.forEach((r, i) => {
    if (i < seen) return
    if (r.type === 'card' && r.status === 'landed') out.set(text(r.id), `${text(r.id)} landed`)
    const id = text(r.card)
    if (r.type === 'decision' && id && text(r.text).startsWith(`${id} parked:`)) {
      out.set(id, `${id} parked: ${text(r.text).slice(`${id} parked:`.length).trim()}`)
    }
  })
  return [...out.values()]
}

// The newest card with journal rows that has neither landed nor parked.
function scan(rows: Row[]): Flight | null {
  const status: Record<string, string> = {}
  const title: Record<string, string> = {}
  const lastJournal: Record<string, number> = {}
  const parkedAt: Record<string, { at: number; note: string }> = {}
  rows.forEach((r, i) => {
    const id = text(r.type === 'journal' ? r.card : r.type === 'card' ? r.id : r.card)
    if (!id) return
    if (text(r.title)) title[id] = text(r.title)
    if (r.type === 'card' && text(r.status)) status[id] = text(r.status)
    if (r.type === 'journal') lastJournal[id] = i
    if (r.type === 'decision' && text(r.text).startsWith(`${id} parked:`)) {
      parkedAt[id] = { at: i, note: text(r.text).slice(`${id} parked:`.length).trim() }
    }
  })
  const parked = (id: string) => (parkedAt[id]?.at ?? -1) > (lastJournal[id] ?? -1)
  const done = (id: string) => status[id] === 'landed' || status[id] === 'closed'

  const id = Object.keys(lastJournal)
    .sort((a, b) => lastJournal[b] - lastJournal[a])
    .find(c => !done(c) && !parked(c))
  if (!id) return null

  const outcome: Record<string, string> = {}
  for (const r of rows) {
    if (r.type === 'journal' && r.card === id) outcome[text(r.stage)] = text(r.outcome)
  }
  let current = false
  const marks = STAGES.map(s => {
    if (outcome[s] === 'advance' || outcome[s] === 'done') return '✓'
    if (outcome[s]) return '✗'
    if (current) return '○'
    current = true
    return '■'
  })
  return { id, title: title[id] ?? '', marks }
}

const line = (f: Flight) =>
  `${f.id}${f.title ? ` ${f.title}` : ''}  ` + STAGES.map((s, i) => `${f.marks[i]} ${s}`).join('  ')

export const register: Register = on => {
  on('session.start', ($, e, next) => {
    $.clock.every(3000, async () => {
      let raw: string
      try {
        raw = await $.fs.read('.ade/records.jsonl')
      } catch {
        return // a failed read changes nothing: seen, flight and band stay as they were
      }
      const rows = parse(raw)
      for (const t of toasts(rows, seen)) $.ui.toast(t)
      seen = rows.length
      let now = scan(rows)
      if (!now && flight) {
        const landed = rows.some(r => r.type === 'card' && r.id === flight!.id && r.status === 'landed')
        if (landed && held !== flight.id) {
          now = { ...flight, marks: STAGES.map(() => '✓') }
          held = flight.id
          hold = 2
        } else if (landed && hold > 0) {
          now = flight
          hold--
        }
      }
      const key = (f: Flight | null) => (f ? `${f.id}|${f.marks.join('')}` : '')
      const changed = key(now) !== key(flight)
      flight = now
      if (!changed) return
      $.ui.invalidate('ui.render')
    })
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, ($, e, next) => {
    if (!flight) return next(e)
    if ((e.surface === 'vscode' || e.surface === 'desktop') && !paneOpened) {
      paneOpened = true
      $.ui.open({ id: PANE, title: 'ADE flight' })
    }
    const { Box, Text } = $.ui.resolve(e)
    return h(Box, { borderStyle: 'round', paddingX: 1 }, h(Text, null, line(flight)))
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, ($, e, next) => {
    const { Box, Text } = $.ui.resolve(e)
    if (!flight) return h(Box, null, h(Text, { dimColor: true }, 'No card flying'))
    return h(
      Box,
      null,
      h(Text, null, `${flight.id}${flight.title ? ` ${flight.title}` : ''}`),
      ...STAGES.map((s, i) => h(Text, null, `${flight!.marks[i]} ${s}`)),
    )
  })
}
