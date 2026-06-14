---
name: create-tickets
description: Breaks a sprint architecture into sequenced, numbered implementation tickets with dependency ordering
---

# Create Tickets Skill

This skill breaks a sprint's architecture document into actionable
implementation tickets. It is typically invoked by the sprint-planner
agent during planning, but can also be used standalone.

## Inputs

- Sprint's `architecture-update.md` (must exist)
- Sprint's `usecases.md` (must exist)

## Process

1. **Read artifacts**: Read the sprint's architecture document and use cases.
2. **Identify work units**: Break the Sprint Changes into coherent
   implementation units. Each unit should be completable in one focused
   session.
3. **Order by dependency**: Number tickets so foundation work comes
   before features. Record dependencies in each ticket's `depends-on` field.
4. **Create ticket files**: Use the `create_ticket` MCP tool. Each ticket
   gets `tickets/NNN-slug.md` with:
   - YAML frontmatter: id, title, status (open), use-cases, depends-on
   - Description and acceptance criteria (checkboxes)
   - Implementation plan: approach, files to create/modify, testing plan,
     documentation updates

   **Issue lifecycle:** When you call `create_ticket(sprint_id, title,
   issue=<filename>)`, the referenced issue file is physically moved from
   `.clasi/issues/` into `<sprint>/issues/` and its frontmatter is updated
   to `status: in-progress`. When all tickets referencing that issue are
   moved to done, `Issue.move_to_done()` is called automatically, which
   moves the file into `<sprint>/issues/done/`. No manual
   `move_issue_to_done` call is needed in the happy path.

   **Multi-ticket issue propagation:** When multiple tickets implement the
   same source issue, every ticket must carry the `issue:` back-reference.
   Use `create_ticket(issue=filename)` for the first ticket. For subsequent
   tickets, call `add_issue_ref(ticket_path, issue_filename)` after
   creation. Before returning from ticket creation, verify that every
   ticket working toward an issue has a non-empty `issue:` field.

5. **Propagate references**: Copy TODO and GitHub issue references to
   ticket frontmatter. List GitHub issues in the sprint doc's
   `## GitHub Issues` section.
6. **Verify coverage**: Every use case covered by at least one ticket.
   Every ticket traces to at least one use case.
7. **Verify sequencing**: No circular dependencies. Foundation before
   features.

## Ticket Format

File: `<sprint-dir>/tickets/NNN-slug.md`

```yaml
---
id: "NNN"
title: Short title
status: open
use-cases: [SUC-001, SUC-002]
depends-on: [NNN]
---
```

Followed by: description, acceptance criteria, and implementation plan.

## Output

Numbered ticket files in the sprint's `tickets/` directory, ready for
implementation.
