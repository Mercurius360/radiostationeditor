# ATS/ETS2 Radio Station Editor (Windows)

A simple Windows tool to import, edit, sort, reorder (drag & drop), and delete radio stations in `live_streams.sii`
for American Truck Simulator and Euro Truck Simulator 2 — without manually editing the `.sii` file.

## Features
- Detect ATS / ETS2 `live_streams.sii`
- Search + sort
- Drag & drop reorder (Custom mode)
- Add / edit / duplicate
- Delete one or multiple stations
- Favorite toggle (star)
- Auto-save on reorder and on Save/Delete/Duplicate
- Creates backups in `live_streams_backup` next to the `.sii`

## Backups
Every write creates a timestamped backup:
`...\live_streams_backup\live_streams.sii.bak_YYYYMMDD_HHMMSS`

## Run from source
```bash
pip install PySide6
python radio_editor.py
