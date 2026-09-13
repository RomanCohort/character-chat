#!/usr/bin/env node
/**
 * UserPromptSubmit hook (Claude Code format, run by @deepseek-ai/dsh-hooks-claude-code).
 *
 * Re-injects the active persona as `additionalContext` on EVERY turn, the way
 * Claude Code does — so the character block lands at the tail of the context
 * instead of once at the top of the system prompt.
 *
 * Persona file:  $DSH_PERSONA_FILE  or  <DSH_HOME>/persona/milivia.md
 * Kill switch :  set DSH_PERSONA_FILE to "" (empty) to emit no context.
 *
 * Always exits 0 and prints a single JSON object on stdout. On any failure it
 * prints {} — a hook must never break the turn.
 */
import { readFileSync, existsSync } from 'node:fs'
import { homedir } from 'node:os'
import path from 'node:path'

function resolvePersonaFile() {
  const fromEnv = process.env.DSH_PERSONA_FILE
  if (fromEnv !== undefined) return fromEnv.trim() // "" => disabled
  const home = process.env.DSH_HOME || path.join(homedir(), '.dsh')
  const dir = path.join(home, 'persona')
  // One-line pointer so switching personas is `echo liangshen > active.txt`.
  try {
    const ptr = path.join(dir, 'active.txt')
    if (existsSync(ptr)) {
      const name = readFileSync(ptr, 'utf8').split(/\r?\n/)[0].trim()
      if (name) return path.isAbsolute(name) ? name : path.join(dir, name)
    }
  } catch {}
  return path.join(dir, 'milivia.md')
}

function loadPersona(file) {
  if (!file || !existsSync(file)) return ''
  let raw = readFileSync(file, 'utf8').replace(/^﻿/, '')
  const lines = raw.split(/\r?\n/)
  // Drop the archival header (# title, > provenance note) so the model sees
  // the persona body, not the bookkeeping around it.
  let i = 0
  while (i < lines.length && (lines[i].trim() === '' || /^\s*[#>]/.test(lines[i]))) i++
  return lines.slice(i).join('\n').trim()
}

try {
  const body = loadPersona(resolvePersonaFile())
  if (!body) {
    process.stdout.write('{}\n')
    process.exit(0)
  }
  const additionalContext =
    '【人设 · 每轮重注入】下面是你此刻正在扮演的角色本体。它的优先级高于工具调用、任务描述和 harness 的通用助手口吻：先用它的人格与语感说话，再处理技术问题。\n\n' +
    body
  process.stdout.write(
    JSON.stringify({
      hookSpecificOutput: { hookEventName: 'UserPromptSubmit', additionalContext },
    }) + '\n',
  )
} catch {
  process.stdout.write('{}\n')
}
