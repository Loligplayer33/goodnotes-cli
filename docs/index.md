# Goodnotes CLI documentation

Start with [installation and your first page](getting-started.md).
For later visits, choose a task below.

## Use the CLI

| I want to… | Read |
| --- | --- |
| Find a notebook, read text, search, or export pages | [Everyday tasks](usage.md) |
| View handwriting or use an app-exported PDF | [Images, PDFs, and handwriting](pdf-and-handwriting.md) |
| Set up a restricted agent or continuous refresh | [Access and refresh](access.md) |
| Interpret JSON, warnings, cursors, or temporary files | [Results and output](output.md) |
| Use the CLI efficiently as an agent | [Agent guide](agent-guide.md) |
| Check a command's flags and defaults | [Command reference](commands.md) |
| Recover from an error | [Troubleshooting](troubleshooting.md) |

## Develop the CLI

| I want to… | Read |
| --- | --- |
| Set up the project and run checks | [Contributing](../CONTRIBUTING.md) |
| Understand package boundaries and the data lifecycle | [Architecture](architecture.md) |
| Check failure behavior and safety guarantees | [Robustness](robustness.md) |
| Verify study workflows or compare rendering references | [Development verification](verification.md) |
| Find the tests behind a workflow | [Verification test map](verification-tests.md) |
| Prepare and publish a release | [Releases](releases.md) |
| Edit, preview, and validate these docs | [Documentation maintenance](documentation.md) |

## Command conventions

Examples run from the checkout with `uv run --locked goodnotes`.
Replace `"Course Notes"`, paths, and snapshot IDs with your own values.
Use a full notebook path or ID if a name is ambiguous.
Page numbers start at **1** and follow the notebook's current order.

Global options go **before** the command:

```sh
uv run --locked goodnotes --text read "Course Notes" --page 3
```

`--text` indents JSON. Omit it for compact JSON. It does not turn results into prose.
