---
name: architecture-authoring
description: Design and write architecture documents — initial architecture or sprint updates
---

# Architecture Authoring Skill

This skill guides writing architecture documents, whether an initial
architecture from scratch or a sprint update.

## Two Modes

### Mode 1: Initial Architecture

Design the system architecture from scratch when no architecture document
exists yet.

Given `.clasi/design/overview.md` and `.clasi/design/usecases.md`, produce
the first architecture document following steps 1-7 below.

### Mode 2: Sprint Architecture Update

Write a focused architecture planning document describing the structural
changes the sprint will introduce. This artifact is authored after use
cases are defined and **before tickets exist** — tickets are derived from
it, not the other way around. The guiding question throughout is: "Is this
description clear enough that tickets can be derived from it without
ambiguity?"

At authoring time the document is a structural plan; after the sprint
closes it accumulates as a historical record (an ADR at sprint
granularity). It is not merged back into the canonical architecture docs —
it stands on its own.

Given the sprint plan and current architecture, write
`architecture-update.md` with: Planned Changes, Rationale, Impact on
Existing Components, Migration Concerns.

### Revision naming and preservation

When an exception loop triggers an architecture revision, **never
overwrite `architecture-update.md`**. The original must be preserved as a
calibration signal for future sprint reviews.

Naming convention:
- First revision: write `architecture-update-r1.md`
- Subsequent revisions: increment the suffix — `architecture-update-r2.md`,
  `architecture-update-r3.md`, etc.
- The latest `-rN.md` file is the active planning artifact.
- The original `architecture-update.md` and all intermediate revisions
  remain in the sprint directory as historical record.

The team-lead and sprint-planner both reference this convention. The full
rule lives here; the sprint-planner agent carries only a brief
cross-reference.

## Steps

### 1. Understand the Problem
Read the overview, use cases, and (if updating) current architecture and
sprint plan.

### 2. Identify Responsibilities
List distinct responsibilities the system handles. Group related ones.
Separate those that change independently.

### 3. Define Subsystems and Modules
Map responsibility groups to modules. For each:
- **Purpose**: One sentence, no "and"
- **Boundary**: What is inside and outside
- **Use cases served**

### 4. Produce Diagrams
Required Mermaid diagrams:
1. **Component/Module Diagram** — subsystems as boxes, labeled edges
2. **Entity-Relationship Diagram** — entities, attributes, cardinality
3. **Dependency Graph** — module dependencies with labeled edges

Guidelines: 5-12 nodes, label every edge, one concern per diagram.

### 5. Complete the Document
Sections: Architecture Overview, Technology Stack, Module Design, Data
Model, Dependency Graph, Security Considerations, Design Rationale, Open
Questions, Sprint Changes.

Stay at module/subsystem level. No function signatures or column schemas.

### 6. Document Design Rationale
For significant decisions: Decision, Context, Alternatives, Why this
choice, Consequences.

### 7. Flag Open Questions
List anything ambiguous or requiring stakeholder input.

## Quality Checks

- Every module addresses at least one use case
- Every use case addressed by at least one module
- Each module passes cohesion test (one sentence, no "and")
- Dependency graph has no cycles
- Fan-out no greater than 4-5 without justification
- Mermaid diagrams included
- Document stays at module level
