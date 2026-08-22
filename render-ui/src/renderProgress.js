const PHASE_PROGRESS = {
  idle: 0,
  saving: 0.03,
  naming: 0.08,
  deleting: 0.12,
  queueing: 0.18,
  rendering: 0.18,
  complete: 1,
  failed: 0,
};

const STAGE_LABELS = {
  QUEUED: 'Waiting for the render worker',
  VALIDATE: 'Validating project assets',
  AUDIO_PREP: 'Preparing narration and scene media',
  CONCAT: 'Assembling the final video',
  FINALIZE: 'Finalizing video and thumbnail',
  ERROR: 'Render failed',
};

export const EMPTY_RENDER_TRACKER = {
  phase: 'idle',
  progress: 0,
  message: '',
  stage: '',
  jobId: '',
  willDelete: false,
  failedFrom: '',
};

export function renderTrackerProgress({ phase = 'idle', jobProgress = 0, progress } = {}) {
  if (Number.isFinite(progress)) return Math.max(0, Math.min(1, progress));
  if (phase === 'rendering') {
    return 0.18 + Math.max(0, Math.min(1, Number(jobProgress) || 0)) * 0.80;
  }
  return PHASE_PROGRESS[phase] ?? 0;
}

export function renderStageLabel(stage = '') {
  if (STAGE_LABELS[stage]) return STAGE_LABELS[stage];
  const sceneMatch = String(stage).match(/^SCENE_BUILD\[(\d+)]$/);
  if (sceneMatch) return `Building scene ${Number(sceneMatch[1]) + 1}`;
  return stage ? String(stage).replaceAll('_', ' ').toLowerCase() : 'Preparing render';
}

export function renderStepStates({ phase = 'idle', willDelete = false, failedFrom = '' } = {}) {
  const failed = phase === 'failed';
  const effectivePhase = failed ? failedFrom : phase;
  const phaseIndex = ['saving', 'naming', 'deleting', 'queueing', 'rendering', 'complete'].indexOf(effectivePhase);
  const step = (index, skipped = false) => {
    if (skipped) return 'skipped';
    if (failed && phaseIndex === index) return 'failed';
    if (failed && phaseIndex > index) return 'complete';
    if (failed) return 'pending';
    if (phase === 'complete' || phaseIndex > index) return 'complete';
    if (phaseIndex === index) return 'active';
    return 'pending';
  };

  return [
    step(0),
    step(1),
    step(2, !willDelete),
    step(3),
    step(4),
    step(5),
  ];
}
