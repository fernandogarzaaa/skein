---
description: Summarise the skein task graph, most actionable nodes first
---

Run `skein status` and `skein graph` in the repository root.

Summarise per node as done, running, failed or blocked. For a blocked node say which dependencies it waits on. For a failed node say what to do next (for example `skein node edit <id> --status unclaimed` to retry, or inspect with `skein log --node <id>`). List the most actionable nodes first: failed, then running, then ready, then blocked, then done. Finish with one line on what is safe to run or ship next.
