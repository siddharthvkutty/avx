# avX

Audio/video player for Linux. GTK4 + libadwaita UI, libmpv does the playback.

## Run

    sudo pacman -S mpv gtk4 libadwaita python-gobject   # mpv ships libmpv
    python -m venv --system-site-packages .venv && .venv/bin/pip install mpv
    .venv/bin/python avx.py [files...]

Open with Ctrl+O, the button, drag-and-drop, or file arguments (extra files queue up).

## Keys

Space pause, Left/Right seek 5s, Up/Down volume, M mute, F/F11/double-click fullscreen,
`<` `>` prev/next, Ctrl+O open, Q quit.
