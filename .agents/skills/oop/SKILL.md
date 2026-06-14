---
name: oop
description: Out-of-process mode — skip SE ceremony for small, targeted changes
---

# /oop — Out of Process

Skip all SE process ceremony and make a quick, targeted change directly
on master. This is for changes where the full process would be overkill.

The stakeholder may request this mode with a variety of phrases, such as: 

- "DO it out of process"
- "Skip the process, just change it"
- "Don't create tickets, just fix it"
- "This is a one-off, just change it on master"
- "oop, plesse"  

## When to use

- When the stakeholder requests it. 

## When NOT to use

- THe stakeholder did not explicitly request it.  

## Process

1. Read the relevant code.
2. Make the change.
3. Run the full test suite: `uv run pytest`.
4. If tests pass, commit directly to master with a descriptive message.
5. Run `dotconfig version bump` and commit the result (`chore: bump
   version`). Tools are installed editable, so the version is how
   sessions tell which code is live — bump after every OOP commit.
6. If the work addressed an issue (from `.clasi/issues/`), call
   `move_issue_to_done(filename)` to close it. The commit is not the
   finish line — the issue lifecycle must be closed too.
7. If tests fail, fix the issue and re-run.

That's it. No sprint, no tickets, no review gates, no architecture review.

## Rules

- Do NOT create sprints, tickets, or planning documents.
- Do NOT use `create_sprint`, `create_ticket`, or other artifact tools.
- Do NOT ask for stakeholder approval at process gates — there are no gates.
- DO run tests before committing. Tests are never optional.
- DO write a clear commit message explaining the change.
- DO run `dotconfig version bump` after each commit and commit the result.
