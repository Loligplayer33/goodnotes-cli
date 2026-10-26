# Access and refresh

Sync needs access to the source Library. Later readers can use only the validated
copy. The CLI does not grant permissions or change your agent runner's policy.

## Read access for an agent

Choose a cache location once and use it for setup and reading:

```sh
uv run --locked goodnotes --data-dir /path/to/cli-cache sync --library /path/to/Library
.venv/bin/goodnotes --data-dir /path/to/cli-cache doctor
.venv/bin/goodnotes --data-dir /path/to/cli-cache ls
```

Run setup in a process with source read and cache write access. Give the reader
access to the installed executable and prepared cache through its runner.
The direct `.venv/bin/goodnotes` command avoids asking a package runner to update
the environment. Use an absolute executable path when running outside the checkout.

| Operation | Read access | Write access |
| --- | --- | --- |
| `sync`, `watch` | Source Library | Managed cache |
| `ls`, `tree`, `info`, `pages`, `read`, `search`, `scan-pdfs`, ordinary `doctor` | Validated cache or marked snapshot, including existing locks where required | None for the read view |
| Native reconstruction | Validated copy plus runtime | Private disposable scratch |
| `render`, `bundle` without `--output` | Validated copy | Separate artifact cache |
| Visual command with explicit `--output`, or `export` | Validated copy | New output destination; native output also needs scratch |
| `attach-pdf` | Validated copy and exported PDF | Registration sidecar storage |

For a reader with a read-only cache, supply a new writable output destination:

```sh
.venv/bin/goodnotes --data-dir /path/to/cli-cache bundle "Course Notes" --pages 3 --output /tmp/agent-page
```

That bundle still needs the native runtime and scratch. Use `--layer background`
when only clean backgrounds are needed. The CLI's cache is private to its system
user; these steps do not grant other users access.

## Select a cache or named copy

| Setting | Selection |
| --- | --- |
| `--data-dir PATH` | Managed cache; overrides `GOODNOTES_DATA_DIR` |
| `GOODNOTES_DATA_DIR` | Overrides the default cache location |
| Default | `$XDG_CACHE_HOME/goodnotes-cli`, or `~/.cache/goodnotes-cli` |
| `--sandbox PATH` | Existing marked snapshot; overrides `GOODNOTES_SANDBOX` |

Do not point `--sandbox` at an arbitrary raw Library folder. Make a validated,
caller-owned snapshot first if you need a separate copy:

```sh
uv run --locked goodnotes snapshot /path/to/Library /path/to/new-saved-copy
uv run --locked goodnotes --sandbox /path/to/new-saved-copy ls
```

The destination must be new. Completed snapshots are immutable.
`doctor.storage` identifies the selected copy and capture time. Named snapshots
have `cache_dir: null`; older markers may have `captured_at: null`.

## Keep the copy current

For normal requests, sync once before reading. For a cache-only reader, a separate
process can keep the cache refreshed:

```sh
uv run --locked goodnotes --data-dir /path/to/cli-cache watch --library /path/to/Library --interval 2 --debounce 1
```

Watch runs in the foreground. It emits an initial result and update, error, or
recovery events, stays quiet while unchanged, and stops with Ctrl-C. It sees files
that have reached the Mac. Neither sync nor watch proves an iPad edit arrived.
Check the image for the edit you expect.

Use `sync --force` to capture afresh even when the inventory appears unchanged.
Do not refresh between reads that must match; use [generation guards](output.md#pagination-and-consistent-reads).

## Diagnose access without writing

```sh
uv run --locked goodnotes --data-dir /path/to/cli-cache doctor --access-only --library /path/to/Library --output-dir /tmp
```

This works before cache initialization. It checks selected paths without database
opens, filesystem writes, or native engine opens. Write access is only a hint.
For native scratch, add `--scratch-dir /path/to/private-scratch`.

Read `error.details` for the failing path, operation, and remedy. The CLI never
switches caches or bypasses locks. If a managed lock is missing, stop other CLI
processes before running sync to repair it. Never delete or replace locks while
commands are running. See [troubleshooting](troubleshooting.md).
