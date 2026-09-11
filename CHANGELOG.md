# Changelog

## Unreleased (Stage 8: multi-backend adapter engine)

- New profile-driven backend engine: `BackendProfile` data schema +
  generic `ProfileAdapter`; adding a backend is now a profile entry,
  not new orchestration code. No router dependency, no claim/event/
  worktree behavior changes.
- Profiles: `claude_code` (migrated byte-identical, verified),
  `codex` (stub-verified e2e, real binary absent), `gemini_cli`,
  `opencode`, `cursor_agent`, `aider` (mock-verified, honestly
  unverified). Per-node `--backend` + repeatable `--backend-config
  key=value` (e.g. opencode's required `model`); `--backend` run
  override;   `skein backends list` support matrix generated from the
  registry and lockstep-tested against the README.
- Bring-your-own-backend: `skein backends add/remove` registers any
  CLI as a JSON data profile (repo or user scope), so new backends
  need no code changes; broken files warn-and-skip, built-ins are
  unremovable.
- Tests only (no behavior change): ASCII guard over shipped sources
  (cp1252 pattern), pin that log commits contain only `.skein` paths.

## 0.2.0 — completion pass

- Web canvas: `skein serve` (stdlib HTTP + embedded SVG UI, live
  polling, add/edit/claim/release, same human_interrupt rules as CLI).
- Blast-radius inference: `skein infer-blast` heuristic suggestions,
  print-only, never auto-applied.
- Multi-machine sync: `skein sync` (fetch/merge/push with log-union
  auto-merge; non-.skein conflicts abort for manual resolve).
- Shared `edits.py` mutation ruleset for CLI and web API.

- Claude Code adapter now invokes `claude -p <prompt>
  --dangerously-skip-permissions --output-format text` (headless
  file-editing requires the permission-skip flag; output format pinned
  for the stdout contract).
- Supervisor `run_node` gained `adapter_timeout` (default 1800s,
  `--adapter-timeout` in `skein run`): a stalled backend process is
  killed and recorded as `failed` instead of hanging.
- Event-log git commits scoped to `.skein` so unrelated user-staged
  files can't ride a log commit.
- Proven against the real `claude` CLI binary end-to-end (see README
  "Backend note"); CI remains stub-only with no live credentials.

## 0.1.0 — 2026-09-09

- Initial release: event-sourced task graph (`.skein/log.ndjson` + derived `graph.json`).
- CAS claim protocol with dependency + blast-radius eligibility, TTL/heartbeat, reaper, explicit force-release.
- Git worktree isolation per node; base branch inherited from dependency; auto-created integration nodes with deterministic merge attempt.
- Verification gate: completion commands executed for real, evidence recorded, done/failed gated on exit codes.
- One backend adapter: Claude Code (headless `claude --print` against the worktree).
- `skein run` full path: claim → worktree → adapter → verify → report, supervised with heartbeat + `human_interrupt` abort.
- CLI: `init`, `node add/edit`, `graph`, `claim`, `run`, `release [--force]`, `status`, `log`, `reap`.
