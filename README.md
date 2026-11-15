# avX

Audio/video player for Linux. GTK4 + libadwaita UI, libmpv does the playback.

## Run

    sudo pacman -S mpv gtk4 libadwaita python-gobject   # mpv ships libmpv
    python -m venv --system-site-packages .venv && .venv/bin/pip install mpv
    .venv/bin/python avx.py [files...]

Open with Ctrl+O, the button, drag-and-drop, or file arguments (extra files queue up).
Drop a subtitle file (.srt .ass .ssa .vtt .sub) on the window to load it. Subtitles next to the video with a similar name load automatically. Playback resumes where you left off.

## Controls

The dock at the bottom has seek, previous/next, volume, subtitles (tracks, load file, delay), audio track, speed, shuffle, repeat (off / file / playlist), screenshot (saved to Pictures) and fullscreen. The list button top right opens the playlist. Controls hide after 3 seconds and leave a thin progress line.

## Keys

| Key | Action |
|---|---|
| Space, click | play/pause |
| Left / Right | seek 5s |
| Up / Down, mouse wheel | volume |
| M | mute |
| F, F11, double-click | fullscreen |
| `<` `>` | previous / next |
| Page Up / Page Down | previous / next chapter |
| `,` `.` | step one frame back / forward |
| `[` `]` | slower / faster |
| V | subtitles on/off |
| R | cycle repeat |
| P | playlist |
| S | screenshot |
| Ctrl+O | open |
| Q | quit |
