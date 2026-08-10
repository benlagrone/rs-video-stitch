# MediaStudio Source Integration Roadmap

Date: 2026-06-30

Source inputs:

- `/Users/benjaminlagrone/Documents/projects/.workspace/proposals/dataset-integrations-2026-06-26/mediastudio.md`
- `/Users/benjaminlagrone/Documents/projects/.workspace/indexes/free-public-apis/project-integrations/mediastudio.md`

## Objective

Build one rights-aware source layer for MediaStudio so creators can search,
review, import, cite, and reuse public-domain or clearly licensed media and
metadata in render projects. The first product surface is an artwork search and
import flow for scene images. Later surfaces add broad licensed media, NASA
imagery, public-domain text and book metadata, research source cards, and
optional demo metadata.

This roadmap is scoped to `rs-video-stitch`: Render API provider adapters,
render UI search/review surfaces, project state, selected asset storage, scene
manifest support, and local tests. Deployment, runtime routing, Docker Compose,
nginx, TLS, public ports, rollback, and release ownership remain outside this
roadmap unless explicitly requested.

## Current Anchor Points

- Render API stores project inputs under `input/`, including `scenes.json`,
  uploaded assets, voiceovers, room annotations, and project state.
- Render UI already has an image loading zone, scene assignment, project save,
  project restore, and render queue flow.
- The renderer expects local image files; external image selections should be
  copied into project storage before render.
- `ProjectSpec` supports scene metadata, but portable provider/source metadata
  should be added deliberately instead of hidden in ignored top-level fields.

## Integration Principles

- Normalize every provider behind one backend adapter interface.
- Call external APIs from the backend, not directly from production UI code.
- Fetch metadata first; download media only when the user selects an item.
- Store provenance beside the project, not only in browser state.
- Treat rights as item-level facts, not provider-level assumptions.
- Show uncertain rights before import and before render.
- Keep first adapters local to `rs-video-stitch`; extract to `local_tools` or a
  future data gateway only after real cross-project reuse or production needs.
- Never bulk-download provider collections by default.

## Core Contracts

### Provider Registry

Returned by `GET /v1/providers`.

```json
{
  "id": "aic",
  "label": "Art Institute of Chicago",
  "kind": "image",
  "requires_key": false,
  "supports_import": true,
  "rights_filter": "public_domain",
  "status": "enabled"
}
```

### AssetCandidate

Shared result shape for image/media providers.

```json
{
  "provider": "aic",
  "provider_id": "27992",
  "title": "A Sunday on La Grande Jatte",
  "creator": "Georges Seurat",
  "date": "1884-1886",
  "media_type": "image",
  "thumbnail_url": "https://...",
  "image_url": "https://...",
  "source_url": "https://...",
  "license": "Public Domain",
  "rights_status": "public_domain",
  "attribution": "Art Institute of Chicago",
  "query": {"q": "landscape", "provider": "aic"},
  "warnings": []
}
```

### Selected External Asset

Written after the user imports a candidate into a project.

```json
{
  "filename": "aic_27992.jpg",
  "candidate": {
    "provider": "aic",
    "provider_id": "27992",
    "source_url": "https://...",
    "license": "Public Domain",
    "attribution": "Art Institute of Chicago",
    "warnings": []
  },
  "selected_at": "2026-06-30T00:00:00Z",
  "review_status": "approved"
}
```

Persist selected media in:

- `input/asset_provenance.json`, keyed by project filename.
- project state as `externalAssets`, so the UI restores review state.
- a later portable manifest section such as `ProjectSpec.assets` once the
  source package shape is stable.

### SourceCard

Metadata-only result shape for books, papers, datasets, and demos.

```json
{
  "provider": "open_library",
  "provider_id": "OL123W",
  "title": "Example Work",
  "creator": "Example Author",
  "source_url": "https://...",
  "record_type": "book",
  "summary": "",
  "rights_status": "metadata_only",
  "warnings": ["Readable scan flags are not reuse permission."]
}
```

Persist metadata-only records in:

- `input/source_cards.json`.
- project state as `sourceCards`.

## Unified Provider Order

| Phase | Provider | Role | Reason for order |
|---|---|---|---|
| 0 | Fixture provider | Contract proof | Proves adapters, storage, and UI contracts without network dependency. |
| 1 | Art Institute of Chicago | Artwork image MVP | The public API note names AIC as the first experiment. |
| 2 | Met Museum | Historical/religious art expansion | Same public-domain artwork shape, strong fit for visual prompts and scene assets. |
| 3 | Wikimedia Commons | Broad licensed media | Useful breadth, but license handling must be strict. |
| 4 | NASA APIs / NASA CMR | Space, sky, climate, and Earth imagery | Good thematic media, mixed media/access patterns need careful labeling. |
| 5 | Gutendex / Project Gutenberg | Public-domain text sources | Script, narration, caption, and educational-video source cards. |
| 6 | Open Library | Book/work metadata | Enriches project metadata and covers only when rights are clear. |
| 7 | Crossref | DOI/citation metadata | Source cards for scholarly/documentary packages. |
| 8 | OpenAlex | Topic graphs and reading lists | Research planning and open-access signals, not direct media reuse. |
| 9 | TVMaze | Optional demo metadata | Demo/source cards only; not copyrighted media import. |
| 10 | Hugging Face datasets | Research-only dataset discovery | Dataset licenses vary; keep out of production asset import initially. |

## Phases

### Phase 0: Provider Framework And Persistence

Goal: create the shared foundation without changing render behavior.

- Add backend schemas for provider registry records, `AssetCandidate`,
  `ExternalAssetSelection`, and `SourceCard`.
- Add provider adapter interfaces under `render-api/app/integrations/`.
- Add `GET /v1/providers`.
- Add `GET /v1/assets/search?provider=aic&q=...`.
- Add `POST /v1/projects/{pid}/external-assets`.
- Add storage helpers for `input/asset_provenance.json`.
- Add storage helpers for `input/source_cards.json`.
- Normalize provider failures for invalid provider, timeout, rate limit, empty
  result, and rights-filter failure.
- Add fixture-backed tests for adapter normalization and persistence.

Exit criteria:

- A fake provider returns normalized candidates from fixtures.
- A fake selected asset can be persisted and restored.
- A fake source card can be persisted and restored.
- Existing render behavior is unchanged.

### Phase 1: Art Institute Of Chicago Artwork MVP

Goal: prove public-domain image search, import, assignment, and render.

- Implement the AIC adapter.
- Search public-domain artwork and generate IIIF image URLs.
- Require item-level public-domain status before marking a result as approved.
- Return title, artist, date, image URL, source URL, rights status, license, and
  attribution.
- Add an asset search panel near the image loading zone.
- Add preview cards with rights and attribution visible before import.
- Import one selected image into `input/images/`.
- Write selected asset provenance and project state `externalAssets`.

Verification terms:

- `landscape`
- `portrait`
- `religious`
- `city`

Exit criteria:

- Common terms return usable candidates when the provider has matches.
- Imported AIC images appear in the image loading zone.
- Imported images can be assigned to scenes and rendered.
- Project reload restores imported image metadata and review status.

### Phase 2: Met Museum Artwork Expansion

Goal: add a second public-domain artwork provider through the same contract.

- Implement the Met adapter using search plus object lookup.
- Filter to image-bearing public-domain records.
- Return normalized title, creator, date, image URL, source URL, license,
  attribution, and warnings.
- Add provider switching in the shared search panel.
- Keep Met-specific missing-image warnings in candidate records.

Verification terms:

- `painting`
- `moses`
- `christ`
- `landscape`

Exit criteria:

- AIC and Met both use `GET /v1/assets/search`.
- One UI card component renders both providers.
- Selected assets from both providers persist with distinct provider IDs.
- A render can use a selected asset from either provider.

### Phase 3: Wikimedia Commons Licensed Media

Goal: add broad media search with strict license handling.

- Implement Commons search plus Imageinfo/extmetadata lookup.
- Require file-level license, author, source page, MIME type, and dimensions
  before returning an importable media candidate.
- Add rights filters for public domain, permissive license, attribution
  required, and share-alike warning.
- Store attribution text in a reproducible form.
- Mark missing-license or unclear-license results as non-importable.

Exit criteria:

- Commons results without usable license metadata are excluded or clearly marked.
- Attribution-required and share-alike records display warnings before import.
- Imported Commons images retain author, license, source page, and accessed
  timestamp.

### Phase 4: NASA Media And Dataset Metadata

Goal: support space, sky, climate, Earth, and creation-themed visuals and
metadata without overclaiming reuse rights.

- Implement NASA API support for APOD and NASA media search slices.
- Add NASA CMR metadata search for Earth science themes such as `earth`,
  `climate`, and `solar`.
- Return media type, title, date, NASA center/creator when available, source
  URL, asset URL, attribution, and warnings.
- Import only image candidates into project assets.
- Save non-image or dataset records as source cards.

Exit criteria:

- NASA image candidates can be selected into projects.
- Non-image NASA and NASA CMR records save as source cards.
- NASA attribution and query context are preserved.

### Phase 5: Public-Domain Text And Book Sources

Goal: add source cards for scripts, captions, narration, and educational-video
metadata.

Gutendex / Project Gutenberg:

- Search `copyright=false`.
- Return title, author, language, subjects, text links, EPUB links, and source
  URL.
- Add text-source cards; do not auto-insert long text by default.

Open Library:

- Search by title/author.
- Return works, authors, editions, cover IDs, subjects, and source URLs.
- Treat covers and readable scan flags as metadata until rights are reviewed.
- Let users attach a book card and optionally use metadata in titles, captions,
  or YouTube-description drafts.

Exit criteria:

- Public-domain text records can be saved and restored as source cards.
- Book metadata can enrich project drafts without silently importing text or
  covers as render assets.
- Rights caveats are visible on Open Library cards.

### Phase 6: Documentary And Scholarly Research Sources

Goal: support source-backed educational and documentary packages.

Crossref:

- Resolve title/author queries to DOI candidates.
- Return DOI, title, authors, publisher, year, citation metadata, URL, and
  license fields when available.
- Use results for source cards and citation metadata, not full-text ingestion.

OpenAlex:

- Search works, authors, concepts, and topics.
- Attach open-access status and source URLs.
- Build lightweight reading lists and topic clusters for project planning.
- Display open-access status as a research signal, not a media reuse guarantee.

Exit criteria:

- A project can save DOI/OpenAlex source cards.
- Source cards export enough metadata to cite later.
- Research metadata stays separate from renderable media assets.

### Phase 7: Optional Demo Metadata

Goal: support media-related demos without importing copyrighted content.

TVMaze:

- Search show and episode metadata.
- Return show, episode, network, airdate, and source URL fields.
- Save records only as metadata source cards.
- Do not import posters, stills, scripts, clips, or other copyrighted media by
  default.

Exit criteria:

- TVMaze records can be attached to demo projects as metadata cards.
- No TVMaze media is downloaded into render inputs by default.
- Demo metadata is clearly separated from usable render assets.

### Phase 8: Dataset Discovery Lab

Goal: keep specialized datasets available for experimentation without treating
license-variable repositories as production asset sources.

Hugging Face datasets:

- Search dataset metadata and README/license fields.
- Save candidates as research notes/source cards.
- Label gated, non-commercial, or unclear-license datasets as research-only.
- Do not expose one-click production import until license and data format are
  explicitly reviewed.

Exit criteria:

- Dataset candidates can be attached to project notes.
- The UI labels dataset discovery as research-only.
- No unclear-license dataset becomes a render asset.

### Phase 9: Reuse And Gateway Decision

Goal: decide whether provider adapters stay local or move into shared workspace
tooling.

Evaluate extraction only after at least two projects need the same provider or
after a provider needs keys, caching, monitoring, production quotas, or shared
source-backed facts.

Options:

- Keep adapters in `rs-video-stitch` for MediaStudio-only workflow.
- Extract reusable provider code to `local_tools`.
- Route production-facing cross-project use through a future workspace data
  gateway.

Exit criteria:

- A written decision records which providers stay local and which move.
- MediaStudio-specific business logic remains inside MediaStudio.

## Implementation Workstreams

Backend:

- Provider registry and adapter interface.
- Shared `AssetCandidate` and `SourceCard` normalization.
- Search endpoint with provider filter, query, page/limit, and timeouts.
- External asset import endpoint that downloads one selected image into
  `input/images/`.
- Source-card persistence for metadata-only providers.
- Provider timeout, retry, rate-limit, and failure behavior.

UI:

- Provider search panel near the image loading zone.
- Provider and rights filters.
- Candidate cards with preview, creator/date, source, license, attribution,
  dimensions, and warnings.
- Add-to-project action for importable image assets.
- Selected asset provenance drawer or review controls.
- Source-card panel for text, book, research, dataset, and demo metadata.

Storage:

- `input/asset_provenance.json` for imported media.
- `input/source_cards.json` for metadata-only records.
- project state fields `externalAssets` and `sourceCards`.
- Later `ProjectSpec.assets` or `ProjectSpec.sources` for portable packages.

Testing:

- Fixture tests for each provider adapter.
- API tests for search, empty result, invalid provider, timeout, and import
  failure paths.
- Storage tests for provenance and source-card persistence.
- UI reload tests for imported assets, review statuses, and source cards.
- Render smoke tests with selected AIC and Met images.

Documentation:

- Provider-specific rights notes.
- Example provenance JSON for AIC, Met, Commons, and NASA.
- Example source-card JSON for Gutendex/Open Library/Crossref/OpenAlex/TVMaze.
- User-facing rights review language.
- Local-vs-shared-provider decision note.

## Milestone Plan

| Milestone | Scope | Providers | Expected result |
|---|---|---|---|
| M0 | Provider framework | Fixture provider | Contracts, storage, and tests exist with no render change. |
| M1 | Artwork MVP | AIC | Search, preview, import, persist, and render one selected artwork. |
| M2 | Artwork expansion | AIC, Met | One UI and backend contract support both art providers. |
| M3 | Licensed media | Wikimedia Commons | License-aware search with attribution and warnings. |
| M4 | Space/Earth media | NASA APIs, NASA CMR | Image candidates import; metadata records save as source cards. |
| M5 | Text/book sources | Gutendex, Open Library | Public-domain text and book metadata enrich scripts and packages. |
| M6 | Research sources | Crossref, OpenAlex | DOI and topic metadata power documentary source lists. |
| M7 | Demo metadata | TVMaze | Optional show/episode cards for demos only. |
| M8 | Dataset lab | Hugging Face datasets | Dataset discovery remains research-only. |
| M9 | Reuse decision | Any reused provider | Local vs shared gateway ownership is documented. |

## Near-Term Backlog

1. Add provider registry, candidate, selection, and source-card schemas.
2. Add provenance and source-card storage helpers.
3. Implement the fixture provider and persistence tests.
4. Implement the AIC adapter with fixture tests.
5. Add `GET /v1/providers`.
6. Add `GET /v1/assets/search` for AIC.
7. Add `POST /v1/projects/{pid}/external-assets`.
8. Add the UI search panel and candidate cards.
9. Persist imported AIC metadata in project state.
10. Render a short test project using one imported AIC image.
11. Add Met only after the AIC flow is stable.

## Risks And Mitigations

- License ambiguity: require item-level rights metadata and show warnings before
  import and render.
- Provider drift: keep fixture tests and explicit provider error handling.
- Browser exposure: route provider calls through the backend.
- Remote asset volatility: download selected render assets into project storage
  at import time.
- Manifest drift: use one provenance helper and one source-card helper.
- UI overload: separate importable media results from metadata-only source
  cards.
- Reuse overengineering: keep first adapters local until another project or
  production requirement justifies extraction.
- Deployment drift: do not add runtime routing or operational compose in this
  repo as part of source integration planning.

## Definition Of Done For The Full Track

- Users can search artwork, licensed media, NASA imagery, public-domain text,
  book metadata, scholarly metadata, demo metadata, and research-only datasets
  from MediaStudio through backend provider adapters.
- Importable image assets are reviewed, copied into project storage, assigned to
  scenes, and rendered through the existing FFmpeg path.
- Every imported asset stores provider, provider ID, source URL, accessed
  timestamp, query context, license/rights fields, attribution, and warnings.
- Metadata-only records save as source cards and never become render assets by
  default.
- Projects reopen with imported assets, source cards, and review statuses
  intact.
- No provider integration performs bulk downloads by default.
- Local-vs-shared-provider ownership is documented after real reuse pressure
  appears.
