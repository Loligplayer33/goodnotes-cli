# Install and read your first page

This guide takes you from a checkout to a page's text. Native handwriting images
are an optional next step.

## 1. Install and check without loading notes

You need Git, [uv](https://docs.astral.sh/uv/getting-started/installation/), and Python 3.11 or newer.
Run these commands in a terminal:

```sh
git clone https://github.com/Loligplayer33/goodnotes-cli.git
cd goodnotes-cli
uv sync --locked
uv run --locked goodnotes --help
uv run --locked goodnotes schema read
```

`uv sync` creates the project environment. Help lists commands; `schema read`
prints the read command's JSON contract. Neither opens a library.
If you already have the checkout, start with `uv sync --locked` there.

## 2. Prepare the library copy

This step reads your library files and stores a validated local copy. It needs
source read access and cache write access. It does not write to Goodnotes.

Choose one source:

| Source | Command |
| --- | --- |
| Goodnotes on this Mac, discovered automatically | `uv run --locked goodnotes sync` |
| An existing Library folder, including on Linux | `uv run --locked goodnotes sync --library /path/to/Library` |

The explicit folder must contain `Databases/` and `Attachments/`.
On macOS, respond to any system privacy prompt for your terminal or agent app.
The CLI cannot grant that permission.

A successful sync returns `status: "ready"` or `"unchanged"` and a `snapshot_id`.
Later reads use that copy. Refresh again after edits you want to inspect.
The default cache is `~/.cache/goodnotes-cli`, or under `XDG_CACHE_HOME` when set.

For development, use generated fixtures or a pre-existing private copy.
Do not run the automatically discovered live-library sync as a development check.

## 3. Find a notebook and read a page

```sh
uv run --locked goodnotes --text ls
uv run --locked goodnotes --text ls --folder "Courses"
uv run --locked goodnotes --text pages "Course Notes" --limit 20
uv run --locked goodnotes --text read "Course Notes" --page 1
```

Replace the example folder and notebook names with values returned by `ls`.
Skip the folder command if your notebook is at the root. If a name is ambiguous,
use its returned full `path` or `id`. Follow `next_offset` when a list has more results.

`read` returns a `pages` array. Each page's `parts` labels text as `pdf`,
`handwriting`, or `textbox`. Check `warnings` and `truncated` before interpreting
missing text. A page can contain visible ink without stored handwriting text.

## 4. Choose your next task

| Goal | Next step |
| --- | --- |
| Read only printed slide text | `uv run --locked goodnotes read "Course Notes" --page 3 --source pdf` |
| Search a phrase across the notebook | [Search recipe](usage.md#search-notes-and-pdf-backgrounds) |
| View a clean page background | `uv run --locked goodnotes render "Course Notes" --page 3` |
| Let an agent inspect handwriting and slide text | [Set up native rendering](pdf-and-handwriting.md#native-ink-without-manual-pdf-exports), then [request a bundle](usage.md#inspect-a-page-with-an-agent) |
| Use Goodnotes' own page rendering | [Register an exported PDF](pdf-and-handwriting.md#use-a-pdf-exported-by-goodnotes) |

Images returned without `--output` are temporary. Open them promptly or
[save a persistent output](output.md#temporary-and-saved-files).
If a command fails, use [troubleshooting](troubleshooting.md).
