# MediaStudio Roadmap

## Cleanup And Bugs

- Support multiline text in the title section and generated thumbnail instead of a single line.

## Active

- Keep render logs mounted continuously while a job runs, even when the latest poll has no new log bytes.
- Capture per-image room labels and notes as project input data in `room_annotations.csv`.
- Let MediaStudio call the `room_renamer` classifier to auto-name rooms from uploaded images.
- Feed saved room labels, headers, and room notes into the Ollama script enhancer.

## Next

- Add a Fortress-managed `room_renamer` service profile so room classification does not depend on a separately started local API.
- Convert saved room annotations into curated `room_renamer/data/train/<room>/` examples.
- Add thumbnail and YouTube metadata defaults that reuse room names and project fields.
- Add a render history view for durable output and publication events. Current
  project state already records the latest YouTube video and thumbnail result.

## Later

- Train and select room classifier versions from the MediaStudio UI.
- Add batch import from listing pages into the upload grid.
- Add per-image scene duration controls and room-aware scene ordering.
