# read-paper

A flow-oriented skill for reading a single research paper with structured outputs and a human-readable summary.

## Purpose

Use this skill when the user wants help reading **one paper** in a disciplined, reviewable way.

This skill is for:
- reading a single paper in passes
- extracting structured artifacts instead of generic summaries
- tying major claims to evidence
- generating critique and reproduction-oriented notes
- producing a readable markdown summary layered on top of the structured artifacts

This skill is **not** for:
- full-topic literature reviews across many papers
- exhaustive academic search
- automatic citation crawling
- replacing human judgment with a one-shot summary

## When to use

Use this skill when the user provides or references:
- a local PDF path
- an arXiv link
- a local text or markdown extract
- copied paper content
- a request like “read this paper”, “help me understand this paper”, or “extract the main claims and evidence from this paper”

Prefer the separate literature-review workflow when the task is really about:
- comparing multiple papers
- building a topic map
- generating a survey or multi-paper synthesis

## Input assumptions for v1

This first version assumes the user has already provided the paper content directly or has supplied a path/link that can be used as input to a later ingestion layer.

The skill should be written as if it can accept:
- `paper_path`
- `paper_url`
- `paper_text`
- `paper_excerpt`
- optional `project_context`

But the skill should **not** pretend that every source can already be fully ingested automatically.

If the source content is incomplete, low-quality, or unavailable, say so explicitly and proceed only with what is actually available.

## Core workflow

This skill follows a three-pass reading workflow.

### Pass 1: Quick pass
Goal:
- identify what kind of paper this is
- identify what problem it addresses
- identify the claimed contributions
- determine whether it is worth deeper reading

Primary artifact:
- `quick_pass`

### Pass 2: Structured read
Goal:
- extract the method and experiment structure
- build claim-evidence links
- capture stated and inferred limitations

Primary artifacts:
- `method_card`
- `experiment_card`
- `claim_evidence_table`
- `limitations_card`

### Pass 3: Critique and reproducibility
Goal:
- identify support gaps, assumptions, confounds, and reproduction risks
- connect the paper to the user's active topic or project when relevant

Primary artifacts:
- `repro_notes`
- `critical_read_notes`
- `project_relevance`
- `uncertainty_summary`

## Required behavior

### 1. Do not flatten the workflow into one big summary
The skill must preserve the pass structure.

### 2. Do not hide uncertainty
If something is unclear from the available source, state that directly.

### 3. Do not invent evidence
Claims must not be presented as supported unless there is some identified source evidence.

### 4. Separate author statements from model interpretation
Where interpretation is involved, make the distinction clear.

### 5. Prefer structured outputs first
Human-readable prose is a rendering layer, not the source of truth.

## Output contract

The skill should produce two layers of output.

### Layer A: Structured artifacts
The core result should include:
- `paper_card`
- `quick_pass`
- `method_card`
- `experiment_card`
- `claim_evidence_table`
- `limitations_card`
- `repro_notes`
- `critical_read_notes`
- `project_relevance`
- `uncertainty_summary`

These should align with the files in `schemas/`.

### Layer B: Human-readable markdown summary
After producing the structured artifacts, the skill should render a readable markdown summary for the user.

Recommended sections:
- Paper at a glance
- What the paper claims
- How the method works
- What evidence supports the main claims
- Main limitations and risks
- Reproduction notes
- Why this paper matters for the current topic/project

## Schema and prompt usage

This skill should orchestrate the existing pass prompts and schemas.

### Prompt files
- `prompts/read-paper/pass1-quick-pass.md`
- `prompts/read-paper/pass2-method-and-results.md`
- `prompts/read-paper/pass3-critique-and-repro.md`

### Schema files
- `schemas/quick-pass.schema.json`
- `schemas/method-card.schema.json`
- `schemas/experiment-card.schema.json`
- `schemas/claim-evidence.schema.json`
- `schemas/limitations-card.schema.json`
- `schemas/repro-notes.schema.json`
- `schemas/critical-read-notes.schema.json`
- `schemas/project-relevance.schema.json`
- `schemas/paper-card.schema.json`

## Input routing guidance

### Pass 1 should prioritize
- title
- abstract
- introduction
- conclusion
- metadata
- section index

### Pass 2 should prioritize
- method or approach sections
- experiment or results sections
- figure/table captions
- selected evidence-relevant chunks

### Pass 3 should prioritize
- Pass 2 artifacts
- limitations/discussion sections
- selected source chunks related to major claims
- project context if provided

## What this skill should say when inputs are weak

If the paper content is partial, malformed, or low quality, the skill should say something like:
- “I can only assess the sections you provided.”
- “Evidence binding is partial because page/section structure is missing.”
- “This critique is limited by incomplete source coverage.”

## Example invocation patterns

- “Read this paper and tell me whether it is worth a deep read.”
- “Extract the main claims and evidence from this arXiv paper.”
- “Give me a structured read of this paper, including limitations and repro risks.”
- “Read this paper in three passes and tell me how it relates to my project.”

## Review checklist for outputs

When reviewing outputs from this skill, prioritize:
- `claim_evidence_table`
- `limitations_card`
- `repro_notes`
- `critical_read_notes`

A good output should be grounded, structured, and reviewable—not just fluent.
