const MAX_INTRO_LINES = 5;

const MANDARIN_TITLE_STYLE = {
  fill: '#ffffff',
  fontFile: 'input/logo/Arial-Unicode.ttf',
  fontSize: 58,
  outline: '#000000',
  position: 'top-center',
};

export function sanitizeSceneImageAssignments(assignments, availableNames) {
  const available = new Set(availableNames);
  return (Array.isArray(assignments) ? assignments : []).map((sceneImages) => (
    (Array.isArray(sceneImages) ? sceneImages : [])
      .filter((name) => available.has(name))
      .slice(0, 3)
  ));
}

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

export function renderOptionsFromProject(state) {
  const savedOptions = state?.renderOptions && typeof state.renderOptions === 'object'
    ? state.renderOptions
    : {};
  const savedTitleStyle = savedOptions.titleStyle && typeof savedOptions.titleStyle === 'object'
    ? savedOptions.titleStyle
    : {};

  if (!String(state?.language || '').toLowerCase().startsWith('zh')) {
    return { ...savedOptions };
  }

  return {
    ...savedOptions,
    titleStyle: {
      ...MANDARIN_TITLE_STYLE,
      ...savedTitleStyle,
    },
  };
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
