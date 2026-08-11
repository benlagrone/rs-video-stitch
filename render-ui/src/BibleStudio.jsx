import { useEffect, useMemo, useState } from 'react';

const FALLBACK_STYLES = [
  { id: 'cinematic-natural-light', name: 'Cinematic natural light', category: 'Sacred & historical', prompt: 'Cinematic natural light and grounded historical realism' },
  { id: 'classical-oil-painting', name: 'Classical oil painting', category: 'Sacred & historical', prompt: 'Layered pigments and museum-quality composition' },
  { id: 'fortress-grid-illustration', name: 'Fortress Grid illustration', category: 'General art & illustration', prompt: 'Clean geometric forms and a restrained palette' },
  { id: 'historical-documentary', name: 'Historical documentary', category: 'Sacred & historical', prompt: 'Authentic material culture and natural available light' },
];

function apiUrl(baseUrl, path) {
  if (/^https?:\/\//i.test(path)) return path;
  return `${baseUrl.replace(/\/+$/, '')}${path}`;
}

function stageLabel(stage) {
  return String(stage || 'QUEUED').replaceAll('_', ' ').toLowerCase().replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function BibleStudio({ authToken, theme, onBack, onOpenProjects }) {
  const effectiveApiBase = window.location.port === '3000'
    ? `${window.location.protocol}//${window.location.hostname}:8082`
    : '';
  const [passage, setPassage] = useState('Genesis 3:1-6');
  const [translation, setTranslation] = useState('kjv');
  const [mode, setMode] = useState('motion');
  const [visualStyle, setVisualStyle] = useState(FALLBACK_STYLES[0].id);
  const [visualStyles, setVisualStyles] = useState(FALLBACK_STYLES);
  const [styleQuery, setStyleQuery] = useState('');
  const [voice, setVoice] = useState('Carter');
  const [job, setJob] = useState(null);
  const [project, setProject] = useState(null);
  const [health, setHealth] = useState({});
  const [error, setError] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [publishReview, setPublishReview] = useState(false);
  const [isPublishing, setIsPublishing] = useState(false);
  const [youtubeResult, setYoutubeResult] = useState('');
  const [youtubeTitle, setYoutubeTitle] = useState('');
  const [youtubeDescription, setYoutubeDescription] = useState('');
  const [youtubePrivacy, setYoutubePrivacy] = useState('private');

  const headers = (extra = {}) => ({
    ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
    ...extra,
  });

  async function request(path, options = {}) {
    const response = await fetch(apiUrl(effectiveApiBase, path), { ...options, headers: headers(options.headers || {}) });
    if (!response.ok) {
      const detail = await response.text().catch(() => '');
      throw new Error(`${response.status} ${response.statusText}${detail ? `: ${detail}` : ''}`);
    }
    return response.json();
  }

  useEffect(() => {
    request('/v1/bible/health').then(setHealth).catch(() => setHealth({ mediastudio: { ok: false } }));
  }, [effectiveApiBase, authToken]);

  useEffect(() => {
    request('/v1/bible/styles').then((result) => {
      if (result.styles?.length) setVisualStyles(result.styles);
    }).catch(() => {});
  }, [effectiveApiBase, authToken]);

  useEffect(() => {
    if (!job?.jobId || ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(job.status)) return undefined;
    const timer = window.setInterval(async () => {
      try {
        const current = await request(`/v1/jobs/${encodeURIComponent(job.jobId)}`);
        setJob((previous) => ({ ...previous, ...current }));
        if (current.status === 'SUCCEEDED') {
          const loaded = await request(`/v1/projects/${encodeURIComponent(job.projectId)}`);
          setProject(loaded);
          const title = loaded.state?.youtubeTitle || loaded.state?.title || passage;
          setYoutubeTitle(title);
          setYoutubeDescription(loaded.state?.youtubeDescription || `A narrated visual presentation of ${passage}.`);
        }
      } catch (pollError) {
        setError(pollError.message || String(pollError));
      }
    }, 1500);
    return () => window.clearInterval(timer);
  }, [job?.jobId, job?.status, job?.projectId, effectiveApiBase, authToken]);

  const scenes = project?.scenes?.scenes || [];
  const outputName = project?.state?.outputName || 'video.mp4';
  const videoHref = project ? apiUrl(effectiveApiBase, `/v1/projects/${encodeURIComponent(project.projectId)}/outputs/video?filename=${encodeURIComponent(outputName)}`) : '';
  const progress = Math.round((job?.progress || 0) * 100);
  const healthRows = [
    ['Sextant Orchestrator', health.sextant],
    ['MediaStudio', health.mediastudio],
    ['Fortress Image GPU', health.image],
    ['Fortress Wan / ComfyUI model', health.motion],
  ];
  const selectedStyle = visualStyles.find((style) => style.id === visualStyle) || visualStyles[0];
  const filteredStyles = useMemo(() => {
    const query = styleQuery.trim().toLowerCase();
    if (!query) return visualStyles;
    return visualStyles.filter((style) => (
      `${style.name} ${style.category} ${style.prompt}`.toLowerCase().includes(query)
      || style.id === visualStyle
    ));
  }, [styleQuery, visualStyle, visualStyles]);
  const styleGroups = useMemo(() => filteredStyles.reduce((groups, style) => {
    const category = style.category || 'Other';
    return { ...groups, [category]: [...(groups[category] || []), style] };
  }, {}), [filteredStyles]);

  async function buildVideo(event) {
    event.preventDefault();
    setError('');
    setProject(null);
    setYoutubeResult('');
    setIsSubmitting(true);
    try {
      const created = await request('/v1/bible/videos', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          passage, translation, mode, visualStyle, voice,
          language: 'en-US', ttsApi: 'voice-gateway', outputName: 'video.mp4',
          renderOptions: { tts: voice, ttsLanguage: 'en-US', ttsApi: 'voice-gateway', introEnabled: true, introTitle: passage, logoEnabled: true },
        }),
      });
      setJob({ ...created, status: 'QUEUED', stage: 'QUEUED', progress: 0 });
    } catch (buildError) {
      setError(buildError.message || String(buildError));
    } finally {
      setIsSubmitting(false);
    }
  }

  async function publish() {
    setError('');
    setIsPublishing(true);
    try {
      const result = await request(`/v1/projects/${encodeURIComponent(project.projectId)}/youtube/upload`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          filename: outputName,
          title: youtubeTitle,
          description: youtubeDescription,
          tags: ['Bible', 'Scripture', passage.split(/\s+/)[0]],
          privacyStatus: youtubePrivacy,
        }),
      });
      setYoutubeResult(result.url);
      setPublishReview(false);
    } catch (publishError) {
      setError(publishError.message || String(publishError));
    } finally {
      setIsPublishing(false);
    }
  }

  return (
    <main className="bible-studio" data-theme={theme}>
      <header className="bible-topbar">
        <button type="button" className="icon-action" onClick={onBack} aria-label="Back to Render Desk">←</button>
        <div><h1>Bible Video Studio</h1><p>Sextant-orchestrated scripture production</p></div>
        <div className="bible-top-actions"><button type="button" className="secondary-action" onClick={onOpenProjects}>Projects</button><span>{job ? `${stageLabel(job.stage)} · ${progress}%` : 'Ready'}</span></div>
      </header>

      <form className="bible-workspace" onSubmit={buildVideo}>
        <aside className="bible-config">
          <section><h2>Scripture source</h2><label>Passage<input value={passage} onChange={(event) => setPassage(event.target.value)} required /></label></section>
          <section><h2>Video mode</h2><div className="mode-switch"><button type="button" className={mode === 'still' ? 'active' : ''} onClick={() => setMode('still')}>Still</button><button type="button" className={mode === 'motion' ? 'active' : ''} onClick={() => setMode('motion')}>Motion</button></div></section>
          <section><label>Translation<select value={translation} onChange={(event) => setTranslation(event.target.value)}><option value="kjv">KJV</option><option value="web">World English Bible</option></select></label><div className="style-picker"><label>Find a visual style<input type="search" value={styleQuery} onChange={(event) => setStyleQuery(event.target.value)} placeholder="Search all styles" /></label><label>Visual style<select value={visualStyle} onChange={(event) => setVisualStyle(event.target.value)}>{Object.entries(styleGroups).map(([category, styles]) => <optgroup label={category} key={category}>{styles.map((style) => <option value={style.id} key={style.id}>{style.name}</option>)}</optgroup>)}</select></label><p><strong>{visualStyles.length} styles</strong> available · {selectedStyle?.prompt}</p></div><label>Narrator voice<select value={voice} onChange={(event) => setVoice(event.target.value)}><option value="Carter">Carter · local</option><option value="en-US-AdamMultilingualNeural">Adam · warm</option><option value="en-US-AvaMultilingualNeural">Ava · clear</option></select></label></section>
          <button className="primary-action bible-build" type="submit" disabled={isSubmitting || (job && !['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(job.status))}>{isSubmitting ? 'Queuing' : `Generate ${mode === 'motion' ? 'Motion' : 'Still'} Video`}</button>
          {error && <div className="error-box">{error}</div>}
        </aside>

        <section className="bible-storyboard">
          <div className="bible-section-heading"><div><h2>Storyboard</h2><p>{scenes.length ? `${scenes.length} scenes from ${project?.state?.passage || passage}` : 'Scenes appear here as the job completes.'}</p></div><strong>{mode.toUpperCase()}</strong></div>
          {scenes.length ? <div className="bible-scene-list">{scenes.map((scene, index) => {
            const image = scene.images?.[0];
            const imageUrl = apiUrl(effectiveApiBase, `/v1/projects/${encodeURIComponent(project.projectId)}/assets/images/${encodeURIComponent(image)}`);
            return <article className="bible-scene" key={`${scene.title}-${index}`}><span className="scene-number">{index + 1}</span><img src={imageUrl} alt="" /><div><h3>{scene.title}</h3><p>{scene.VO}</p><small>{Math.round(scene.duration || 0)} sec · {project.state?.mode === 'motion' ? 'Motion clip' : 'Still image'}</small></div></article>;
          })}</div> : <div className="storyboard-empty"><div className="empty-frame">16:9</div><h3>Name a passage. Sextant handles the rest.</h3><p>The job will retrieve the verses, create the storyboard, generate imagery, add motion when selected, narrate, and render one MP4.</p></div>}
        </section>

        <aside className="bible-output">
          <section><div className="bible-section-heading"><div><h2>Video preview</h2><p>16:9 · 1080p MP4</p></div></div>{videoHref ? <video controls src={videoHref} /> : <div className="video-placeholder"><span>{progress}%</span><p>{job ? stageLabel(job.stage) : 'Waiting for a job'}</p></div>}</section>
          <section className="progress-card"><h2>Render progress</h2><progress value={job?.progress || 0} max="1" /><div><span>{stageLabel(job?.stage)}</span><strong>{progress}%</strong></div></section>
          <section className="health-card"><h2>Worker health</h2>{healthRows.map(([label, value]) => <div className="health-row" key={label}><span>{label}</span><strong className={value?.ok ? 'healthy' : 'offline'}>{value?.ok ? 'Healthy' : 'Unavailable'}</strong></div>)}</section>
          <section className="publish-card"><h2>YouTube publishing</h2><label>Title<input value={youtubeTitle} onChange={(event) => setYoutubeTitle(event.target.value)} disabled={!project} /></label><label>Privacy<select value={youtubePrivacy} onChange={(event) => setYoutubePrivacy(event.target.value)} disabled={!project}><option value="private">Private</option><option value="unlisted">Unlisted</option><option value="public">Public</option></select></label><button type="button" className="primary-action" disabled={!project || isPublishing} onClick={() => setPublishReview(true)}>Review &amp; Publish</button>{youtubeResult && <a href={youtubeResult} target="_blank" rel="noreferrer">Open published video</a>}</section>
        </aside>
      </form>

      {publishReview && <div className="publish-modal" role="dialog" aria-modal="true"><div><h2>Final publishing review</h2><p>This uploads <strong>{youtubeTitle}</strong> to YouTube as <strong>{youtubePrivacy}</strong>. Publishing is not automatic.</p><label>Description<textarea value={youtubeDescription} onChange={(event) => setYoutubeDescription(event.target.value)} /></label><div><button type="button" className="secondary-action" onClick={() => setPublishReview(false)}>Cancel</button><button type="button" className="primary-action" onClick={publish} disabled={isPublishing}>{isPublishing ? 'Publishing' : 'Confirm YouTube Publish'}</button></div></div></div>}
    </main>
  );
}
