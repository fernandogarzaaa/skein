---
description: Merge finished skein nodes onto the current feature branch
argument-hint: "[target branch]"
---

1. Run `git branch --show-current`. Refuse if it is `main` or `master`, or if the requested target branch (`$ARGUMENTS`) is.
2. Run `skein status` and show which nodes are done. If none are done, stop.
3. Run `skein ship --all`. Only if the user passed a branch in `$ARGUMENTS`, run `skein ship --all --to $ARGUMENTS` instead.
4. Report what merged and any conflicts, with the node ids involved. Never pass `--force`; on a conflict, stop and ask the user how to proceed.
