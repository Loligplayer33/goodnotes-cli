# Documentation maintenance

Markdown in this repository is the source. MkDocs adds local navigation and search;
there is no separate copy of the content and no automatic public deployment.

## Choose where a change belongs

| Reader's question | Owner |
| --- | --- |
| What is this tool, and where do I start? | Root `README.md`, then [getting started](getting-started.md) |
| How do I finish a notebook task? | [Everyday tasks](usage.md) |
| Which image/text source do I need? | [Images, PDFs, and handwriting](pdf-and-handwriting.md) |
| What access does a runner need? | [Access and refresh](access.md) |
| What does a result mean? | [Results and output](output.md); [agent guide](agent-guide.md) for agent decisions |
| What flags exist? | [Generated command reference](commands.md) |
| Why did a command fail? | [Troubleshooting](troubleshooting.md) |
| How does the implementation stay safe? | [Architecture](architecture.md) and [robustness](robustness.md) |
| What do the tests prove? | [Development verification](verification.md) |

Keep operating steps in task guides and detailed invariants in technical docs.
Link to the authoritative explanation rather than repeating it on every page.
The [documentation index](index.md) and `mkdocs.yml` define the entry points.

## Preview and check

Run from the checkout:

```sh
uv sync --locked --group docs
uv run --locked python scripts/docs.py --check
uv run --locked --group docs mkdocs build --strict
uv run --locked --group docs mkdocs serve
```

The preview command prints a local URL and reloads on edits. The built site stays
under the ignored `.local/` directory. The Markdown files also work on GitHub.
The site renders Contributing from the root file at build time. Links to code,
tests, and repository policy files point to GitHub.

After changing command signatures or help, regenerate the reference:

```sh
uv run --locked python scripts/docs.py
```

Do not edit `docs/commands.md` by hand. The generator reads command metadata
without loading a library. `--check` catches a stale reference, broken local
Markdown links/anchors, and navigation omissions. CI also builds MkDocs in strict
mode. External URL availability is not checked in CI.

## Write a useful task recipe

1. Name the outcome, such as "Export selected pages".
2. State prerequisites that change the command choice, such as native runtime or prior PDF registration.
3. Show a short command sequence with placeholders clearly explained.
4. Tell the reader what to open or inspect and how to recognize an incomplete result.
5. Link to limits and recovery steps instead of adding a long explanation.

Use synthetic names and content. Keep real notebooks, paths, IDs, emails, and note
text out of examples and build output. Test library workflows only with generated
fixtures or a pre-existing private copy handled under [repository guidance](../AGENTS.md).

## Keep claims tied to evidence

| Change | Check and update |
| --- | --- |
| CLI options/defaults/bounds | Help, `schema COMMAND`, generated reference, task examples |
| Output profile or provenance | `output.py`, output tests, results guide and agent guide |
| Capture/locks/safety/storage | Implementation, regression tests, architecture and robustness |
| Native decoding/rendering | Generated native journeys, capability table, exact uncertainty and warnings |
| Verification/release workflow | Scripts and CI; avoid static pass counts or unverified repository settings |

Generated tests verify declared synthetic cases. They do not establish complete
Goodnotes compatibility or native rendering fidelity. User testing should cover
both finding a recipe and judging its output, not just whether the command exits 0.

## Design references

These docs use a small task/reference/explanation split rather than requiring a
separate page for every documentation category.

- [Write the Docs](https://www.writethedocs.org/guide/writing/beginners-guide-to-docs/) informed the short purpose statement, installation path, small examples, and separate contributor route.
- [uv's guides and reference](https://docs.astral.sh/uv/) informed the separation of task instructions, reference, and internals.
- [GitHub CLI's command manual](https://cli.github.com/manual/gh_pr_create) informed generated option lookup beside concrete examples.
- [Git's reference index](https://git-scm.com/docs) informed grouping commands by task and linking deeper technical material.
- The [documentation-writer skill](https://github.com/github/awesome-copilot/blob/main/skills/documentation-writer/SKILL.md) informed the audience/purpose split. It is guidance here, not a project dependency.
