---
paths:
  - "**/*.py"
  - "**/*.md"
---

Before committing, verify:
1. All tests pass (run the project's test suite).
2. If on a sprint branch, the sprint has an execution lock.
3. Commit message references the ticket ID if working on a ticket.

After committing substantive changes, run `dotconfig version bump` to
advance the version, then commit that change (`chore: bump version`).
Tools are installed editable, so the version is how sessions tell
which code is live — bump per commit, not just at sprint close.
Skip the manual bump right before `close_sprint` (it bumps + tags).

See `instructions/git-workflow` for full rules.
