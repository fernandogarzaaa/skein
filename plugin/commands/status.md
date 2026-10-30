---
description: Summarise the skein task graph, most actionable nodes first
---

Run `skein status` and `skein graph` in the repository root. If `skein status` prints `(empty graph)`, say there is no graph yet and suggest `skein init` followed by `skein node add`, then stop.

Group nodes by skein's own statuses:

- `failed`: say what to do next, for example inspect with `skein log --node <id>` and retry with `skein node edit <id> --status unclaimed`.
- `needs_human`: a person must act. Show the node's NOTE column and `skein log --node <id>`.
- `blocked`: set by a person, not by dependencies. Check `skein log --node <id>` for the reason.
- `claimed` and `in_progress`: running. Show the HOLDER and LEASE columns.
- `unclaimed`: split into ready (every dependency `done`) and waiting (name the dependencies not yet done, from the `[depends: ...]` lines in `skein graph`). A LEASE of `backoff Ns` means it waits to retry.
- `done`: say whether it is shipped (the SHIPPED column).

List the groups in that order. Finish with one line on what is safe to run or ship next.
