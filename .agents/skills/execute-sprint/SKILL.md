---
name: execute-sprint
description: Executes sprint tickets serially — dispatches one programmer agent per ticket in dependency order
---

# Execute Sprint Skill

This skill executes all tickets in an active sprint. The team-lead
dispatches one programmer agent per ticket, in dependency order, one
at a time. All work happens directly on the sprint branch.

This skill is **strictly serial**. There is no parallel mode and no
worktree usage. Re-enabling parallel/worktree execution is a future
project; see
`docs/clasi/todo/define-proper-worktree-process-for-parallel-ticket-execution.md`.
When parallel execution is re-enabled, see
`docs/clasi/design/worktree-process.md` for the process spec.

## Inputs

- Active sprint with tickets in `open` status
- Execution lock acquired (`acquire_execution_lock`)
- Sprint branch exists and is checked out

## Process

### 1. Read Tickets

Read all tickets from the sprint's `tickets/` directory. Parse
frontmatter for `status`, `depends-on`, and `id`.

### 2. Order by Dependencies

Build a dependency graph from `depends-on` fields and produce a flat,
topologically-sorted list of tickets. Tie-breaks by ticket id ascending.

There are no execution groups. Tickets run one at a time.

### 3. Dispatch Programmer Agents Serially

For each ticket in dependency order:

1. Verify the ticket is `open` and all of its `depends-on` tickets are
   `done`. If not, stop and report the inconsistency.
2. Update the ticket status to `in-progress` via
   `update_ticket_status(path, "in-progress")`.
3. Invoke the programmer agent via the Agent tool with:
   - Path to the ticket file
   - Path to the ticket plan (if separate)
   - Sprint ID and ticket ID
   - Sprint branch name (the agent works on this branch directly)
   - Relevant architecture sections
4. Wait for the programmer agent to complete before moving on.
5. Verify `status: done` is set in the ticket's frontmatter.
6. Call `move_ticket_to_done(ticket_path)` where `ticket_path` is the
   relative path: `docs/clasi/sprints/NNN-slug/tickets/NNN-slug.md`.
   This is a team-lead responsibility — the programmer sets the
   frontmatter; the team-lead moves the file.
7. Continue with the next ticket.

**Do not** invoke a second programmer agent until the first has
returned. Do not create git worktrees. Do not branch off the sprint
branch.

### 4. Handle Failures

If a programmer agent fails, escalate to the stakeholder. Do not skip
the ticket and continue — the dependency chain assumes each prior
ticket is complete.

If a programmer agent leaves a ticket in `in-progress` (e.g. because
tests failed and the agent reported back without marking it done):
fix the issue in-process or with a follow-up programmer dispatch on
the same ticket. Either way, the ticket must end at `done` before
moving to the next one.

**Ticket completion is mandatory.** When a programmer completes a
ticket, its status must be set to `done` and `move_ticket_to_done`
called. There is no valid reason to leave a completed ticket in an
incomplete state. If the stakeholder says "leave it open", that means
leave the sprint open — the ticket itself must still be marked done.

### 5. Close Sprint

After all tickets are `done`:

1. Verify all tickets have `status: done`.
2. Run the full test suite on the sprint branch.
3. Present sprint summary to stakeholder.
4. Invoke the `close-sprint` skill.

## Output

- All tickets implemented and marked done
- All tests passing on sprint branch
- Sprint ready for review and close
