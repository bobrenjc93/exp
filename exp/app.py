"""exp — a notebook-style TUI for experimenting with `claude -p`.

Each cell holds a prompt that is executed with `claude -p`. Cells can be
nested: a child cell resumes its parent's Claude session (forked), so
nesting represents conversational branching. Sibling children of the same
parent are independent forks of the same context.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Label, Markdown, Static, TextArea

STREAM_LIMIT = 10 * 1024 * 1024  # single stream-json lines can be large


STATUS_ICON = {
    "idle": "○",
    "running": "◐",
    "done": "●",
    "error": "✗",
}


# --------------------------------------------------------------------------- model

@dataclass
class Cell:
    prompt: str = ""
    output: str = ""
    status: str = "idle"
    note: str = ""  # duration/cost or error summary
    session_id: str | None = None
    model: str = ""  # model that actually served the last run
    collapsed: bool = False  # children hidden
    folded: bool = False  # cell body (prompt/output) hidden, children too
    children: list["Cell"] = field(default_factory=list)
    parent: "Cell | None" = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def depth(self) -> int:
        d, node = 0, self.parent
        while node is not None:
            d, node = d + 1, node.parent
        return d

    def ancestor_session(self) -> str | None:
        node = self.parent
        while node is not None:
            if node.session_id:
                return node.session_id
            node = node.parent
        return None

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def to_dict(self) -> dict:
        return {
            "prompt": self.prompt,
            "output": self.output,
            "status": "idle" if self.status == "running" else self.status,
            "note": self.note,
            "session_id": self.session_id,
            "model": self.model,
            "collapsed": self.collapsed,
            "folded": self.folded,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, data: dict, parent: "Cell | None" = None) -> "Cell":
        cell = cls(
            prompt=data.get("prompt", ""),
            output=data.get("output", ""),
            status=data.get("status", "idle"),
            note=data.get("note", ""),
            session_id=data.get("session_id"),
            model=data.get("model", ""),
            collapsed=data.get("collapsed", False),
            folded=data.get("folded", False),
            parent=parent,
        )
        cell.children = [cls.from_dict(c, cell) for c in data.get("children", [])]
        return cell


# --------------------------------------------------------------------------- widgets

class PromptArea(TextArea):
    """The editable prompt of a cell."""

    def __init__(self, cell: Cell) -> None:
        super().__init__(cell.prompt, soft_wrap=True, tab_behavior="focus")
        self.cell = cell
        self.show_line_numbers = False

    def on_key(self, event) -> None:
        # While the prompt is still empty (e.g. right after `o`/`a`),
        # left/right adjust the cell's indentation instead of moving the
        # cursor, so `o` then `←` turns the new child into a sibling.
        if not self.text and event.key in ("left", "right"):
            event.prevent_default()
            event.stop()
            app = self.app
            app.select(self.cell)
            if event.key == "left":
                app.action_dedent(keep_editing=True)
            else:
                app.action_indent(keep_editing=True)


class CellWidget(Vertical):
    def __init__(self, cell: Cell) -> None:
        super().__init__(classes="cell")
        self.cell = cell
        self.styles.margin = (0, 1, 1, cell.depth() * 4)

    def compose(self) -> ComposeResult:
        yield Static(self._header_text(), classes="cell-header")
        yield PromptArea(self.cell)
        yield Markdown(self.cell.output, classes="cell-output")

    def on_mount(self) -> None:
        self._fit_prompt_height()
        self._apply_fold()

    def _apply_fold(self) -> None:
        folded = self.cell.folded
        self.query_one(PromptArea).display = not folded
        self.query_one(Markdown).display = not folded and bool(self.cell.output)

    def _header_text(self) -> str:
        cell = self.cell
        icon = STATUS_ICON.get(cell.status, "?")
        parts = [f"{icon} {cell.status}"]
        if cell.folded:
            summary = " ".join(cell.prompt.split())
            parts.append(f"⊞ {summary[:60] + '…' if len(summary) > 60 else summary or '(empty)'}")
        if cell.model:
            parts.append(cell.model)
        if cell.note:
            parts.append(cell.note)
        if cell.session_id:
            parts.append(f"session {cell.session_id[:8]}")
        if cell.parent is not None:
            parts.append("↳ continues parent")
        if cell.children:
            marker = "▸" if (cell.collapsed or cell.folded) else "▾"
            parts.append(f"{marker} {len(cell.children)} child(ren)")
        return "  ·  ".join(parts)

    def _fit_prompt_height(self) -> None:
        area = self.query_one(PromptArea)
        area.styles.height = max(3, min(area.wrapped_document.height + 2, 14))

    def refresh_from_cell(self) -> None:
        self.query_one(".cell-header", Static).update(self._header_text())
        self._apply_fold()
        self.query_one(Markdown).update(self.cell.output)
        self.set_class(self.cell.status == "running", "running")
        self.set_class(self.cell.status == "error", "errored")

    @on(TextArea.Changed)
    def _prompt_changed(self, event: TextArea.Changed) -> None:
        self.cell.prompt = event.text_area.text
        self._fit_prompt_height()


class ModelScreen(ModalScreen[str | None]):
    """Prompt for a model name; empty submits claude's default."""

    AUTO_FOCUS = "Input"

    CSS = """
    ModelScreen {
        align: center middle;
    }
    #model-dialog {
        width: 70;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $panel;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, current: str) -> None:
        super().__init__()
        self.current = current

    def action_cancel(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Vertical(id="model-dialog"):
            yield Label("Model for new runs (empty = claude default, Esc = cancel)")
            yield Input(value=self.current, placeholder="e.g. claude-haiku-4-5-20251001")

    @on(Input.Submitted)
    def _submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())


# --------------------------------------------------------------------------- app

class ExpApp(App):
    TITLE = "exp"
    AUTO_FOCUS = None  # start in command mode; Enter focuses a cell's prompt

    CSS = """
    #cells {
        padding: 1 2;
    }
    .cell {
        height: auto;
        border-left: wide $surface-lighten-2;
        padding: 0 1;
    }
    .cell.selected {
        border-left: wide $accent;
        background: $boost;
    }
    .cell.running {
        border-left: wide $warning;
    }
    .cell.errored {
        border-left: wide $error;
    }
    .cell-header {
        color: $text-muted;
        text-style: italic;
    }
    .cell PromptArea {
        border: round $surface-lighten-2;
    }
    .cell PromptArea:focus {
        border: round $accent;
    }
    .cell-output {
        margin: 0;
        padding: 0 1;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("up", "select_prev", "↑/↓ navigate", show=True),
        Binding("down", "select_next", "", show=False),
        Binding("left", "dedent", "◂ dedent", show=False),
        Binding("right", "indent", "▸ indent", show=False),
        Binding("c", "toggle_children", "Fold children", show=False),
        Binding("f", "toggle_fold", "Fold cell"),
        Binding("enter", "edit", "Edit"),
        Binding("escape", "leave_edit", "Done editing", show=False, priority=True),
        Binding("ctrl+r", "run", "Run", priority=True),
        Binding("a", "add_after", "+Cell"),
        Binding("b", "add_before", "+Cell above", show=False),
        Binding("o", "add_child", "+Nested cell"),
        Binding("d", "delete", "Delete"),
        Binding("k", "cancel_run", "Kill run", show=False),
        Binding("m", "set_model", "Model"),
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("q", "quit_save", "Quit"),
        Binding("ctrl+q", "quit_save", "Quit", show=False, priority=True),
        Binding("ctrl+c", "quit_save", "Quit", show=False, priority=True),
    ]

    def __init__(self, path: Path, model: str = "") -> None:
        super().__init__()
        self.path = path
        # precedence: --model flag > EXP_MODEL > notebook's saved model
        self.model = model or os.environ.get("EXP_MODEL", "")
        self._model_from_cli = bool(self.model)
        self.roots: list[Cell] = []
        self.selected: Cell | None = None
        self.cell_widgets: dict[str, CellWidget] = {}
        self.procs: dict[str, asyncio.subprocess.Process] = {}
        self.dirty = False
        self._load()

    # ---------------------------------------------------------------- persistence

    def _load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text())
            self.roots = [Cell.from_dict(c) for c in data.get("cells", [])]
            if not self._model_from_cli:
                self.model = data.get("model", "")
        if not self.roots:
            self.roots = [Cell()]
        self.selected = self.roots[0]

    def save(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "model": self.model,
                    "cells": [c.to_dict() for c in self.roots],
                },
                indent=2,
            )
        )
        self.dirty = False
        self._update_title()

    def mark_dirty(self) -> None:
        self.dirty = True
        self._update_title()

    def _update_title(self) -> None:
        model = self.model or os.environ.get("ANTHROPIC_MODEL", "") or "claude default"
        self.title = f"exp — {self.path.name}{' *' if self.dirty else ''} · {model}"

    # ---------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        scroll = VerticalScroll(id="cells")
        scroll.can_focus = False
        yield scroll
        yield Footer()

    def on_mount(self) -> None:
        self._update_title()
        self.rebuild()

    def visible_cells(self) -> list[Cell]:
        out: list[Cell] = []

        def visit(cell: Cell) -> None:
            out.append(cell)
            if not cell.collapsed and not cell.folded:
                for child in cell.children:
                    visit(child)

        for root in self.roots:
            visit(root)
        return out

    def rebuild(self) -> None:
        container = self.query_one("#cells", VerticalScroll)
        container.remove_children()
        self.cell_widgets = {}
        for cell in self.visible_cells():
            widget = CellWidget(cell)
            self.cell_widgets[cell.id] = widget
            container.mount(widget)
        self._apply_selection()
        self.call_after_refresh(self._refresh_all_headers)

    def _refresh_all_headers(self) -> None:
        for widget in self.cell_widgets.values():
            widget.refresh_from_cell()

    def _apply_selection(self) -> None:
        for widget in self.cell_widgets.values():
            widget.set_class(widget.cell is self.selected, "selected")
        if self.selected and self.selected.id in self.cell_widgets:
            self.cell_widgets[self.selected.id].scroll_visible()

    def select(self, cell: Cell) -> None:
        self.selected = cell
        self._apply_selection()

    def update_cell_view(self, cell: Cell) -> None:
        widget = self.cell_widgets.get(cell.id)
        if widget is not None and widget.is_mounted:
            widget.refresh_from_cell()

    # ---------------------------------------------------------------- navigation

    def _move_selection(self, delta: int) -> None:
        cells = self.visible_cells()
        if not cells or self.selected is None:
            return
        try:
            idx = cells.index(self.selected)
        except ValueError:
            idx = 0
        self.select(cells[max(0, min(len(cells) - 1, idx + delta))])

    def action_select_prev(self) -> None:
        self._move_selection(-1)

    def action_select_next(self) -> None:
        self._move_selection(1)

    def action_toggle_children(self) -> None:
        cell = self.selected
        if cell and cell.children:
            cell.collapsed = not cell.collapsed
            self.rebuild()

    def _reindent(self, cell: Cell, new_parent: Cell | None, index: int, keep_editing: bool) -> None:
        old_siblings = cell.parent.children if cell.parent else self.roots
        old_siblings.remove(cell)
        cell.parent = new_parent
        siblings = new_parent.children if new_parent else self.roots
        siblings.insert(index, cell)
        self.mark_dirty()
        self.rebuild()
        self.select(cell)
        if keep_editing:
            self.call_after_refresh(self._focus_prompt)
        if cell.status != "idle":
            self.notify("Cell re-parented — a re-run will continue the new parent's session.")

    def action_dedent(self, keep_editing: bool = False) -> None:
        cell = self.selected
        if cell is None or cell.parent is None:
            return
        # become the next sibling of the old parent
        parent = cell.parent
        grandparent = parent.parent
        target = grandparent.children if grandparent else self.roots
        self._reindent(cell, grandparent, target.index(parent) + 1, keep_editing)

    def action_indent(self, keep_editing: bool = False) -> None:
        cell = self.selected
        if cell is None:
            return
        if cell.folded:
            self.action_toggle_fold()
            return
        siblings = cell.parent.children if cell.parent else self.roots
        idx = siblings.index(cell)
        if idx == 0:
            return  # no previous sibling to become a child of
        new_parent = siblings[idx - 1]
        new_parent.collapsed = new_parent.folded = False
        self._reindent(cell, new_parent, len(new_parent.children), keep_editing)

    def action_toggle_fold(self) -> None:
        cell = self.selected
        if cell is None:
            return
        cell.folded = not cell.folded
        self.mark_dirty()
        if cell.children:
            self.rebuild()
        else:
            self.update_cell_view(cell)
        self._apply_selection()

    # ---------------------------------------------------------------- editing

    def action_edit(self) -> None:
        if self.selected is None:
            return
        if self.selected.folded:
            self.action_toggle_fold()
            self.call_after_refresh(self._focus_prompt)
        else:
            self._focus_prompt()

    def _focus_prompt(self) -> None:
        widget = self.cell_widgets.get(self.selected.id) if self.selected else None
        if widget is not None:
            area = widget.query_one(PromptArea)
            area.focus()
            area.move_cursor(area.document.end)

    def check_action(self, action: str, parameters) -> bool:
        # App-level priority bindings must not swallow keys meant for modals.
        if isinstance(self.screen, ModelScreen):
            return False
        if action == "leave_edit":
            return isinstance(self.focused, PromptArea)
        return True

    def action_leave_edit(self) -> None:
        if isinstance(self.focused, PromptArea):
            self.select(self.focused.cell)
            self.screen.set_focus(None)
            self.mark_dirty()

    @on(TextArea.Changed)
    def _on_prompt_changed(self, event: TextArea.Changed) -> None:
        self.mark_dirty()

    def on_descendant_focus(self, event) -> None:
        widget = event.widget
        if isinstance(widget, PromptArea):
            self.select(widget.cell)

    # ---------------------------------------------------------------- structure

    def _new_cell(self, parent: Cell | None, index: int) -> None:
        cell = Cell(parent=parent)
        siblings = parent.children if parent else self.roots
        siblings.insert(index, cell)
        self.mark_dirty()
        self.rebuild()
        self.select(cell)
        self.call_after_refresh(self.action_edit)

    def action_add_after(self) -> None:
        cell = self.selected
        if cell is None:
            self._new_cell(None, len(self.roots))
            return
        siblings = cell.parent.children if cell.parent else self.roots
        self._new_cell(cell.parent, siblings.index(cell) + 1)

    def action_add_before(self) -> None:
        cell = self.selected
        if cell is None:
            self._new_cell(None, 0)
            return
        siblings = cell.parent.children if cell.parent else self.roots
        self._new_cell(cell.parent, siblings.index(cell))

    def action_add_child(self) -> None:
        cell = self.selected
        if cell is None:
            return
        cell.collapsed = False
        self._new_cell(cell, len(cell.children))

    def action_delete(self) -> None:
        cell = self.selected
        if cell is None:
            return
        for node in cell.walk():
            proc = self.procs.pop(node.id, None)
            if proc is not None:
                proc.terminate()
        siblings = cell.parent.children if cell.parent else self.roots
        idx = siblings.index(cell)
        siblings.remove(cell)
        if not self.roots:
            self.roots = [Cell()]
        if cell.parent and cell.parent.children:
            self.selected = cell.parent.children[min(idx, len(cell.parent.children) - 1)]
        elif cell.parent:
            self.selected = cell.parent
        else:
            self.selected = self.roots[min(idx, len(self.roots) - 1)]
        self.mark_dirty()
        self.rebuild()

    # ---------------------------------------------------------------- running

    def action_run(self) -> None:
        if isinstance(self.focused, PromptArea):
            self.select(self.focused.cell)
            self.screen.set_focus(None)
        cell = self.selected
        if cell is None:
            return
        if not cell.prompt.strip():
            self.notify("Cell prompt is empty.", severity="warning")
            return
        if cell.id in self.procs:
            self.notify("Cell is already running (press k to kill it).", severity="warning")
            return
        self._run_cell(cell)

    def action_set_model(self) -> None:
        def apply(model: str | None) -> None:
            if model is None:
                return
            self.model = model
            self.mark_dirty()
            self.notify(f"Model for new runs: {model or 'claude default'}")

        self.push_screen(ModelScreen(self.model), apply)

    def action_cancel_run(self) -> None:
        cell = self.selected
        if cell and cell.id in self.procs:
            self.procs[cell.id].terminate()
            self.notify("Run cancelled.")

    @work(exclusive=False)
    async def _run_cell(self, cell: Cell) -> None:
        claude = shutil.which("claude")
        if claude is None:
            cell.status, cell.note = "error", "claude CLI not found on PATH"
            self.update_cell_view(cell)
            return

        cmd = [claude, "-p", cell.prompt, "--output-format", "stream-json", "--verbose"]
        resume = cell.ancestor_session()
        if resume:
            cmd += ["--resume", resume, "--fork-session"]
        if self.model:
            cmd += ["--model", self.model]
        cmd += shlex.split(os.environ.get("EXP_CLAUDE_ARGS", ""))

        cell.status, cell.note, cell.output, cell.session_id = "running", "", "", None
        cell.model = ""
        self.update_cell_view(cell)
        self.mark_dirty()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=STREAM_LIMIT,
            )
        except OSError as exc:
            cell.status, cell.note = "error", str(exc)
            self.update_cell_view(cell)
            return

        self.procs[cell.id] = proc
        transcript: list[str] = []
        result_event: dict | None = None

        try:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = event.get("type")
                if etype == "system" and event.get("subtype") == "init":
                    cell.session_id = event.get("session_id")
                    cell.model = event.get("model", "")
                elif etype == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "text" and block.get("text"):
                            transcript.append(block["text"])
                        elif block.get("type") == "tool_use":
                            transcript.append(f"*⚙ {block.get('name', 'tool')}*")
                elif etype == "result":
                    result_event = event
                    cell.session_id = event.get("session_id", cell.session_id)
                cell.output = "\n\n".join(transcript)
                self.update_cell_view(cell)

            stderr = (await proc.stderr.read()).decode(errors="replace") if proc.stderr else ""
            code = await proc.wait()
        finally:
            self.procs.pop(cell.id, None)

        if result_event and not result_event.get("is_error") and code == 0:
            if not cell.output:
                cell.output = result_event.get("result") or ""
            secs = (result_event.get("duration_ms") or 0) / 1000
            cost = result_event.get("total_cost_usd")
            cell.status = "done"
            cell.note = f"{secs:.1f}s" + (f" · ${cost:.2f}" if cost else "")
        else:
            cell.status = "error"
            detail = ""
            if result_event and result_event.get("result"):
                detail = str(result_event["result"])
            elif stderr.strip():
                detail = stderr.strip().splitlines()[-1]
            cell.note = f"exit {code}" + (f" — {detail[:120]}" if detail else "")
        self.update_cell_view(cell)
        self.mark_dirty()

    # ---------------------------------------------------------------- misc actions

    def action_save(self) -> None:
        self.save()
        self.notify(f"Saved {self.path}")

    def action_quit_save(self) -> None:
        for proc in self.procs.values():
            proc.terminate()
        if self.dirty:
            self.save()
        self.exit()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="exp",
        description="A notebook-style TUI for experimenting with claude -p. "
        "Nested cells continue their parent's Claude session.",
    )
    parser.add_argument(
        "notebook",
        nargs="?",
        default="notebook.exp",
        help="notebook file to open or create (default: ./notebook.exp)",
    )
    parser.add_argument(
        "--model",
        default="",
        help="model to pass to claude --model (also settable via EXP_MODEL "
        "or the 'm' key in the app; default: claude's own default)",
    )
    args = parser.parse_args()
    ExpApp(Path(args.notebook), model=args.model).run()


if __name__ == "__main__":
    main()
