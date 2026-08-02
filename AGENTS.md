# AGENTS.md

## Project identity

This repository is the Personal Knowledge & Agent System（PKAS）at E:\codex-kb.

## Working rules

- Treat E:\codex-kb as the only writable project boundary for this project unless the user explicitly authorizes another source or destination.
- Preserve imported source material. Derived text, normalized copies, indexes, and summaries must live separately from originals.
- Use Chinese-first documentation and UTF-8 encoding.
- Prefer minimal, reviewable changes and focused validation.
- Check git status before edits once the repository is initialized.
- Do not import, scan, upload, decrypt, or analyze personal data without an explicitly confirmed source path and scope.
- Do not store passwords, tokens, private keys, recovery codes, or unredacted production secrets.
- Treat third-party chat content as restricted by default.
- Every derived knowledge item must retain provenance, timestamps, privacy classification, confidence, and review status.
- Search for an existing canonical entry before creating a new one.
- Default to read-only retrieval. Long-term profile changes, distillation promotion, deletion, external publishing, and high-impact actions require explicit approval.
- Do not describe candidate personality observations as established facts.

## Validation

- Validate schemas and indexes with focused checks.
- Verify that imported sources remain byte-identical when the workflow promises preservation.
- Verify search results include resolvable source references.
- Verify restricted records are excluded from ordinary retrieval.
- Report exact artifact paths, checks performed, and remaining risks.
