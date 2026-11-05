const VIDEO_FILE = /\.(mp4|mov|m4v|webm|mpg|mpeg)$/i;

export function isTikTokOutput(filename, variantFilename = '') {
  return VIDEO_FILE.test(filename) && (filename === variantFilename || /^tiktok[_-]/i.test(filename));
}

export function tikTokPreviewOutputs(outputs = [], variantFilename = '') {
  const videos = outputs.filter((filename) => typeof filename === 'string' && VIDEO_FILE.test(filename));
  return [
    ...videos.filter((filename) => isTikTokOutput(filename, variantFilename)),
    ...videos.filter((filename) => !isTikTokOutput(filename, variantFilename)),
  ];
}

export function completedTikTokHighlight(state, outputs = []) {
  return state?.status === 'SUCCEEDED' && VIDEO_FILE.test(state.filename || '') && outputs.includes(state.filename)
    ? state.filename : '';
}

export function selectTikTokPreviewVideo(outputs = [], selected = '', variantFilename = '', latestHighlightFilename = '') {
  const videos = tikTokPreviewOutputs(outputs, variantFilename);
  if (videos.includes(selected)) return selected;
  if (videos.includes(variantFilename)) return variantFilename;
  if (videos.includes(latestHighlightFilename)) return latestHighlightFilename;
  return videos.find((filename) => isTikTokOutput(filename) && !/^tiktok_highlight(?:[_-]|\.)/i.test(filename)) || '';
}

export function refreshTikTokPreviewVideo(outputs = [], selected = '', variantFilename = '', latestHighlightFilename = '') {
  if (/^tiktok_highlight(?:[_-]|\.)/i.test(selected) && outputs.includes(latestHighlightFilename)) return latestHighlightFilename;
  return selectTikTokPreviewVideo(outputs, selected, variantFilename, latestHighlightFilename);
}
