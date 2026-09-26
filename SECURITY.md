# Security policy

## Reporting a vulnerability

Report privately through
[GitHub security advisories](https://github.com/fernandogarzaaa/skein/security/advisories/new).
Please do not open a public issue for a vulnerability.

Include what you would need to reproduce it yourself: version or commit
and a minimal case. You should get an initial response within a week.

## Supported versions

Only the latest published minor release receives security fixes. Skein is
pre-1.0, so older minors are not patched — upgrade first, then report if
the issue persists.

## Scope notes

A few things about Skein's design are worth knowing before reporting:

- **Claimed nodes execute agent work.** Skein coordinates multi-agent
  work through task graphs; agents claim nodes and run arbitrary commands
  in their worktrees. Only run Skein graphs from sources you trust, and
  review node definitions before claiming.
- **Git worktrees touch your repositories.** Skein creates and manages
  git worktrees for parallel agents. It operates on the repositories you
  point it at; do not run it against repositories with uncommitted
  sensitive changes you cannot afford to have read by an agent.
- **The CLI shells out to Python.** Skein invokes the Python interpreter
  to run its own modules. The interpreter resolution honors `APE_PYTHON`
  and falls back from `python` to `python3`; a hostile `PATH` could
  redirect this, so run Skein in an environment you control.
