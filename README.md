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
- `←`/`→` re-indent a cell (Workflowy-style). Because nesting decides which
  session a cell continues, re-indenting a cell changes what context its
  *next* run sees — existing output is kept but a re-run uses the new
  parent's session. Right after `o` or `a`, while the empty prompt is
  focused, `←`/`→` re-indent the new cell directly.
- Notebooks are plain JSON (`.exp` files), saved automatically on quit.

## Keys

| Key | Action |
| --- | --- |
| `↑` / `↓` | Move between cells |
| `←` | Dedent: cell becomes a sibling of its parent |
| `→` | Indent: cell becomes a child of the cell above (or unfolds a folded cell) |
| `c` | Collapse/expand the cell's children |
| `f` | Fold/unfold the cell itself (one-line summary) |
| `Enter` | Edit the selected cell's prompt |
| `Esc` | Stop editing (back to command mode) |
| `Ctrl+R` | Run the selected cell (works while editing too) |
| `a` | New cell below (same level) |
| `b` | New cell above |
| `o` | New nested cell (continues this cell's session) |
| `d` | Delete cell (and its children) |
| `k` | Kill a running cell |
| `m` | Set the model for new runs |
| `Ctrl+S` | Save |
| `q` | Save and quit |

## Choosing a model

The model can be set (highest precedence first) via:

1. `exp --model claude-haiku-4-5-20251001`
2. `EXP_MODEL=claude-haiku-4-5-20251001 exp`
3. the `m` key inside the app (saved with the notebook)

If none is set, `claude` uses its own default. The header bar shows the
model in effect, and each cell shows the model that actually served its
last run.

Environment variables are passed through to `claude`, so provider config
works as usual:

```
AWS_REGION=us-east-1 ANTHROPIC_MODEL="us.anthropic.claude-fable-5" exp
```

`EXP_CLAUDE_ARGS` passes arbitrary extra flags to every invocation.

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
