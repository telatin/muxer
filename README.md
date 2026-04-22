# muxer

![logo](muxer.png)

`muxer` is a small tmux-like multiplexer implemented in Python. It focuses on a narrower feature set:

- persistent background sessions
- multiple terminal tabs backed by real PTYs
- a status bar
- keyboard navigation between terminals
- page-based scrollback
- session save/restore to YAML

It is intentionally a lightweight, hackable alternative rather than a full tmux replacement.
The current UI is tab-oriented rather than split-pane-oriented.

## Install

```bash
pip install .
```

This exposes the `muxer` executable.

## Commands

```bash
muxer new work
muxer attach work
muxer save work
muxer restore ~/.muxer/sessions/work.yaml --name work-restored
muxer ls
muxer kill work
```

If a session does not exist, `muxer attach NAME` will fail. `muxer new NAME` starts the daemon and immediately attaches a client.

## Client shortcuts

`muxer` now uses a Screen-like command prefix so normal application keys pass through to the terminal. The default prefix is `Ctrl+A`.

- `Ctrl+A c`: open a new terminal tab
- `Ctrl+A d`: detach the client and leave the daemon running
- `Ctrl+A k`: kill the current terminal tab
- `Ctrl+A a`: rename the current terminal tab
- `Ctrl+A p`: move to the previous tab
- `Ctrl+A n`: move to the next tab
- `Ctrl+A 0..9`: jump to a tab by index
- `Ctrl+A s`: save the session to `~/.muxer/sessions/<name>.yaml`
- `Ctrl+A PageUp` / `Ctrl+A PageDown`: scroll the active terminal
- `Ctrl+A Ctrl+A`: send a literal prefix character through to the running program

After `Ctrl+A a`, `muxer` enters a simple inline rename mode: type the new name, press `Enter` to confirm, `Backspace` to edit, or `Esc` to cancel.

You can change the prefix with `MUXER_PREFIX=c-b muxer attach work`, `muxer --prefix c-b attach work`, or `muxer attach work --prefix c-b`.

## Save and Restore

The YAML save file stores, for each terminal:

- tab id and tab name
- best-effort current working directory
- a best-effort environment snapshot
- the launch shell
- a tail of recent plain-text output for reference

Running processes are not checkpointed. On restore, `muxer` starts fresh shells, changes to the saved directory, and reuses the saved environment where possible.

For better `$PWD` and environment capture, `muxer` injects a small shell hook for `bash` and `zsh` that refreshes state every prompt.

## State Directory

By default, `muxer` stores runtime sockets and save files under `~/.muxer`.
If that location is not writable, it falls back to a per-user temporary directory.
You can override the location explicitly with `MUXER_HOME=/path/to/state`.
