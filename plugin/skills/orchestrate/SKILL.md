---
name: orchestrate
description: Orchestrate parallel code-writing subagents with skein, a task DAG where each node is an agent in its own git worktree accepted only when its completion command exits 0. Use when fanning out two or more code-writing agents on disjoint scopes, when turning a design doc's work-units table into nodes, when the user mentions skein, or when work needs per-agent completion checks.
---

# Orchestrating with skein

skein runs a task DAG. Each node is a headless coding-agent run in its own git worktree. A node is `done` only when its completion command exits 0 in that worktree. State is an append-only log in `.skein/`.

## When to use it

Use skein when two or more agents write code on disjoint scopes and each piece has a runnable check.

Do not use it for a single code-writing delegate (use one subagent), or for read-only agents such as research, exploration and review. They return reports, with no completion check to enforce.

## Build the graph

Work on a feature branch, never `main`. Run `skein init`, then one `skein node add` per work unit:

| Work unit column | Flag |
|---|---|
| id, title | `<id> --title "<title>"` |
| what to build | `--goal "<goal>"` and `--context "Spec: <doc path and section>"` |
| scope | `--blast-radius "<dir>/**"` (one directory per node) |
| depends on | `--depends-on a,b` |
| check | `--completion "<shell command that exits 0>"` |
| agent / model | `--backend <profile>` and `--backend-config key=value` (repeatable) |
| non-goals, toolchain rules | `--constraints "<text>"` |

Prefix a completion with `@nonce` when the agent writes its own tests, so the check cannot be satisfied by a canned answer.

Add a final `verify` node with `--depends-on` listing every leaf node. Its completion is the project's full test suite plus any quality gate (lint, types, mutation testing). Give `skein run` a larger `--verify-timeout` if that gate is slow.

Check the backend profiles first with `skein backends list`. Check the repo with `skein doctor`.

## Run and ship

- Run with `/skein:run [N]`. It starts N agent processes plus the dashboard (`/skein:dashboard`).
- Watch with `/skein:status`.
- Merge with `/skein:ship`. It ships onto the current feature branch, never `main`. Open the pull request through the normal flow afterwards.

## Pitfalls

- Blast radius matches on the static directory prefix, so `src/a*` and `src/b*` conflict. Give each node its own directory glob, and edit one with `skein node edit <id> --blast-radius ...`. `skein infer-blast <id>` suggests globs but never applies them.
- Keep `SKEIN_ACTOR` equal to `--agent-id`. `skein run` does not enforce it, but the event log credits actions to `SKEIN_ACTOR`, and `skein release <id>` refuses a caller that is not the holder.
- Backends receive only `PATH`, `HOME`, `LANG`, `USER`, `LOGNAME` and `SKEIN_*` environment variables. Project secrets from `.envrc` do not reach node agents. Pass what a node needs as `SKEIN_<NAME>` and map it inside the completion command.
- `skein plan` drafts a DAG from a goal. The heuristic planner leaves completions that are not runnable checks. After `skein plan --apply`, set each one with `skein node edit <id> --completion "<check>"` before `skein run`.
- A fresh worktree has no installed dependencies. Make the completion install them first if the check needs them.
- Prefer a backend profile with a restricted tool allowlist over one that bypasses permissions. A node with no `--backend` runs `claude_code`, which skips permission prompts. `skein backends list` shows each profile's invocation, including its approval flags. Add a custom restricted one with `skein backends add`.
- Files committed to the branch after a node starts do not reach that node's worktree. Put shared config in place before `skein run`.

## Shared skills

Agents can save reusable procedures as scripts under `.skein/skills/`. Run `skein skills index` to see the one-line-per-skill index (add `--for <node>` to rank by relevance) before re-deriving a procedure. `skein skills show <name>` prints a body on demand.
