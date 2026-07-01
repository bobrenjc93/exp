# exp

A notebook-style TUI for experimenting with `claude -p`. Think Jupyter, but
each cell is a prompt sent to Claude Code, and nesting a cell continues its
parent's conversation.

```
exp                # opens ./notebook.exp (created on first save)
exp ideas.exp      # open/create a specific notebook
```

## How it works

- Each cell holds a prompt. Running a cell invokes `claude -p <prompt>
  --output-format stream-json` and streams the assistant's output (including
  tool-use markers) into the cell as it arrives.
- **Nested cells continue their parent's session.** A child cell runs with
  `--resume <parent-session> --fork-session`, so it sees the full parent
  conversation but gets its own session ID. Multiple children of the same
  parent are independent forks — branch an experiment as many ways as you
  like.
- Top-level cells are fresh, independent sessions.
- Notebooks are plain JSON (`.exp` files), saved automatically on quit.

## Keys

| Key | Action |
| --- | --- |
| `↑` / `↓` | Move between cells |
| `←` | Collapse cell's children (or jump to parent) |
| `→` | Expand collapsed children |
| `Enter` | Edit the selected cell's prompt |
| `Esc` | Stop editing (back to command mode) |
| `Ctrl+R` | Run the selected cell (works while editing too) |
| `a` | New cell below (same level) |
| `b` | New cell above |
| `o` | New nested cell (continues this cell's session) |
| `d` | Delete cell (and its children) |
| `k` | Kill a running cell |
| `Ctrl+S` | Save |
| `q` | Save and quit |

## Extra claude flags

Set `EXP_CLAUDE_ARGS` to pass extra flags to every invocation, e.g.:

```
EXP_CLAUDE_ARGS="--model claude-haiku-4-5-20251001" exp
```

## Install

```
uv venv .venv && uv pip install --python .venv/bin/python -e .
ln -s "$PWD/bin/exp" ~/.local/bin/exp
```

Requires the `claude` CLI on your PATH.

## Development

Run the headless smoke test (drives the UI via Textual's pilot):

```
.venv/bin/python tests/test_smoke.py
```
