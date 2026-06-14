---
name: dispatch-subagent
description: Controller/worker pattern for dispatching isolated subagents via the Agent tool with curated context, directory scope, and dispatch logging
---

# Dispatch Subagent Skill

This skill defines the controller/worker dispatch pattern. The
controller curates context, declares a directory scope, logs the full
dispatch prompt, and sends a fresh subagent to do the work.

## Process

### 1. Determine task scope

Identify:
- Which agent to dispatch (by tier and role)
- What directory the subagent may write to (`scope_directory`)
- What files and instructions the subagent needs

### 2. Curate context

Select only the files and instructions relevant to the task. Follow
`instructions/subagent-protocol` for include/exclude rules.

**Include:**
- Ticket description and acceptance criteria (if executing a ticket)
- Ticket plan (approach, files to modify)
- Content of source files the subagent will read or modify
- Relevant architecture decisions
- Applicable coding standards and testing instructions

**For raw-text delegation** (e.g., TODO creation, sprint planning):
- Pass the stakeholder's raw words verbatim
- Provide file references (TODO paths, overview path) instead of
  pre-digested content
- Let the subordinate agent make structuring decisions

**Exclude:**
- Controller's conversation history
- Other tickets in the sprint
- Debug logs from prior attempts
- Full directory listings
- Sprint-level planning documents (unless the task is planning)

### 3. Compose the prompt

Include in the subagent prompt:
- The curated context
- The scope constraint: "You may only create or modify files under
  `<scope_directory>`. You may read files from any location."
- The specific task and acceptance criteria
- Instructions for how to report results

### 4. Log the dispatch (MANDATORY)

**Before sending the prompt, you MUST call `log_subagent_dispatch`.**
This is not optional. Every dispatch at every tier must be logged. Pass:
- `parent_agent`: your agent name (e.g., "sprint-executor")
- `child_agent`: the subagent being dispatched (e.g., "code-monkey")
- `sprint_id`: the sprint ID (if applicable)
- `ticket_id`: the ticket ID (if applicable)
- `prompt`: the full prompt text being sent

This creates an audit trail with a dispatch ID you will use in step 6.

**If `log_subagent_dispatch` is unavailable or fails, STOP and report
the failure. Do not dispatch without logging.**

### 5. Dispatch

Send the subagent via the Agent tool with the composed prompt.

### 6. Log the result and review (MANDATORY)

When the subagent returns:
- **First, call `update_dispatch_log`** with the dispatch ID from step 4,
  the outcome (success/failure), a summary of what the subagent
  produced (files modified, key results), and the subagent's **response
  text** (via the `response` parameter). This preserves both sides of
  the conversation in the log. This is mandatory — do not skip it.
- Read the output
- Check that the work meets the task requirements
- If issues found, compose a new prompt with feedback and re-dispatch
  (max 2 retries, then escalate to the controller's parent). **Log
  every re-dispatch** with `log_subagent_dispatch` and
  `update_dispatch_log` as well.

## Notes

- The controller never writes code directly — all implementation is
  delegated to subagents.
- Each subagent starts with fresh context. It does not inherit the
  controller's conversation.
- Scope enforcement is prompt-level + rule-level. Path-scoped rules
  reinforce the constraint when the subagent accesses files.
