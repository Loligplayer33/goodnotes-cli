"""Agent-oriented commands for Goodnotes library reading."""

from __future__ import annotations

from typing import Annotated, Any

import typer
from typer.core import TyperGroup

from .. import __version__, build_info
from ..presentation.contracts import output_contract
from ..presentation.json import emit
from .core import command, settings


@command()
def schema(
    ctx: typer.Context,
    command_name: Annotated[
        str | None, typer.Argument(metavar="COMMAND", help="Command to describe in detail.")
    ] = None,
) -> None:
    """Show the command index or one command's machine-readable contract.

    Does not load notes or refresh the library. Targeted details include registered
    arguments, options, defaults, bounds, global options, and usage notes.

    Example: goodnotes schema read
    """
    if command_name is not None:
        root = ctx.find_root()
        group = root.command
        assert isinstance(group, TyperGroup)
        target = group.commands.get(command_name)
        if target is None:
            raise typer.BadParameter(f"Unknown command: {command_name}", param_hint="COMMAND")
        with typer.Context(target, parent=root, info_name=command_name) as target_ctx:
            parameters = target.get_params(target_ctx)
            result: dict[str, Any] = {
                "version": __version__,
                "build": build_info(),
                "command": command_name,
                "usage": target.get_usage(target_ctx).removeprefix("Usage: "),
                "help": target.help,
                "arguments": [
                    parameter_contract(p) for p in parameters if p.param_type_name == "argument"
                ],
                "options": [
                    parameter_contract(p) for p in parameters if p.param_type_name == "option"
                ],
                "global_options": [parameter_contract(p) for p in group.get_params(root)],
            }
            contract = output_contract(command_name)
            if contract is not None:
                result["output"] = contract
            emit(
                result,
                settings(ctx),
            )
        return
    group = ctx.find_root().command
    assert isinstance(group, TyperGroup)
    emit(
        {
            "version": __version__,
            "build": build_info(),
            "format": "compact-json",
            "page_numbers": "one-based",
            "commands": {
                name: target.get_short_help_str()
                for name, target in group.commands.items()
                if not target.hidden
            },
            "advanced_commands": {
                name: target.get_short_help_str()
                for name, target in group.commands.items()
                if target.hidden
            },
            "limits": {
                "read_pages": 20,
                "read_chars": 12000,
                "search_hits": 20,
                "snippet_chars": 320,
                "list_items": 100,
                "bundle_pages": 10,
            },
            "limits_note": (
                "read_pages, snippet_chars, bundle_pages are maxima; other limits are defaults. "
                "Use schema COMMAND for accepted option ranges."
            ),
            "global_options": [
                flag
                for parameter in group.get_params(ctx.find_root())
                if not getattr(parameter, "hidden", False)
                for flag in parameter.opts
            ],
            "usage_notes": [
                "Global options go before the command. schema COMMAND gives targeted details.",
                "A managed cache can select a default root in library-root.json "
                "with root_folder_id. --library-root ID or all overrides it; "
                "info exposes root_folder_id with --fields. Explicit --sandbox reads "
                "do not inherit the managed cache selection.",
                "schema COMMAND includes output field hints for row-based responses.",
                "Notebook selectors accept an exact UUID, full path, or unique exact name.",
                "Follow next_offset or next_page_offset; pin snapshot with --expect-snapshot.",
                "For recent edits, sync once per request and pin its snapshot_id across reads.",
                "Check source labels, coverage, warnings, and truncated when interpreting results.",
                "--text indents JSON; --fields selects row fields or top-level keys.",
                "Output keeps essential evidence by default; --verbose includes full metadata.",
                "--fields selects from full rows or responses, overriding concise defaults.",
                "For explanation or learning, use bundle and open its images, including diagrams.",
                "read is a rough text overview; read/search/bundle exclude handwriting candidates "
                "unless --source handwriting or all is requested.",
                "pages includes recognition counts, not candidate text. Use these to prioritize "
                "inspection, never to exclude pages with diagrams or unindexed ink.",
                "Advanced commands remain available with COMMAND --help or schema COMMAND.",
                "Source files are read-only; some commands write local copies or exports.",
            ],
            "annotations": (
                "--layer native reconstructs supported copied pen/highlighter records with "
                "coverage diagnostics. It requires a compatible RocksDB runtime and writable "
                "scratch storage. Full visual fidelity is unverified. --layer annotated uses "
                "a PDF registered with attach-pdf. A successful native query with no matching "
                "records renders the background with no-native-records-observed coverage; "
                "absence of annotations in Goodnotes remains unverified."
            ),
            "exit_codes": {
                "0": "success",
                "1": "operation failed",
                "2": "invalid arguments",
                "130": "interrupted or broken pipe",
            },
        },
        settings(ctx),
    )


def parameter_contract(parameter: Any) -> dict[str, Any]:
    """Describe registered parser metadata without maintaining a second option table."""
    result = {
        "name": parameter.name,
        "type": parameter.type.name,
        "required": parameter.required,
        "default": parameter.default,
    }
    if parameter.param_type_name == "option":
        result["flags"] = parameter.opts + parameter.secondary_opts
        result["is_flag"] = parameter.is_flag
    for key in ("help", "envvar"):
        value = getattr(parameter, key, None)
        if value is not None:
            result[key] = value
    if getattr(parameter, "hidden", False):
        result["advanced"] = True
    for key in ("min", "max"):
        value = getattr(parameter.type, key, None)
        if value is not None:
            result[key] = value
    return result
