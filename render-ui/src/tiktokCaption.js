export function captionContextKey(projectId, filename, source = {}) {
  return JSON.stringify([projectId, filename, source.title, source.propertyAddress,
    source.originalScriptText, source.script, source.language]);
}

export function shouldAutoGenerateCaption({ loaded, caption, hasVideo, sourceText, attempted }) {
  return Boolean(loaded && !String(caption || '').trim() && hasVideo
    && String(sourceText || '').trim() && !attempted);
}

// A provider response belongs to one source context and one draft revision.
// Invalidate synchronously on typing, before React's next render/effect.
export function createCaptionGenerationGuard() {
  let context = '';
  let revision = 0;
  return {
    context(value) {
      if (value !== context) { context = value; revision += 1; }
    },
    invalidate() { revision += 1; },
    begin() { revision += 1; return { context, revision }; },
    current(token) { return token.context === context && token.revision === revision; },
  };
}
