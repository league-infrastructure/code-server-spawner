---
name: plan-sprint
description: Creates sprint plans using a two-phase model -- roadmap (batch, lightweight) and detail (full artifacts, pre-execution)
---

# Plan Sprint Skill

This skill creates sprint plans using a two-phase model:

- **Phase 1 -- Roadmap**: Batch planning. Multiple sprints can be planned
  in one session. Produces a lightweight `sprint.md` only (goals, scope,
  TODO references). No branches created.

- **Phase 2 -- Detail**: One sprint at a time. Produces full planning
  artifacts: `usecases.md`, `architecture-update.md`, and tickets. Runs
  architecture review. No branches created.

Branches are created later via `acquire_execution_lock`, not during
planning. All planning happens on main.

## Agent Used

**sprint-planner** (orchestrates all phases inline — architecture,
review, and ticket creation)

## Inputs

- Stakeholder conversation describing the work to be done
- `.clasi/brief.md` or `.clasi/design/overview.md` (must exist)
- `.clasi/design/usecases.md` (must exist, or overview covers use cases)

## Critical Rule

**DO NOT create tickets** during roadmap mode or in steps 1-10 of detail
mode. Tickets are only created in step 12, after the sprint has advanced
to the `ticketing` phase. The `create_ticket` MCP tool will reject
attempts to create tickets before that phase. Follow the phases in order.

**DO NOT create a git branch** during planning. Branches are created
at execution time by `acquire_execution_lock`.

## Phase 1: Roadmap Mode

The team-lead dispatches to sprint-planner in roadmap mode for batch
planning. Multiple sprints can be planned in a single session.

### Roadmap Process

1. **Determine sprint number**: Check `.clasi/sprints/` and
   `.clasi/sprints/done/` for existing sprints. The new sprint gets the
   next sequential number (NNN format: 001, 002, ...).

2. **Mine the issues directory**: Scan `.clasi/issues/` for ideas relevant
   to the sprint. Discuss relevant issues with the stakeholder.

   For each issue claimed by this sprint, set `sprint: "NNN"` in the
   issue's YAML frontmatter (using `write_artifact_frontmatter`).

3. **Create sprint directory**: Use the `create_sprint` MCP tool. This
   creates the directory structure and registers the sprint.

4. **Write sprint.md**: Create a lightweight `sprint.md` with:
   - Frontmatter: `status: roadmap`
   - Goals and feature scope
   - TODO references
   - No tickets, no architecture, no use cases

5. **Repeat** for additional sprints as needed.

### Roadmap Output

- Sprint directory `.clasi/sprints/NNN-slug/` with `sprint.md`
- Sprint `sprint.md` status set to `roadmap`
- No branch created
- No tickets created

## Phase 2: Detail Mode

The team-lead dispatches to sprint-planner in detail mode when a
roadmap sprint is ready for execution. Detail mode fills in full
planning artifacts for one sprint at a time.

### Detail Process

1. **Verify sprint exists**: The sprint directory and roadmap `sprint.md`
   should already exist from Phase 1.

2. **Update sprint.md**: Update the existing `sprint.md` with full
   details. Set frontmatter `status: planning_docs`.

3. **Write usecases.md**: Sprint-level use cases (SUC-NNN).

4. **Write architecture-update.md**: Lightweight architecture update.
   The architect fills this in to describe what changed in this sprint,
   why, and the impact on existing components.

5. **Advance to architecture-review**: Call `advance_sprint_phase` to
   move from `planning-docs` to `architecture-review`.

6. **Architecture review**: Delegate to the architecture-reviewer agent.
   The reviewer reads the sprint plan, architecture document, and relevant
   existing code, then produces a review (APPROVE / APPROVE WITH CHANGES /
   REVISE).
   - If REVISE: update the sprint document and re-review.
   - If APPROVE WITH CHANGES: note the changes for ticket creation.
   - Call `record_gate_result` with gate `architecture_review` and result
     `passed` or `failed`.

7. **Advance to stakeholder-review**: If architecture review passed,
   call `advance_sprint_phase` to move to `stakeholder-review`.

8. **Breakpoint (conditional)**: Check the sprint's
   `architecture-update.md` for a `## Open Questions` section.
   - If open questions **exist**: skip this breakpoint and proceed to
     step 9 (which resolves them interactively via `AskUserQuestion`).
   - If **no open questions** exist: present an `AskUserQuestion` to
     confirm continuation.

9. **Resolve open questions**: If open questions exist in the
   architecture document:
   - Parse each numbered question into a separate `AskUserQuestion` call.
   - Provide 2-4 concrete options where possible.
   - After all questions are answered, replace `## Open Questions` with
     `## Decisions` listing each question and answer.

10. **Stakeholder review gate**: Present the sprint plan and architecture
    review to the stakeholder. Use `AskUserQuestion`:
    - "Approve sprint plan" (recommended)
    - "Request changes"
    - Call `record_gate_result` with gate `stakeholder_approval`.

11. **Advance to ticketing**: If stakeholder approved, call
    `advance_sprint_phase` to move to `ticketing`.

11b. **Split partial-scope issues**: Before creating tickets, review
    each issue claimed by this sprint. If an issue covers more work than
    fits in this sprint, call `split_issue(filename, new_filename,
    new_title, new_body)` to carve out the in-scope piece. The new file
    is a sibling of the original in the same directory. Then reference
    `new_filename` in the ticket's `issue` field. The original issue
    retains the out-of-scope portion for a future sprint.

12. **Create tickets**: Create tickets inline. Tickets are created in
    the sprint's `tickets/` directory with per-sprint numbering (001, 002, ...).

12b. **Update sprint.md ticket table**: After all tickets are created,
     update the `## Tickets` section in `sprint.md` with a summary table
     listing each ticket's number, title, and depends-on values, in
     dependency order. Tickets execute serially in the order listed.

13. **Acquire execution lock**: Call `acquire_execution_lock` to claim
    the lock and create the sprint branch. Then call
    `advance_sprint_phase` to move to `executing`.

14. **Set sprint status**: Update the sprint document status to `active`.

15. **Confirm before execution**: Present the list of tickets to the
    stakeholder. Use `AskUserQuestion`:
    - "Start executing tickets" (recommended)
    - "Review tickets first"

    **Do NOT ask again between individual tickets** -- once execution
    starts, tickets proceed without interruption.

### Detail Output

- Sprint directory with full planning documents (sprint.md, usecases.md,
  architecture-update.md)
- Sprint `sprint.md` status set to `active`
- Sprint branch `sprint/NNN-slug` created (via acquire_execution_lock)
- Sprint phase advanced to `executing` in the state database
- Execution lock acquired for this sprint
- Tickets in `tickets/` ready for execution
