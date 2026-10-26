const MAX_INTRO_LINES = 6;

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
  const titleSections = [];

  if (isAllCapsTitle || isExplicitTitle) {
    titleSections.push(candidate);
    for (let index = 1; index < sections.length; index += 1) {
      const sectionLetters = sections[index].match(/\p{L}/gu) || [];
      const isAllCapsContinuation = sectionLetters.length > 0
        && sectionLetters.some((letter) => letter !== letter.toLowerCase())
        && sectionLetters.every((letter) => letter === letter.toUpperCase());
      if (!isAllCapsContinuation) break;
      titleSections.push(sections[index]);
    }
  }

  const title = titleSections.join(' ').replace(/\s+/g, ' ').trim();
  return {
    title,
    narration: titleSections.length ? sections.slice(titleSections.length).join('\n\n') : sections.join('\n\n'),
  };
}

export function scriptWithNarrationSections(script, explicitTitle = '', sections = []) {
  const parsed = titleFromScript(script, explicitTitle);
  const narration = sections
    .map((section) => String(section || '').trim())
    .filter(Boolean)
    .join('\n\n');
  const inlineTitle = !String(explicitTitle || '').trim() && parsed.title
    && String(script || '').trim().startsWith(parsed.title);
  return `${inlineTitle ? `${parsed.title}\n\n` : ''}${narration}`.trim();
}

export function titleCardFromScript(script, explicitTitle = '') {
  const { title } = titleFromScript(script, explicitTitle);
  const segments = title.split('|').map((segment) => segment.replace(/\s+/g, ' ').trim()).filter(Boolean);
  return {
    mainTitle: segments[0] || title,
    lines: segments.slice(1),
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

export function fitSceneAssignmentsToNarration(assignments, narrationCount) {
  const count = Math.max(0, Number(narrationCount) || 0);
  if (!count || assignments.length <= count) return assignments;
  return Array.from({ length: count }, (_, index) => {
    const start = Math.floor((index * assignments.length) / count);
    const end = Math.floor(((index + 1) * assignments.length) / count);
    return assignments.slice(start, end).flat();
  });
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
