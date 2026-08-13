# MediaStudio No-Attribution SFX MCP

This dependency-free stdio MCP gives Codex and Fortress Sextant one governed
catalog for background ambience and sound effects.

The policy is fail-closed:

- accepted providers: Pixabay Sound Effects and Mixkit Sound Effects;
- attribution must not be required;
- commercial and social-media video use must be permitted;
- provider scraping, bulk downloads, ripped audio, CC BY, CC BY-NC, and
  personal-use-only assets are rejected;
- acquisition is a manual, single-item download followed by MCP import.

The manual acquisition boundary is intentional. Pixabay prohibits unauthorized
scraping and systematic copying, Mixkit prohibits scripted mass downloads, and
neither offers a public sound-effects API suitable for this commercial workflow.
The free Freesound API is also excluded because its API terms limit free API use
to noncommercial purposes.

## Tools

- `sfx_policy`: show the enforced license and provider policy.
- `sfx_search_links`: build provider search links without scraping.
- `sfx_import_asset`: import one manually downloaded file with source and
  license provenance.
- `sfx_list_assets`: search the governed local catalog.
- `sfx_verify_asset`: verify a catalog file against its SHA-256 digest.
- `sfx_stage_asset`: copy an approved sound into
  `projects/<project-id>/input/sound-effects/` with provenance.

Supported input formats are WAV, MP3, FLAC, and OGG. Imports are limited to
`SFX_IMPORT_ROOTS` and default to `~/Downloads` plus the library `inbox`.

## Codex

Run locally over stdio:

```bash
/usr/bin/python3 /Users/benjaminlagrone/Documents/projects/MediaStudio/rs-video-stitch/mcp/sfx_mcp.py
```

Registration command:

```bash
codex mcp add mediastudio_sfx \
  --env SFX_LIBRARY_ROOT=/Users/benjaminlagrone/Videos/MediaStudio/sfx-library \
  --env SFX_PROJECTS_ROOT=/Users/benjaminlagrone/Videos/MediaStudio/projects \
  --env SFX_IMPORT_ROOTS=/Users/benjaminlagrone/Downloads \
  -- /usr/bin/python3 /Users/benjaminlagrone/Documents/projects/MediaStudio/rs-video-stitch/mcp/sfx_mcp.py
```

The MCP marks import and staging tools as writes so Codex can apply write-tool
approval policy.

## Fortress Sextant

Use the same checked-out script on Sextant. The MediaStudio runtime remains the
owner of the project and asset state; this MCP is only a stdio capability and
does not expose a port or define another deployment path.

Example environment for the Sextant-side MCP process:

```bash
SFX_LIBRARY_ROOT="$HOME/Videos/MediaStudio/sfx-library" \
SFX_PROJECTS_ROOT="$HOME/Videos/MediaStudio/projects" \
SFX_IMPORT_ROOTS="$HOME/Videos/MediaStudio/sfx-inbox" \
/usr/bin/python3 /path/to/rs-video-stitch/mcp/sfx_mcp.py
```

No provider credentials are required. Put individually downloaded files in the
configured inbox, import them with their exact provider item URL, and then stage
them into a MediaStudio project.

## Validation

```bash
PYTHONPYCACHEPREFIX=/tmp/mediastudio-sfx-pycache \
  python3 -m unittest discover -s mcp/tests -p 'test_*.py'
```
