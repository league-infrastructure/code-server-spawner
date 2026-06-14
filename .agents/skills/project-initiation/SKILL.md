---
name: project-initiation
description: Bootstrap a new project from a stakeholder's specification — produces overview, specification, and use cases
---

# Project Initiation Skill

Process a written specification into structured project documents that
all other processes will reference throughout the project lifecycle.

## When to Use

At the start of a new project when the stakeholder has provided a
written specification. There are no existing `overview.md`,
`specification.md`, or `usecases.md` documents yet.

## Inputs

- A path to a written specification file from the stakeholder

## Process

1. **Dispatch to the sprint-planner agent** via the Agent tool with:
   - The specification file path
   - Instruction to write all three documents to `.clasi/design/`

   The sprint-planner agent writes all three documents. Do not write
   them yourself.

2. **Await completion.** The sprint-planner returns when all three
   documents are written.

3. **Report the result** to the stakeholder — confirm the files
   created and any key decisions made.

## Documents the Sprint-Planner Produces

**`.clasi/design/overview.md`** — A one-page summary of the
project. An elevator pitch for quick context. It is additive, NOT a
replacement for the specification.

**`.clasi/design/specification.md`** — The full feature
specification, preserving ALL stakeholder detail. Exact messages,
behavior rules, edge cases, test expectations — if the stakeholder
wrote it, it MUST survive. Reorganize for clarity, but do not lose
information. Do not summarize, paraphrase, or omit.

**`.clasi/design/usecases.md`** — Numbered use cases (UC-001,
UC-002, etc.) extracted from the specification. Each use case has: ID,
title, actor, preconditions, main flow, postconditions, and error flows.

## Critical Rule

**Do not write documents yourself.** Dispatch to the sprint-planner
agent. Your role is orchestration, not authorship.

## Output

Three documents in `.clasi/design/`: overview.md, specification.md, usecases.md.
