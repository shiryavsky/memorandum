<!--
  Thanks for the PR! Keep this template — it's deliberately short.
  Delete the comments before submitting; leave the headings.
-->

## What this changes

<!-- One paragraph. Imperative tone. What does this do and why does it do it?
     Skip the implementation detail; that's in the diff. -->

## Why

<!-- The motivation. Link the issue if there is one (`Fixes #N`). If there
     isn't, briefly describe the workflow this unblocks. -->

## Test plan

<!-- How did you verify this works? At minimum: `pytest tests/` is green.
     For UI / dashboard changes, paste the relevant `--once` snippet.
     For connector changes, describe what you ran against a real source. -->

- [ ] `pytest tests/` passes
- [ ] `flake8 . --select=E9,F63,F7,F82` is clean
- [ ] Touched modules have docstrings / inline comments where the WHY isn't obvious
- [ ] Docs updated (`README.md` / `AGENTS.md` / `connectors/CONTRIBUTING.md`) if user-visible
- [ ] No real tokens / chat names / personal data in tests or example config

## Notes / open questions (optional)

<!-- Anything you want the reviewer to weigh in on, or follow-ups you want
     to file separately rather than bundle here. -->
