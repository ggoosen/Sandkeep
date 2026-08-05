"""--max-turns is gone (improvement plan, step 10): the upstream claude CLI
removed the flag, so accepting-and-ignoring it was silent input loss. It now
fails loud with the reason and the supported alternatives."""

from __future__ import annotations

import pytest

from sandkeep.cli import build_parser


def test_max_turns_errors_with_explanation(capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["run", "--repo", "/r", "--task", "t", "--max-turns", "3"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "no longer supported" in err
    assert "--max-budget-usd" in err
