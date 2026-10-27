"""Scope decisions depend on structured reasons, not diagnostic wording."""

from types import SimpleNamespace
from typing import NoReturn, cast

import pytest

from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.reader.catalog import Catalog, _DifferentKindError
from goodnotes_cli.reader.scope import ScopeResolver


def test_wrong_kind_scope_error_does_not_depend_on_wording() -> None:
    def wrong_kind(*_args: object, **_kwargs: object) -> NoReturn:
        raise _DifferentKindError("alternate diagnostic wording")

    resolver = ScopeResolver(cast(Catalog, SimpleNamespace(folder_key=wrong_kind)))
    with pytest.raises(GoodnotesError, match="Use --notebook"):
        resolver.scope("item", "notebook")
