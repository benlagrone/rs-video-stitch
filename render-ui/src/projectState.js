const MAX_INTRO_LINES = 5;

const MANDARIN_TITLE_STYLE = {
  fill: '#ffffff',
  fontFile: 'input/logo/Arial-Unicode.ttf',
  fontSize: 58,
  outline: '#000000',
  position: 'top-center',
};

export function titleFromScript(script, explicitTitle = '') {
  const sections = String(script || '')
    .split(/\n{2,}/)
    .map((section) => section.trim())
    .filter(Boolean);
  const candidate = sections[0] || '';
  const letters = candidate.match(/\p{L}/gu) || [];
  const isAllCapsTitle = letters.length >= 8
    && letters.some((letter) => letter !== letter.toLowerCase())
    && letters.every((letter) => letter === letter.toUpperCase());
  const isExplicitTitle = candidate === String(explicitTitle || '').trim();
  return {
    title: (isAllCapsTitle || isExplicitTitle) ? candidate : '',
    narration: (isAllCapsTitle || isExplicitTitle) ? sections.slice(1).join('\n\n') : sections.join('\n\n'),
  };
}

export function sanitizeSceneImageAssignments(assignments, availableNames) {
  const available = new Set(availableNames);
  return (Array.isArray(assignments) ? assignments : []).map((sceneImages) => (
    (Array.isArray(sceneImages) ? sceneImages : [])
      .filter((name) => available.has(name))
      .slice(0, 3)
  ));
}

export function sceneAssignmentsFromRoomInfo(images, imageRoomInfo) {
  const groups = new Map();
  (Array.isArray(images) ? images : []).forEach((image) => {
    const name = typeof image === 'string' ? image : image?.name;
    if (!name) return;
    const room = String(imageRoomInfo?.[name]?.label || 'unclassified').trim() || 'unclassified';
    if (!groups.has(room)) groups.set(room, []);
    groups.get(room).push(name);
  });

  return Array.from(groups.values()).flatMap((names) => {
    const scenes = [];
    for (let index = 0; index < names.length; index += 3) scenes.push(names.slice(index, index + 3));
    return scenes;
  });
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
