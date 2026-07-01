"""Headless smoke test driving the TUI with Textual's pilot."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from exp.app import Cell, ExpApp


async def run_pilot(tmp: Path):
    nb = tmp / "nb.exp"
    app = ExpApp(nb)
    async with app.run_test(size=(100, 40)) as pilot:
        # starts with one empty cell selected
        assert len(app.roots) == 1
        assert app.selected is app.roots[0]

        # type into the first cell
        await pilot.press("enter")
        await pilot.press(*"hello world")
        await pilot.press("escape")
        assert app.roots[0].prompt == "hello world"

        # add a sibling after
        await pilot.press("a")
        await pilot.press(*"second")
        await pilot.press("escape")
        assert len(app.roots) == 2
        assert app.roots[1].prompt == "second"
        assert app.selected is app.roots[1]

        # add a nested child under the second cell
        await pilot.press("o")
        await pilot.press(*"child")
        await pilot.press("escape")
        assert len(app.roots[1].children) == 1
        child = app.roots[1].children[0]
        assert child.prompt == "child"
        assert child.parent is app.roots[1]
        assert child.depth() == 1

        # arrow-key navigation: up from child -> its parent
        await pilot.press("up")
        assert app.selected is app.roots[1]
        await pilot.press("up")
        assert app.selected is app.roots[0]
        await pilot.press("down", "down")
        assert app.selected is child

        # left collapses the parent, hiding the child
        await pilot.press("up", "left")
        assert app.roots[1].collapsed
        assert child not in app.visible_cells()
        await pilot.press("right")
        assert not app.roots[1].collapsed

        # session inheritance plumbing
        app.roots[1].session_id = "abc-123"
        assert child.ancestor_session() == "abc-123"

        # delete the child
        await pilot.press("down", "d")
        assert app.roots[1].children == []
        assert app.selected is app.roots[1]

        # save and reload round-trip
        app.save()
        data = json.loads(nb.read_text())
        assert [c["prompt"] for c in data["cells"]] == ["hello world", "second"]
        reloaded = [Cell.from_dict(c) for c in data["cells"]]
        assert reloaded[0].prompt == "hello world"

    print("all pilot assertions passed")


def test_smoke(tmp_path):
    import asyncio

    asyncio.run(run_pilot(tmp_path))


if __name__ == "__main__":
    import asyncio
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        asyncio.run(run_pilot(Path(td)))
