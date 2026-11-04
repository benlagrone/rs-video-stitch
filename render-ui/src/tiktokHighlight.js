const numberValue = (value) => (typeof value === 'string' && !value.trim()) ? NaN : Number(value);
const seconds = (value, fallback) => value == null ? fallback : numberValue(value);

export function highlightDuration(plan) {
  if (!Array.isArray(plan?.segments) || !plan.segments.length) return NaN;
  const footage = plan.segments.reduce((total, segment) => total + numberValue(segment.sourceEnd) - numberValue(segment.sourceStart), 0);
  const graphics = plan.graphics || {};
  const total = footage + seconds(graphics.hookSeconds, 2) + seconds(graphics.outroSeconds, 4)
    + seconds(graphics.transitionSeconds, 0.4) * Math.max(0, plan.segments.length - 1);
  return Math.round(total * 1000) / 1000;
}

export function highlightPlanErrors(plan) {
  if (!plan) return [];
  const errors = [];
  if (!String(plan.title || '').trim()) errors.push('Add a highlight title.');
  if (!String(plan.hook || '').trim()) errors.push('Add an opening hook.');
  if (!Array.isArray(plan.segments) || !plan.segments.length) errors.push('The highlight needs at least one clip.');
  (plan.segments || []).forEach((segment, index) => {
    const start = numberValue(segment.sourceStart);
    const end = numberValue(segment.sourceEnd);
    if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start) {
      errors.push(`Clip ${index + 1} needs an end time after its start time.`);
    } else if (Number.isFinite(Number(plan.sourceDuration)) && end > Number(plan.sourceDuration) + 0.05) {
      errors.push(`Clip ${index + 1} ends after the source video.`);
    }
    if (!String(segment.caption || '').trim()) errors.push(`Add a project-text caption for clip ${index + 1}.`);
  });
  const duration = highlightDuration(plan);
  if (!Number.isFinite(duration) || duration < 30 - 0.005 || duration > 60 + 0.005) {
    errors.push('Keep the complete highlight, including its HyperFrames cards, between 30 and 60 seconds.');
  }
  return errors;
}

export function normalizeHighlightPlan(plan) {
  const normalized = {
    ...plan,
    title: String(plan.title || '').trim(),
    hook: String(plan.hook || '').trim(),
    contact: Object.fromEntries(Object.entries(plan.contact || {}).map(([key, value]) => [key, String(value ?? '').trim()])),
    segments: (plan.segments || []).map((segment) => ({
      ...segment,
      sourceStart: numberValue(segment.sourceStart),
      sourceEnd: numberValue(segment.sourceEnd),
      caption: String(segment.caption || '').trim(),
    })),
  };
  normalized.durationSeconds = Math.round(highlightDuration(normalized) * 100) / 100;
  return normalized;
}

export function mergeHighlightJob(previous, incoming) {
  const progress = Math.max(0, Math.min(1, Number(incoming.progress) || 0));
  return {
    ...incoming,
    progress: incoming.status === 'SUCCEEDED' ? 1
      : Math.max(previous?.jobId === incoming.jobId ? Number(previous.progress) || 0 : 0, progress),
  };
}
