# Icons

Application and toolbar icons live here as PNG files (multiple sizes) or as a
single SVG.

Planned assets:

| File | Used by |
|---|---|
| `enzydock.svg` | application icon (bundled into the EXE by PyInstaller) |
| `open.svg` | open protein / ligand actions |
| `run.svg` | START DOCKING button |
| `view_*.svg` | viewer display toggles |

Until the artwork exists the GUI uses text-only buttons, so nothing is broken
by the empty folder. When adding icons, reference them through
`app.utils.paths.resource("icons", "<file>")` so that the path also resolves
inside a PyInstaller bundle.
