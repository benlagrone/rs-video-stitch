const MAX_INTRO_LINES = 5;

export function leadLinesFromState(state, fallbackTitle = '') {
  const savedLines = Array.isArray(state.introLines) ? state.introLines : [];
  const sourceLines = savedLines.length
    ? savedLines
    : String(state.introTitle || fallbackTitle || '').split(/\r?\n/);
  return Array.from(
    { length: MAX_INTRO_LINES },
    (_, index) => String(sourceLines[index] || ''),
  );
}

export function sceneImageAssignmentsFromProject(state, persistedScenes) {
  const savedAssignments = Array.isArray(state.sceneImageAssignments)
    ? state.sceneImageAssignments
    : [];
  const savedAssignmentsAreRenderable = savedAssignments.length > 0
    && savedAssignments.every((sceneImages) => Array.isArray(sceneImages) && sceneImages.length > 0);

  if (savedAssignmentsAreRenderable) return savedAssignments;

  return (Array.isArray(persistedScenes) ? persistedScenes : [])
    .map((scene) => (Array.isArray(scene?.images) ? scene.images : []));
}

