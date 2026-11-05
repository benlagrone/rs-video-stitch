import { useEffect, useMemo, useRef, useState } from 'react';
import { highlightDuration, highlightPlanErrors, normalizeHighlightPlan, mergeHighlightJob } from './tiktokHighlight.js';
import './TikTokHighlightPanel.css';

const VIDEO_FILE = /\.(mp4|mov|m4v|webm|mpg|mpeg)$/i;
const ACTIVE = new Set(['QUEUED', 'RUNNING']);
const STAGES = {
  QUEUED: 'Waiting for the render worker',
  VALIDATE: 'Checking the source video and highlight plan',
  HIGHLIGHT_VALIDATE: 'Checking the source video and highlight plan',
  HIGHLIGHT_CUTS: 'Preparing the selected clips',
  HYPERFRAMES: 'Rendering vertical HyperFrames',
  HIGHLIGHT_GRAPHICS: 'Rendering vertical HyperFrames',
  CAPTIONS: 'Adding project-text captions',
  HIGHLIGHT_CAPTIONS: 'Adding project-text captions',
  CAPTION_TRANSCRIBE: 'Transcribing recorded audio with Whisper',
  CAPTION_TIMING: 'Timing narration-script captions',
  CAPTION_BURN: 'Burning captions into video',
  CONCAT: 'Assembling the highlight',
  HIGHLIGHT_ASSEMBLE: 'Assembling the highlight',
  FINALIZE: 'Checking the completed highlight',
};
const prettyStage = (stage) => STAGES[stage] || String(stage || 'Preparing highlight').replaceAll('_', ' ').toLowerCase();
const message = (error) => error?.message || String(error);

export function TikTokHighlightPanel({ projectId, outputs = [], defaultSource = '', spokenCaptionsEnabled = false, request, onCreated, outputHref }) {
  const [source, setSource] = useState('');
  const [targetDuration, setTargetDuration] = useState(45);
  const [plan, setPlan] = useState(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState('');
  const [job, setJob] = useState(null);
  const [filename, setFilename] = useState('');
  const [captionFilename, setCaptionFilename] = useState('');
  const [editorUrl, setEditorUrl] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [pollRetry, setPollRetry] = useState(0);
  const [connectionPaused, setConnectionPaused] = useState(false);
  const callbacks = useRef({ request, onCreated });
  const currentProject = useRef(projectId);
  const delivered = useRef('');
  callbacks.current = { request, onCreated };
  currentProject.current = projectId;
  const base = `/v1/projects/${encodeURIComponent(projectId)}/tiktok/highlight`;
  const videos = useMemo(() => outputs.filter((file) => VIDEO_FILE.test(file)), [outputs]);
  const active = ACTIVE.has(job?.status);
  const editingDisabled = Boolean(busy) || active;
  const duration = highlightDuration(plan);
  const errors = useMemo(() => highlightPlanErrors(plan), [plan]);

  function deliverCompleted(state, { notifyParent = true } = {}) {
    if (state.filename) setFilename(state.filename);
    setCaptionFilename(state.captionFilename || '');
    setEditorUrl(state.editorUrl || state.graphicsEditorUrl || '');
    const key = `${projectId}:${state.jobId || ''}:${state.filename || ''}`;
    if (state.filename && delivered.current !== key) {
      delivered.current = key;
      if (notifyParent) callbacks.current.onCreated?.(state.filename);
    }
  }

  useEffect(() => {
    let cancelled = false;
    setPlan(null);
    setJob(null);
    setFilename('');
    setCaptionFilename('');
    setEditorUrl('');
    setDirty(false);
    setError('');
    setNotice('');
    setBusy('loading');
    setConnectionPaused(false);
    delivered.current = '';
    setSource(videos.includes(defaultSource) ? defaultSource : videos.find((file) => !file.startsWith('tiktok_')) || videos[0] || '');
    if (!projectId) {
      setBusy('');
      return () => { cancelled = true; };
    }
    callbacks.current.request(base).then((state) => {
      if (cancelled) return;
      if (state.plan) {
        setPlan(state.plan);
        setSource(state.plan.sourceFilename || '');
        setTargetDuration([30, 45, 60].includes(state.plan.durationSeconds) ? state.plan.durationSeconds : 45);
      }
      if (state.jobId) setJob(mergeHighlightJob(null, state));
      // Loading an older highlight must not replace the project's selected preview.
      if (state.status === 'SUCCEEDED') deliverCompleted(state, { notifyParent: false });
      if (state.error) setError(state.error);
    }).catch((err) => {
      if (!cancelled) setError(`Could not load the saved highlight: ${message(err)}`);
    }).finally(() => {
      if (!cancelled) setBusy('');
    });
    return () => { cancelled = true; };
    // Project selection owns this load. Callback functions and output refreshes must not reset an editor draft.
  }, [projectId]);

  useEffect(() => {
    if (!source && videos.length) setSource(videos.includes(defaultSource) ? defaultSource : videos[0]);
  }, [source, defaultSource, videos]);

  useEffect(() => {
    if (!job?.jobId || !ACTIVE.has(job.status)) return undefined;
    let cancelled = false;
    let timeout;
    let failures = 0;
    const expectedProject = projectId;
    const jobId = job.jobId;
    async function poll() {
      try {
        const current = await callbacks.current.request(`/v1/jobs/${encodeURIComponent(jobId)}`);
        if (cancelled || currentProject.current !== expectedProject) return;
        setConnectionPaused(false);
        if (current.status === 'SUCCEEDED') {
          const completed = await callbacks.current.request(base);
          if (cancelled || currentProject.current !== expectedProject) return;
          if (completed.jobId && completed.jobId !== jobId) throw new Error('Another highlight render was started for this project. Reopen the project to view its latest progress.');
          if (!completed.filename) throw new Error('The render completed, but its highlight file is not available yet. Check progress again.');
          setJob((previous) => mergeHighlightJob(previous, current));
          deliverCompleted({ ...completed, jobId });
          setNotice('Your TikTok highlight is ready. The YouTube master is unchanged.');
          setError('');
          return;
        }
        failures = 0;
        setJob((previous) => mergeHighlightJob(previous, current));
        if (current.status === 'FAILED' || current.status === 'CANCELLED') {
          setError(current.error || `Highlight render ${current.status.toLowerCase()}.`);
          return;
        }
      } catch (err) {
        if (cancelled) return;
        failures += 1;
        if (failures >= 5) {
          setError(`Progress updates are paused: ${message(err)}`);
          setConnectionPaused(true);
          return;
        }
      }
      timeout = setTimeout(poll, 1800);
    }
    poll();
    return () => { cancelled = true; clearTimeout(timeout); };
  }, [projectId, job?.jobId, active, pollRetry]);

  function editPlan(update) {
    setPlan((previous) => {
      const next = update(previous);
      const nextDuration = highlightDuration(next);
      return Number.isFinite(nextDuration) ? { ...next, durationSeconds: Math.round(nextDuration * 100) / 100 } : next;
    });
    setDirty(true);
    setNotice('');
  }

  async function generatePlan(renderAutomatically = false) {
    const expectedProject = projectId;
    const renderCaptionsEnabled = Boolean(spokenCaptionsEnabled);
    setBusy(renderAutomatically ? 'automatic' : 'planning');
    setError('');
    setNotice('');
    try {
      const result = await callbacks.current.request(`${base}/plan`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: source, durationSeconds: targetDuration }),
      });
      if (currentProject.current !== expectedProject) return;
      setPlan(result.plan);
      setDirty(false);
      if (renderAutomatically) {
        setBusy('queueing');
        const queued = await callbacks.current.request(`${base}/render`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ plan: { ...result.plan, spokenCaptionsEnabled: renderCaptionsEnabled } }),
        });
        if (currentProject.current !== expectedProject) return;
        setFilename('');
        setCaptionFilename('');
        setEditorUrl('');
        setConnectionPaused(false);
        setJob({ jobId: queued.jobId, status: 'QUEUED', stage: 'QUEUED', progress: 0 });
      } else {
        setNotice('Highlight plan generated and saved. You can edit every clip, caption and contact line below.');
      }
    } catch (err) {
      if (currentProject.current === expectedProject) setError(message(err));
    } finally {
      if (currentProject.current === expectedProject) setBusy('');
    }
  }

  async function savePlan() {
    const expectedProject = projectId;
    const savedPlan = { ...normalizeHighlightPlan(plan), spokenCaptionsEnabled: Boolean(spokenCaptionsEnabled) };
    setBusy('saving');
    setError('');
    try {
      const result = await callbacks.current.request(`${base}/plan`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan: savedPlan }),
      });
      if (currentProject.current !== expectedProject) return;
      setPlan(result.plan || savedPlan);
      setDirty(false);
      setNotice('Highlight plan saved. Render it to update the highlight video.');
    } catch (err) {
      if (currentProject.current === expectedProject) setError(message(err));
    } finally {
      if (currentProject.current === expectedProject) setBusy('');
    }
  }

  async function renderHighlight() {
    const expectedProject = projectId;
    setBusy('queueing');
    setError('');
    setNotice('');
    try {
      const result = await callbacks.current.request(`${base}/render`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan: { ...normalizeHighlightPlan(plan), spokenCaptionsEnabled: Boolean(spokenCaptionsEnabled) } }),
      });
      if (currentProject.current !== expectedProject) return;
      setDirty(false);
      setFilename('');
      setCaptionFilename('');
      setEditorUrl('');
      setConnectionPaused(false);
      setJob({ jobId: result.jobId, status: 'QUEUED', stage: 'QUEUED', progress: 0 });
    } catch (err) {
      if (currentProject.current === expectedProject) setError(message(err));
    } finally {
      if (currentProject.current === expectedProject) setBusy('');
    }
  }

  const videoUrl = filename ? (outputHref?.(filename) || `/v1/projects/${encodeURIComponent(projectId)}/outputs/video?filename=${encodeURIComponent(filename)}`) : '';
  const previewUrl = videoUrl ? `${videoUrl}${videoUrl.includes('?') ? '&' : '?'}highlight=${encodeURIComponent(job?.jobId || filename)}` : '';
  const captionUrl = captionFilename ? (outputHref?.(captionFilename) || `/v1/projects/${encodeURIComponent(projectId)}/outputs/video?filename=${encodeURIComponent(captionFilename)}`) : '';

  return (
    <section className="tiktok-highlight" aria-label="Automatic TikTok highlight">
      <div className="tiktok-highlight__heading">
        <h4>Automatic TikTok highlight</h4><span>30–60 seconds · 9:16</span>
      </div>
      <p>Build a separate highlight with vertical HyperFrames, project-text captions and Jie’s contact card. Each shot uses its original portrait or landscape framing; verified padding is removed. Your original YouTube video stays unchanged. Spoken subtitles follow the Video captions setting above.</p>
      <div className="tiktok-highlight__controls">
        <label className="tiktok-highlight__field">Highlight source video
          <select value={source} onChange={(event) => { setSource(event.target.value); setPlan(null); setDirty(false); setNotice(''); }} disabled={editingDisabled}>
            <option value="">Choose a finished video</option>
            {videos.map((file) => <option key={file} value={file}>{file}</option>)}
          </select>
        </label>
        <label className="tiktok-highlight__field">Target highlight length
          <select value={targetDuration} onChange={(event) => setTargetDuration(Number(event.target.value))} disabled={editingDisabled}>
            <option value={30}>30 seconds</option><option value={45}>45 seconds</option><option value={60}>60 seconds</option>
          </select>
        </label>
      </div>
      <div className="tiktok-highlight__actions">
        <button type="button" className="secondary-action" onClick={() => generatePlan(true)} disabled={editingDisabled || !projectId || !videos.includes(source)}>{busy === 'automatic' ? 'Planning highlight…' : busy === 'queueing' ? 'Queueing highlight…' : 'Create automatic highlight'}</button>
        <button type="button" className="secondary-action" onClick={() => generatePlan(false)} disabled={editingDisabled || !projectId || !videos.includes(source)}>{busy === 'planning' ? 'Planning highlight…' : 'Generate editable plan'}</button>
      </div>
      {plan && <>
        <p className="tiktok-highlight__hint">Creating an automatic highlight or generating a plan replaces this plan’s edits. Use Render edited highlight to keep your changes. Captions use the project’s written information; they are feature overlays, not a verbatim speech transcript.</p>
        <div className="tiktok-highlight__heading">
          <strong>Highlight plan</strong><span>{Number.isFinite(duration) ? `${duration.toFixed(1)} seconds total` : 'Check clip times'}{dirty ? ' · Unsaved edits' : ' · Saved'}</span>
        </div>
        <label className="tiktok-highlight__field">Highlight title
          <input value={plan.title || ''} maxLength={120} disabled={editingDisabled} onChange={(event) => editPlan((current) => ({ ...current, title: event.target.value }))} />
        </label>
        <label className="tiktok-highlight__field">Opening hook
          <textarea value={plan.hook || ''} maxLength={120} rows={2} disabled={editingDisabled} onChange={(event) => editPlan((current) => ({ ...current, hook: event.target.value }))} />
        </label>
        <fieldset className="tiktok-highlight__contact" disabled={editingDisabled}>
          <legend>Jie’s contact card</legend>
          {[['brokerage', 'Brokerage line'], ['broker', 'Broker line'], ['phone', 'Phone'], ['email', 'Email (optional)']].map(([key, label]) => <label key={key} className="tiktok-highlight__field">{label}
            <input type={key === 'email' ? 'email' : key === 'phone' ? 'tel' : 'text'} maxLength={120} value={plan.contact?.[key] || ''} onChange={(event) => editPlan((current) => ({ ...current, contact: { ...current.contact, [key]: event.target.value } }))} />
          </label>)}
        </fieldset>
        <div className="tiktok-highlight__clips">
          {(plan.segments || []).map((segment, index) => <fieldset key={segment.id || index} className="tiktok-highlight__clip" disabled={editingDisabled}>
            <legend>Clip {index + 1}</legend>
            <div className="tiktok-highlight__times">
              <label className="tiktok-highlight__field">Source start (seconds)<input type="number" min="0" step="0.1" value={segment.sourceStart ?? ''} onChange={(event) => editPlan((current) => ({ ...current, segments: current.segments.map((item, itemIndex) => itemIndex === index ? { ...item, sourceStart: event.target.value } : item) }))} /></label>
              <label className="tiktok-highlight__field">Source end (seconds)<input type="number" min="0" max={plan.sourceDuration || undefined} step="0.1" value={segment.sourceEnd ?? ''} onChange={(event) => editPlan((current) => ({ ...current, segments: current.segments.map((item, itemIndex) => itemIndex === index ? { ...item, sourceEnd: event.target.value } : item) }))} /></label>
            </div>
            <label className="tiktok-highlight__field">Project-text caption
              <textarea value={segment.caption || ''} maxLength={180} rows={2} onChange={(event) => editPlan((current) => ({ ...current, segments: current.segments.map((item, itemIndex) => itemIndex === index ? { ...item, caption: event.target.value } : item) }))} />
            </label>
          </fieldset>)}
        </div>
        {!!errors.length && <ul className="tiktok-highlight__validation" role="status">{errors.map((item) => <li key={item}>{item}</li>)}</ul>}
        <div className="tiktok-highlight__actions">
          <button type="button" className="secondary-action" onClick={savePlan} disabled={editingDisabled || !!errors.length || !dirty}>{busy === 'saving' ? 'Saving…' : 'Save highlight plan'}</button>
          <button type="button" className="secondary-action" onClick={renderHighlight} disabled={editingDisabled || !!errors.length}>{busy === 'queueing' ? 'Queueing…' : job?.status === 'FAILED' ? 'Retry highlight render' : 'Render edited highlight'}</button>
        </div>
      </>}
      {job && <div className="tiktok-highlight__progress" role="status" aria-live="polite">
        <div><strong>{job.status === 'SUCCEEDED' ? 'Highlight ready' : job.status === 'FAILED' ? 'Highlight render failed' : prettyStage(job.stage)}</strong><span>{Math.round((job.progress || 0) * 100)}%</span></div>
        <progress max="1" value={job.progress || 0} aria-label="TikTok highlight render progress" />
        <small>Job {job.jobId} · {job.status}</small>
        {connectionPaused && <button type="button" className="secondary-action" onClick={() => { setError(''); setConnectionPaused(false); setPollRetry((value) => value + 1); }}>Check highlight progress</button>}
      </div>}
      {error && <p className="tiktok-highlight__error" role="alert">{error}</p>}
      {notice && <p className="tiktok-highlight__notice" role="status">{notice}</p>}
      {filename && <div className="tiktok-highlight__result">
        <strong>Completed highlight: {filename}</strong>
        {dirty && <p className="tiktok-highlight__hint">This preview shows the last completed render. Render again to apply your edits.</p>}
        <video key={previewUrl} controls playsInline preload="metadata" src={previewUrl} aria-label="TikTok highlight preview" />
        <div className="tiktok-highlight__actions"><a href={previewUrl} target="_blank" rel="noreferrer">Open highlight video</a><a href={previewUrl} download={filename}>Download highlight</a>{captionUrl && <a href={captionUrl} download={captionFilename}>Download captions (SRT)</a>}{editorUrl && <a href={editorUrl} target="_blank" rel="noreferrer">Edit vertical HyperFrames</a>}</div>
        <p className="tiktok-highlight__hint">Review this highlight in Video to publish, then check the TikTok account and post settings below before publishing.</p>
      </div>}
    </section>
  );
}
