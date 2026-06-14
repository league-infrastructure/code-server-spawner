---
name: plan-sprint
description: Creates sprint plans using a two-phase model — roadmap (batch, lightweight) and detail (full artifacts, pre-execution)
---

# Plan Sprint Skill

This skill creates sprint plans using a two-phase model:

- **Phase 1 — Roadmap**: Batch planning. Multiple sprints can be planned
  in one session. Produces a lightweight `sprint.md` only (goals, scope,
  TODO references). No branches created.

- **Phase 2 — Detail**: One sprint at a time. Produces full planning
  artifacts via the sprint-planner agent: `usecases.md`,
  `architecture-update.md`, and tickets. Runs architecture review inline.

Branches are created later via `acquire_execution_lock`, not during
planning. All planning happens on main.

## Inputs

- Stakeholder conversation describing the work to be done
- `docs/clasi/design/overview.md` (must exist)

## Critical Rules

**DO NOT create tickets** during roadmap mode or before the sprint has
advanced to the `ticketing` phase. The `create_ticket` MCP tool will
reject attempts before that phase.

**DO NOT create a git branch** during planning. Branches are created
at execution time by `acquire_execution_lock`.

## Phase 1: Roadmap Mode

For batch roadmap planning of multiple sprints.

### Process

1. **Determine sprint number**: Check `docs/clasi/sprints/` and
   `docs/clasi/sprints/done/` for existing sprints. Next sequential number.

2. **Mine the TODO directory**: Scan `docs/clasi/todo/` for relevant ideas.
   Discuss with the stakeholder.

3. **Create sprint directory**: Use the `create_sprint` MCP tool.

4. **Write sprint.md**: Lightweight plan with:
   - Frontmatter: `status: roadmap`
   - Goals and feature scope
   - TODO references
   - No tickets, no architecture, no use cases

5. **Repeat** for additional sprints as needed.

## Phase 2: Detail Mode

When a roadmap sprint is ready for execution. Invoke the sprint-planner
agent via the Agent tool to fill in full planning artifacts.

### Process

1. **Verify sprint exists**: Sprint directory and roadmap `sprint.md`
   should already exist from Phase 1.

2. **Invoke sprint-planner agent**: Use the Agent tool to dispatch the
   sprint-planner agent with:
   - Sprint ID and directory path
   - Sprint goals and TODO references
   - Path to `docs/clasi/design/overview.md`
   - Path to current architecture

   The sprint-planner handles architecture, architecture review, and
   ticket creation inline — no sub-dispatches needed.

   2a. **Split partial-scope issues**: If any issue in scope covers work
       that cannot all fit in this sprint, call `split_issue` first to
       carve out the in-scope piece as a separate issue file. The split
       issue will be a sibling of the original in the same directory.
       Then call `create_ticket(issue=<split-filename>)` to bring the
       in-scope piece into the sprint. The remainder stays in the pool
       or sprint for a future sprint.

3. **Stakeholder review**: Present the completed plan to the stakeholder.
   Record stakeholder approval gate (`record_gate_result`).

4. **Acquire execution lock**: Call `acquire_execution_lock` to claim
   the lock and create the sprint branch. Advance to `executing`.

5. **Set sprint status**: Update sprint doc status to `active`.

## Output

- Sprint directory with full planning documents
- Sprint branch created (via acquire_execution_lock)
- Tickets in `tickets/` ready for execution
