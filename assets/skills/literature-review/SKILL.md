# literature-review

A flow-oriented skill for topic-first multi-paper review with structured outputs and a human-readable synthesis.

## Purpose

Use this skill when the user wants help reviewing **multiple papers around one topic** in a disciplined, reviewable way.

This skill is for:
- topic-first multi-paper review
- scoping a research question before synthesis
- aggregating a paper set with explicit coverage state
- comparing papers using structured axes instead of generic survey prose
- generating a readable markdown review layered on top of structured artifacts

This skill is **not** for:
- reading one paper in depth as a Layer 1 workflow
- exhaustive academic search
- automatic citation crawling
- pretending missing Layer 1 reads were completed automatically

## When to use

Use this skill when the user provides or references:
- a `topic`
- a `research_question`
- `seed_papers`
- optional project context
- optional existing Layer 1 artifacts for some papers
- a request like “compare these papers”, “help me scope a literature review”, or “build a topic-first review from these seed papers”

Prefer the separate `read-paper` workflow when the task is really about:
- deeply reading one paper
- building Layer 1 artifacts for a single source before cross-paper comparison

## Input assumptions for v1

This first version assumes the user has already provided:
- `topic`
- `research_question`
- `seed_papers`
- optional `project_context`
- optional Layer 1 artifacts when available

The skill should consume Layer 1 artifacts when they exist.
It should **not** pretend that every paper already has full Layer 1 coverage.
It does **not** perform missing Layer 1 reads in v1.

If the seed set is weak, coverage is partial, or Layer 1 artifacts are missing, say so explicitly and proceed only with what is actually available.

## Core workflow

This skill follows a four-stage Layer 2 workflow.

### Stage 1: Topic scoping
Goal:
- define the topic boundary
- clarify the research question
- set initial comparison axes
- flag visible scope risks

Primary artifact:
- `topic_scope`

### Stage 2: Aggregation
Goal:
- define the current review `paper_set`
- record explicit coverage state per paper
- identify missing Layer 1 artifacts and deeper-read candidates

Primary artifact:
- `paper_set`

### Stage 3: Comparison
Goal:
- build a reviewable `comparison_matrix`
- keep missing evidence visible
- avoid flattening papers across unequal coverage tiers

Primary artifact:
- `comparison_matrix`

### Stage 4: Synthesis
Goal:
- derive taxonomy and evidence patterns
- record gaps, disagreements, and review limits
- propose a reading order and next actions

Primary artifact:
- `literature_review`

## Required behavior

### 1. Keep the workflow topic-first
The skill must start from `topic`, `research_question`, and `seed_papers`.

### 2. Do not hide coverage state
Every paper should remain visibly classified as `seed_only`, `partial_layer1`, or `full_layer1`.

### 3. Do not invent missing evidence
Missing Layer 1 coverage must not be presented as evidence-backed comparison.

### 4. Consume Layer 1 artifacts when available
Where Layer 1 artifacts exist, use them as the preferred basis for grounded comparison.

### 5. Do not perform missing Layer 1 reads in v1
The skill may recommend deeper Layer 1 reads, but it must not pretend those reads were completed during the Layer 2 workflow.

### 6. Prefer structured outputs first
Human-readable prose is a rendering layer, not the source of truth.

## Output contract

The skill should produce two layers of output.

### Layer A: Structured artifacts
The core result should include:
- `topic_scope`
- `paper_set`
- `comparison_matrix`
- `literature_review`

`paper_set` is not just a named top-level artifact.
It should be backed by the standalone `schemas/paper-set.schema.json` contract, including the row-level coverage-state fields that keep each paper's comparison readiness and Layer 1 status reviewable.

The aggregate `literature_review` should preserve:
- `topic_scope`
- `paper_set`
- `comparison_matrix`
- `taxonomy`
- `evidence_patterns`
- `gaps_and_disagreements`
- `recommended_reading_order`
- `review_limits`
- `next_actions`

These structured artifacts should align with the files in `schemas/` and assemble into:
- `literature-review.json`

### Layer B: Human-readable markdown review
After producing the structured artifacts, the skill should render a readable markdown review for the user.

Recommended sections:
- Topic at a glance
- Research question
- Scope and boundaries
- Included papers and coverage state
- Comparison highlights
- Taxonomy and evidence patterns
- Gaps, disagreements, and review limits
- Recommended reading order
- Next actions

The canonical human-readable rendering is:
- `literature-review.md`

## Schema and prompt usage

This skill should orchestrate the full Layer 2 workflow across topic scoping, aggregation, comparison, and synthesis.

The staged contract is:
- `topic + research_question + seed_papers`
- `topic_scope`
- `paper_set`
- `comparison_matrix`
- `literature_review`
- `literature-review.json`
- `literature-review.md`

Aggregation remains an explicit stage in this workflow even though there is no separate aggregation prompt file in v1.
The skill should make that stage visible by assembling `paper_set` from the scoped topic, `seed_papers`, and any available Layer 1 artifacts before running comparison and synthesis.

### Prompt files
- `prompts/literature-review/topic-scope.md`
- `prompts/literature-review/comparison-matrix.md`
- `prompts/literature-review/synthesis.md`

### Schema files
- `schemas/topic-scope.schema.json`
- `schemas/paper-set.schema.json`
- `schemas/comparison-matrix.schema.json`
- `schemas/literature-review.schema.json`

## Coverage-state guidance

Use `seed_only` when a paper is represented by seed input or lightweight metadata only.
Use `partial_layer1` when some Layer 1 artifacts exist but coverage is incomplete.
Use `full_layer1` when a durable Layer 1 package exists and can support grounded comparison.

The skill should keep these tiers visible in `paper_set`, `comparison_matrix`, and `review_limits`.
It should not smooth over unequal evidence depth across the paper set.

## What the skill should say when inputs are weak

If topic definition is underspecified, seed coverage is weak, or Layer 1 artifacts are missing, the skill should say something like:
- “This review is limited by the current seed-paper set.”
- “Cross-paper comparison is partial because some papers only have seed-level coverage.”
- “I can recommend which papers need deeper Layer 1 reads next, but I am not performing those reads in this workflow.”

## Example invocation patterns

- “Scope a literature review for this topic and research question.”
- “Compare these seed papers and show me where coverage is still weak.”
- “Build a topic-first review from these papers using any Layer 1 artifacts we already have.”
- “Give me a structured literature review with next reads and open gaps.”

## Review checklist for outputs

When reviewing outputs from this skill, prioritize:
- whether `topic_scope` is specific enough to constrain the review
- whether `paper_set` keeps `seed_only`, `partial_layer1`, and `full_layer1` visible
- whether `comparison_matrix` keeps missing evidence explicit
- whether `review_limits` and `next_actions` stay honest about remaining work

A good output should be grounded, structured, and reviewable—not just fluent.
