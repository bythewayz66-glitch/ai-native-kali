# kali-ai-native-shell

Hermes Desktop as the **native session** rather than an application. The shell
is the Kanban surface: the board is not a window you open, it is the desktop.

## Session

Registers `/usr/share/wayland-sessions/hermes-shell.desktop` so the display
manager offers *Hermes AI Desktop* at login:

```
Exec=/usr/bin/hermes-shell-session
```

## Surfaces

| Surface | What it is |
|---|---|
| `kanban_board` | the board columns, live from the API |
| `taskbar_widget` | counters plus the window-button list |
| `notification_feed` | events, with inline approve/reject on pending gates |
| `card_drawer` | card detail: traces, artifacts, approvals, replay |
| `file_manager_drop` | drag a file onto a card to attach it |
| `overlay_widget` | floating status: board *and* service state |
| `window_manager` | focus, stacking, minimise/maximise/close, taskbar |

## Honest scope

The window manager is real (a tested state machine) but the session is a
browser-hosted panel rather than a GTK/Qt compositor client. See
`BUILD_STATUS.md` in the source tree.
