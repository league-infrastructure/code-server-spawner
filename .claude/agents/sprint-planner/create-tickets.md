---
name: create-tickets
description: Breaks a sprint architecture into sequenced, numbered implementation tickets with dependency ordering
---

# Create Tickets Skill

This skill breaks a sprint's architecture document (especially the Sprint
Changes section) into actionable implementation tickets. The sprint-planner
handles ticket creation inline.

## Agent Used

**sprint-planner**

## Inputs

- Sprint's `architecture-update.md` (must exist in the sprint directory)
- Sprint's `usecases.md` (must exist in the sprint directory)

## Process

1. **Read artifacts**: Read the sprint's architecture document and use cases.
2. **Identify work units**: Break the Sprint Changes into coherent
   implementation units. Each unit should be completable in one focused
   session.
3. **Order by dependency**: Number tickets so that foundation work comes
   before features that depend on it. Record dependencies in each ticket's
   `depends-on` field.
4. **Create ticket files**: Write each ticket to the sprint's
   `tickets/NNN-slug.md` with YAML frontmatter (id, title, status,
   use-cases, depends-on, github-issue) and body (description, acceptance
   criteria, implementation notes). Ticket numbering is per-sprint
   (starts at 001).

   **Issue lifecycle:** When you call `create_ticket(sprint_id, title,
   issue=<filename>)`, the referenced issue file is physically moved from
   `clasi/issues/` into `<sprint>/issues/` and its frontmatter is updated
   to `status: in-progress`. When all tickets referencing that issue are
   moved to done, `Issue.move_to_done()` is called automatically, which
   moves the file into `<sprint>/issues/done/`. No manual
   `move_issue_to_done` call is needed in the happy path.

5. **Propagate issue and GitHub issue references**: When creating tickets
   from issues, set the ticket's `issue` frontmatter field to the issue
   filename (e.g., `issue: "my-idea.md"`). This creates the back-link
   from ticket to issue. Also copy `github-issue` if present. After all
   tickets are created, collect all `github-issue` references from the
   sprint's tickets and list them in the sprint doc's `## GitHub Issues`
   section using the format `owner/repo#N`.
6. **Verify coverage**: Every use case must be covered by at least one
   ticket. Every ticket must trace to at least one use case.
7. **Verify sequencing**: No circular dependencies. Foundation before
   features.

## Output

Numbered ticket files in the sprint's `tickets/` directory, ready for
implementation.
