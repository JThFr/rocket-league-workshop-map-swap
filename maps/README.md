# Maps

Where `rpmap` looks for candidate maps (in this order):

1. **Steam Workshop** — `<steamapps>/workshop/content/252950/*/` (any `.udk` / `.upk`).
   Subscribe on the Steam Workshop and the item shows up here, and in the UI.
2. **`mods/`** — `<CookedPCConsole>/mods/` (sub-folders or loose `.udk` / `.upk` files).
3. **A local maps folder** — `~/maps` by default, or whatever `RL_MAPS` points at.

Drop `.udk` / `.upk` files (or folders containing them) into `~/maps` — or set
`RL_MAPS=/path/to/maps` — and they appear in `rpmap list` and the web UI.

## Rules

- Pick the map by **name + author + version** and keep the **same file on every player**;
  mismatched versions cause a content mismatch.
- `rpmap` identifies a map by size + `sha256`, so two files with identical content match
  even if they are named differently.
- Map files are **never** committed to this repo — `.gitignore` excludes `*.upk` / `*.udk`.
