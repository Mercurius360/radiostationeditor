# ATS/ETS2 Radio Station Editor (Windows)

![Image](https://github.com/user-attachments/assets/d4cd97e9-484e-4fb3-b87d-a497bd6e2196)

![Image](https://github.com/user-attachments/assets/260a52a6-4a82-439d-8362-e4dc5a1045df)

A simple Windows tool to import, edit, sort, reorder (drag & drop), and delete radio stations in `live_streams.sii`
for American Truck Simulator and Euro Truck Simulator 2 — without manually editing the `.sii` file.

## Features
- Detect ATS / ETS2 `live_streams.sii` or import custom-linked ones and let the app know which game it's associated to
- Search + sort
- Drag & drop reordering single or multiple slots
- Add / edit / duplicate
- Delete one or multiple stations
- Favorite toggle for one or multiple stations
- Auto-save on reorder and on Save/Delete/Duplicate
- Copies & syncs live_streams.sii from ATS -> ETS2 and vise vera.
- Creates backups in `live_streams_backup` next to the `.sii`

## Backups
Every write or radio slot change creates a timestamped backup:
`...\live_streams_backup\live_streams.sii.bak_YYYYMMDD_HHMMSS`

Tutorial Video:
https://screenpal.com/content/video/cOVQ16nrtbp
