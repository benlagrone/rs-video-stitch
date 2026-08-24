import { useEffect, useMemo, useState } from 'react';
import { YouTubeChannelSelector } from './YouTubeChannelSelector.jsx';

const FALLBACK_STYLES = [
  { id: 'cinematic-natural-light', name: 'Cinematic natural light', category: 'Sacred & historical', prompt: 'Cinematic natural light and grounded historical realism' },
  { id: 'classical-oil-painting', name: 'Classical oil painting', category: 'Sacred & historical', prompt: 'Layered pigments and museum-quality composition' },
  { id: 'fortress-grid-illustration', name: 'Fortress Grid illustration', category: 'General art & illustration', prompt: 'Clean geometric forms and a restrained palette' },
  { id: 'historical-documentary', name: 'Historical documentary', category: 'Sacred & historical', prompt: 'Authentic material culture and natural available light' },
];
const FALLBACK_FONTS = [
  { id: 'EB Garamond', name: 'EB Garamond', family: 'EB Garamond' },
  { id: 'Cinzel', name: 'Cinzel', family: 'Cinzel' },
];
const FALLBACK_CHARACTER_POLICY = {
  god: { locked: true, summary: 'Masculine, mature-to-elderly, never feminine, never young' },
};

function apiUrl(baseUrl, path) {
  if (/^https?:\/\//i.test(path)) return path;
  return `${baseUrl.replace(/\/+$/, '')}${path}`;
}

function stageLabel(stage) {
  return String(stage || 'QUEUED').replaceAll('_', ' ').toLowerCase().replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function MotionPlanEditor({ plan, busy, onPlan, onSave, onRegionChange }) {
  if (!plan) {
    return <div className="motion-plan-empty"><strong>Controlled motion regions</strong><span>Plan separate actions for the fire, terrain, atmosphere, or other existing parts of this still.</span><button type="button" className="secondary-action" onClick={onPlan} disabled={busy}>{busy ? 'Planning regions…' : 'Plan motion regions'}</button></div>;
  }
  return <section className="motion-plan-editor"><div className="motion-plan-heading"><div><strong>Controlled motion regions</strong><span>{plan.summary}</span><small>Grounded in the source-image prompt and scripture. The unselected background stays fixed.</small></div><div><button type="button" className="secondary-action" onClick={onSave} disabled={busy}>Save plan</button><button type="button" className="secondary-action" onClick={onPlan} disabled={busy}>{busy ? 'Replanning…' : 'Replan'}</button></div></div><div className="motion-region-list">{(plan.regions || []).map((region, index) => <div className="motion-region" key={region.id || index}><label className="motion-region-toggle"><input type="checkbox" checked={region.enabled !== false} onChange={(event) => onRegionChange(index, { enabled: event.target.checked })} />{region.label}</label><input aria-label={`${region.label} action`} value={region.action || ''} onChange={(event) => onRegionChange(index, { action: event.target.value })} /><select aria-label={`${region.label} direction`} value={region.direction || 'right'} onChange={(event) => onRegionChange(index, { direction: event.target.value })}><option value="left">Left</option><option value="right">Right</option><option value="up">Up</option><option value="down">Down</option><option value="outward">Outward</option><option value="clockwise">Clockwise</option><option value="counterclockwise">Counterclockwise</option><option value="pulse">Pulse</option></select><label>Strength<input type="range" min="0.1" max="1" step="0.05" value={region.strength || 0.5} onChange={(event) => onRegionChange(index, { strength: Number(event.target.value) })} /></label></div>)}</div></section>;
}

export function BibleStudio({ authToken, theme, initialProjectId = '', onBack, onOpenProjects }) {
  const effectiveApiBase = window.location.port === '3000'
    ? `${window.location.protocol}//${window.location.hostname}:8082`
    : '';
  const [passage, setPassage] = useState('Genesis 3:1-6');
  const [themeInterpretation, setThemeInterpretation] = useState('');
  const [translation, setTranslation] = useState('kjv');
  const [mode, setMode] = useState('motion');
  const [visualStyle, setVisualStyle] = useState(FALLBACK_STYLES[0].id);
  const [visualStyles, setVisualStyles] = useState(FALLBACK_STYLES);
  const [styleQuery, setStyleQuery] = useState('');
  const [voice, setVoice] = useState('Carter');
  const [ttsApi, setTtsApi] = useState('vibevoice-proxy');
  const [voiceProviders, setVoiceProviders] = useState([]);
  const [captionFont, setCaptionFont] = useState('EB Garamond');
  const [captionFonts, setCaptionFonts] = useState(FALLBACK_FONTS);
  const [characterPolicy, setCharacterPolicy] = useState(FALLBACK_CHARACTER_POLICY);
  const [job, setJob] = useState(null);
  const [project, setProject] = useState(null);
  const [health, setHealth] = useState({});
  const [error, setError] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isSavingProject, setIsSavingProject] = useState(false);
  const [settingsNotice, setSettingsNotice] = useState('');
  const [publishReview, setPublishReview] = useState(false);
  const [isPublishing, setIsPublishing] = useState(false);
  const [youtubeResult, setYoutubeResult] = useState('');
  const [youtubeTitle, setYoutubeTitle] = useState('');
  const [youtubeDescription, setYoutubeDescription] = useState('');
  const [youtubePrivacy, setYoutubePrivacy] = useState('private');
  const [youtubeChannels, setYoutubeChannels] = useState([]);
  const [youtubeProfile, setYoutubeProfile] = useState('animals');
  const [isConnectingYoutube, setIsConnectingYoutube] = useState(false);
  const [animationPrompts, setAnimationPrompts] = useState({});
  const [motionPlans, setMotionPlans] = useState({});
  const [planningMotionFor, setPlanningMotionFor] = useState(0);
  const [cameraBehaviors, setCameraBehaviors] = useState({});
  const [animationJobs, setAnimationJobs] = useState({});
  const [writingPromptFor, setWritingPromptFor] = useState(0);
  const [isQueuingAllAnimations, setIsQueuingAllAnimations] = useState(false);
  const [stillRegenerationJob, setStillRegenerationJob] = useState(null);
  const [titleCardJob, setTitleCardJob] = useState(null);

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

  async function loadBibleProject(projectId) {
    const loaded = await request(`/v1/projects/${encodeURIComponent(projectId)}`);
    const state = loaded.state || {};
    const loadedScenes = loaded.scenes?.scenes || [];
    setProject(loaded);
    setPassage(state.passage || state.title || passage);
    setThemeInterpretation(state.themeInterpretation ?? loaded.scenes?.info?.themeInterpretation ?? '');
    setTranslation(state.translation || 'kjv');
    setMode(state.mode || 'still');
    setVisualStyle(state.visualStyle || FALLBACK_STYLES[0].id);
    setVoice(state.voice || state.renderOptions?.tts || 'Carter');
    setTtsApi(state.ttsApi || state.renderOptions?.ttsApi || 'vibevoice-proxy');
    setCaptionFont(state.renderOptions?.titleStyle?.fontFamily || 'EB Garamond');
    setYoutubeTitle(state.youtubeTitle || state.title || state.passage || projectId);
    setYoutubeDescription(state.youtubeDescription || `A narrated visual presentation of ${state.passage || state.title || projectId}.`);
    const savedYoutubeProfile = state.youtubeProfile || state.youtubeUpload?.profile || 'animals';
    setYoutubeProfile(savedYoutubeProfile === 'bible' ? 'animals' : savedYoutubeProfile);
    setAnimationPrompts(Object.fromEntries(loadedScenes.map((scene, index) => [index + 1, scene.motionPrompt || ''])));
    setMotionPlans(Object.fromEntries(loadedScenes.map((scene, index) => [index + 1, scene.motionPlan]).filter(([, plan]) => plan)));
    setCameraBehaviors(Object.fromEntries(loadedScenes.map((scene, index) => [
      index + 1,
      scene.animationQuality?.cameraBehavior || scene.timeline?.[0]?.motionGeneration?.cameraBehavior || 'locked',
    ])));
    return loaded;
  }

  useEffect(() => {
    if (!stillRegenerationJob?.jobId || ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(stillRegenerationJob.status)) return undefined;
    const timer = window.setInterval(async () => {
      try {
        const current = await request(`/v1/jobs/${encodeURIComponent(stillRegenerationJob.jobId)}`);
        setStillRegenerationJob((previous) => ({ ...previous, ...current }));
        if (current.status === 'SUCCEEDED' && project?.projectId) await loadBibleProject(project.projectId);
      } catch (pollError) {
        setError(pollError.message || String(pollError));
      }
    }, 1500);
    return () => window.clearInterval(timer);
  }, [stillRegenerationJob?.jobId, stillRegenerationJob?.status, project?.projectId, effectiveApiBase, authToken]);

  useEffect(() => {
    request('/v1/youtube/channels').then((result) => setYoutubeChannels(result.channels || [])).catch(() => setYoutubeChannels([]));
  }, [effectiveApiBase, authToken]);

  useEffect(() => {
    request('/v1/bible/health').then(setHealth).catch(() => setHealth({ mediastudio: { ok: false } }));
  }, [effectiveApiBase, authToken]);

  useEffect(() => {
    request('/v1/bible/styles').then((result) => {
      if (result.styles?.length) setVisualStyles(result.styles);
    }).catch(() => {});
  }, [effectiveApiBase, authToken]);

  useEffect(() => {
    request('/v1/bible/fonts').then((result) => {
      if (result.fonts?.length) setCaptionFonts(result.fonts);
    }).catch(() => {});
  }, [effectiveApiBase, authToken]);

  useEffect(() => {
    request('/v1/bible/character-policy').then((result) => {
      if (result.god) setCharacterPolicy(result);
    }).catch(() => {});
  }, [effectiveApiBase, authToken]);

  useEffect(() => {
    request('/v1/voice-options').then((result) => {
      const providers = (result.providers || []).filter((provider) => provider.selectable && provider.voices?.length);
      setVoiceProviders(providers);
      const selectedProvider = providers.find((provider) => (
        provider.ttsApi === ttsApi && provider.voices.includes(voice)
      ));
      if (!selectedProvider && providers[0]) {
        setTtsApi(providers[0].ttsApi);
        setVoice(providers[0].voices[0]);
      }
    }).catch(() => setVoiceProviders([]));
  }, [effectiveApiBase, authToken]);

  useEffect(() => {
    if (!initialProjectId) return;
    setError('');
    loadBibleProject(initialProjectId).catch((loadError) => setError(loadError.message || String(loadError)));
  }, [initialProjectId, effectiveApiBase, authToken]);

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

  useEffect(() => {
    const activeJobs = Object.entries(animationJobs).filter(([, value]) => (
      value?.jobId && !['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(value.status)
    ));
    if (!activeJobs.length || !project?.projectId) return undefined;
    const timer = window.setInterval(async () => {
      for (const [sceneIndex, animationJob] of activeJobs) {
        try {
          const current = await request(`/v1/jobs/${encodeURIComponent(animationJob.jobId)}`);
          setAnimationJobs((previous) => ({ ...previous, [sceneIndex]: { ...animationJob, ...current } }));
          if (['SUCCEEDED', 'FAILED'].includes(current.status)) {
            const loaded = await loadBibleProject(project.projectId);
            const refreshedScene = loaded.scenes?.scenes?.[Number(sceneIndex) - 1];
            if (refreshedScene?.motionPrompt) {
              setAnimationPrompts((previous) => ({ ...previous, [sceneIndex]: refreshedScene.motionPrompt }));
            }
          }
        } catch (pollError) {
          setError(pollError.message || String(pollError));
        }
      }
    }, 1500);
    return () => window.clearInterval(timer);
  }, [animationJobs, project?.projectId, effectiveApiBase, authToken]);

  useEffect(() => {
    if (!titleCardJob?.jobId || ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(titleCardJob.status)) return undefined;
    const timer = window.setInterval(async () => {
      try {
        const current = await request(`/v1/jobs/${encodeURIComponent(titleCardJob.jobId)}`);
        setTitleCardJob((previous) => ({ ...previous, ...current }));
        if (current.status === 'SUCCEEDED' && project?.projectId) await loadBibleProject(project.projectId);
      } catch (pollError) {
        setError(pollError.message || String(pollError));
      }
    }, 1500);
    return () => window.clearInterval(timer);
  }, [titleCardJob?.jobId, titleCardJob?.status, project?.projectId, effectiveApiBase, authToken]);

  const scenes = project?.scenes?.scenes || [];
  const motionClipCount = scenes.filter((scene) => scene.timeline?.[0]?.video).length;
  const activeAnimationCount = Object.values(animationJobs).filter((animationJob) => (
    animationJob?.jobId && !['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(animationJob.status)
  )).length;
  const remainingAnimationCount = scenes.length - motionClipCount;
  const outputName = project?.state?.outputName || 'video.mp4';
  const videoHref = project ? apiUrl(effectiveApiBase, `/v1/projects/${encodeURIComponent(project.projectId)}/outputs/video?filename=${encodeURIComponent(outputName)}&v=${encodeURIComponent(project.state?.updatedAt || project.state?.titleCardUpdatedAt || '')}`) : '';
  const progress = Math.round((job?.progress || 0) * 100);
  const healthRows = [
    ['Sextant Orchestrator', health.sextant],
    ['MediaStudio', health.mediastudio],
    ['Fortress Story Planner', health.planning],
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
  const activeVoiceProvider = voiceProviders.find((provider) => (
    provider.ttsApi === ttsApi && provider.voices.includes(voice)
  ));
  const selectedYoutubeChannel = youtubeChannels.find((channel) => channel.profile === youtubeProfile) || null;
  const canPublishToYoutube = Boolean(
    selectedYoutubeChannel?.authenticated && selectedYoutubeChannel?.matchesExpectedChannel !== false,
  );

  function selectNarrator(event) {
    const [nextTtsApi, ...voiceParts] = event.target.value.split('::');
    setTtsApi(nextTtsApi);
    setVoice(voiceParts.join('::'));
  }

  async function autoWriteAnimationPrompt(sceneIndex) {
    if (!project?.projectId) return;
    setError('');
    setWritingPromptFor(sceneIndex);
    try {
      const result = await request(`/v1/projects/${encodeURIComponent(project.projectId)}/scenes/${sceneIndex}/animation-prompt`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cameraBehavior: cameraBehaviors[sceneIndex] || 'locked' }),
      });
      const nextPrompt = result.prompt || '';
      setAnimationPrompts((previous) => ({ ...previous, [sceneIndex]: nextPrompt }));
      return nextPrompt;
    } catch (promptError) {
      setError(promptError.message || String(promptError));
      return null;
    } finally {
      setWritingPromptFor(0);
    }
  }

  async function animateScene(sceneIndex, promptOverride) {
    if (!project?.projectId) return;
    setError('');
    try {
      const created = await request(`/v1/projects/${encodeURIComponent(project.projectId)}/scenes/${sceneIndex}/animate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          prompt: promptOverride ?? animationPrompts[sceneIndex] ?? '',
          cameraBehavior: cameraBehaviors[sceneIndex] || 'locked',
          motionPlan: motionPlans[sceneIndex] || null,
        }),
      });
      setAnimationJobs((previous) => ({
        ...previous,
        [sceneIndex]: { ...created, status: 'QUEUED', stage: 'QUEUED', progress: 0 },
      }));
    } catch (animationError) {
      setError(animationError.message || String(animationError));
    }
  }

  async function planSceneMotion(sceneIndex, regenerate = true) {
    if (!project?.projectId) return null;
    setError('');
    setPlanningMotionFor(sceneIndex);
    try {
      const result = await request(`/v1/projects/${encodeURIComponent(project.projectId)}/scenes/${sceneIndex}/motion-plan`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ regenerate, motionPlan: regenerate ? null : motionPlans[sceneIndex] }),
      });
      setMotionPlans((previous) => ({ ...previous, [sceneIndex]: result.motionPlan }));
      return result.motionPlan;
    } catch (planError) {
      setError(planError.message || String(planError));
      return null;
    } finally {
      setPlanningMotionFor(0);
    }
  }

  function updateMotionRegion(sceneIndex, regionIndex, patch) {
    setMotionPlans((previous) => {
      const plan = previous[sceneIndex];
      if (!plan) return previous;
      const regions = plan.regions.map((region, index) => index === regionIndex ? { ...region, ...patch } : region);
      return { ...previous, [sceneIndex]: { ...plan, regions } };
    });
  }

  async function retryRejectedScene(sceneIndex) {
    const currentPrompt = (animationPrompts[sceneIndex] || '').trim();
    if (currentPrompt) {
      await animateScene(sceneIndex, currentPrompt);
      return;
    }
    const generatedPrompt = await autoWriteAnimationPrompt(sceneIndex);
    if (generatedPrompt !== null) await animateScene(sceneIndex, generatedPrompt);
  }

  async function animateAllScenes() {
    if (!project?.projectId || !scenes.length) return;
    setError('');
    setIsQueuingAllAnimations(true);
    try {
      const created = await request(`/v1/projects/${encodeURIComponent(project.projectId)}/scenes/animate-all`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompts: animationPrompts, cameraBehaviors, motionPlans, includeAnimated: false }),
      });
      setAnimationJobs((previous) => ({
        ...previous,
        ...Object.fromEntries((created.jobs || []).map((animationJob) => [
          animationJob.sceneIndex,
          { ...animationJob, stage: 'QUEUED', progress: 0 },
        ])),
      }));
    } catch (animationError) {
      setError(animationError.message || String(animationError));
    } finally {
      setIsQueuingAllAnimations(false);
    }
  }

  async function regenerateStills(sceneIndex = 0) {
    if (!project?.projectId) return;
    setError('');
    try {
      const path = sceneIndex
        ? `/v1/projects/${encodeURIComponent(project.projectId)}/scenes/${sceneIndex}/regenerate-still`
        : `/v1/projects/${encodeURIComponent(project.projectId)}/scenes/regenerate-stills`;
      const created = await request(path, { method: 'POST' });
      setStillRegenerationJob({ ...created, status: 'QUEUED', stage: 'QUEUED', progress: 0 });
    } catch (regenerationError) {
      setError(regenerationError.message || String(regenerationError));
    }
  }

  async function regenerateTitleCard() {
    if (!project?.projectId) return;
    setError('');
    try {
      const created = await request(`/v1/projects/${encodeURIComponent(project.projectId)}/bible-title-card`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ visualStyle, regenerateImage: true, renderVideo: false }),
      });
      setTitleCardJob({ ...created, status: 'QUEUED', stage: 'QUEUED', progress: 0 });
    } catch (titleCardError) {
      setError(titleCardError.message || String(titleCardError));
    }
  }

  async function rebuildWithTitleCard() {
    if (!project?.projectId || !project.state?.titleCardImageName) return;
    setError('');
    try {
      const renderOptions = {
        ...(project.state?.renderOptions || {}),
        scriptureCaptionEnabled: true,
        titleStyle: { ...(project.state?.renderOptions?.titleStyle || {}), fontFamily: captionFont, fontSize: 48, fill: '#ffffff', outline: '#000000', position: 'bottom-left' },
      };
      await request(`/v1/projects/${encodeURIComponent(project.projectId)}/state`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ state: { ...project.state, renderOptions } }),
      });
      const created = await request(`/v1/projects/${encodeURIComponent(project.projectId)}/bible-title-card`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ visualStyle, regenerateImage: false, renderVideo: true }),
      });
      setTitleCardJob({ ...created, status: 'QUEUED', stage: 'QUEUED', progress: 0 });
    } catch (titleCardError) {
      setError(titleCardError.message || String(titleCardError));
    }
  }

  async function saveProjectSettings() {
    if (!project?.projectId) return;
    setError('');
    setSettingsNotice('');
    setIsSavingProject(true);
    try {
      const renderOptions = {
        ...(project.state?.renderOptions || {}),
        tts: voice,
        ttsLanguage: 'en-US',
        ttsApi,
        scriptureCaptionEnabled: true,
        titleStyle: {
          ...(project.state?.renderOptions?.titleStyle || {}),
          fontFamily: captionFont,
          fontSize: 48,
          fill: '#ffffff',
          outline: '#000000',
          position: 'bottom-left',
        },
      };
      await request(`/v1/projects/${encodeURIComponent(project.projectId)}/state`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          state: {
            ...project.state,
            passage,
            themeInterpretation,
            translation,
            mode,
            visualStyle,
            voice,
            ttsApi,
            renderOptions,
          },
        }),
      });
      await loadBibleProject(project.projectId);
      setSettingsNotice('Project settings saved');
    } catch (saveError) {
      setError(saveError.message || String(saveError));
    } finally {
      setIsSavingProject(false);
    }
  }

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
          passage, themeInterpretation, translation, mode, visualStyle, voice,
          language: 'en-US', ttsApi, outputName: 'video.mp4',
          renderOptions: { tts: voice, ttsLanguage: 'en-US', ttsApi, introEnabled: true, introTitle: passage, introBackgroundImage: 'bible-title-card.png', introLeaderEnabled: false, logoEnabled: true, logoImage: 'animal-safari-kids.png', logoCorner: 'bottom-right', logoMargin: 28, scriptureCaptionEnabled: true, titleStyle: { fontFamily: captionFont, fontSize: 48, fill: '#ffffff', outline: '#000000', position: 'bottom-left' } },
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
          profile: youtubeProfile,
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

  async function connectYoutube() {
    setError('');
    setIsConnectingYoutube(true);
    try {
      const result = await request(`/v1/youtube/auth/start?profile=${encodeURIComponent(youtubeProfile)}`, { method: 'POST' });
      window.open(result.authUrl, '_blank', 'noopener,noreferrer');
      if (!result.manualCallback) {
        for (let attempt = 0; attempt < 60; attempt += 1) {
          await new Promise((resolve) => window.setTimeout(resolve, 2000));
          const status = await request(`/v1/youtube/auth/status?profile=${encodeURIComponent(youtubeProfile)}`);
          if (status.authenticated) {
            const catalog = await request('/v1/youtube/channels?force=true');
            setYoutubeChannels(catalog.channels || []);
            return;
          }
        }
      }
    } catch (connectError) {
      setError(connectError.message || String(connectError));
    } finally {
      setIsConnectingYoutube(false);
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
          <section><h2>Scripture source</h2><label>Passage<input value={passage} onChange={(event) => { setPassage(event.target.value); setSettingsNotice(''); }} required /></label><label>Theme / interpretation<textarea value={themeInterpretation} onChange={(event) => { setThemeInterpretation(event.target.value); setSettingsNotice(''); }} maxLength={2000} placeholder="Optional — describe the video’s theme, interpretation, symbolism, emotional arc, setting, or portrayal details" /></label><span className="caption-font-note">This direction is mixed with the selected art style and applied to title art, scenes, motion, and permitted portrayal details. Leave blank to follow the scripture text closely without an added interpretation.</span>{project && <><button type="button" className="secondary-action" onClick={saveProjectSettings} disabled={isSavingProject}>{isSavingProject ? 'Saving project settings…' : 'Save project settings'}</button>{settingsNotice && <span className="project-save-notice" role="status">{settingsNotice}</span>}</>}</section>
          <section><h2>Video mode</h2><div className="mode-switch"><button type="button" className={mode === 'still' ? 'active' : ''} onClick={() => setMode('still')}>Still</button><button type="button" className={mode === 'motion' ? 'active' : ''} onClick={() => setMode('motion')}>Motion</button></div></section>
          <section><label>Translation<select value={translation} onChange={(event) => setTranslation(event.target.value)}><option value="kjv">KJV</option><option value="web">World English Bible</option></select></label><div className="style-picker"><label>Find a visual style<input type="search" value={styleQuery} onChange={(event) => setStyleQuery(event.target.value)} placeholder="Search all styles" /></label><label>Visual style<select value={visualStyle} onChange={(event) => setVisualStyle(event.target.value)}>{Object.entries(styleGroups).map(([category, styles]) => <optgroup label={category} key={category}>{styles.map((style) => <option value={style.id} key={style.id}>{style.name}</option>)}</optgroup>)}</select></label><p><strong>{visualStyles.length} styles</strong> available · {selectedStyle?.prompt}</p></div><div className="character-policy"><strong>Locked God portrayal</strong><span>{characterPolicy.god?.summary}</span><small>Applied to title cards, still images, motion planning, animation prompts, and negative prompts.</small></div><label>Scripture caption font<select value={captionFont} onChange={(event) => setCaptionFont(event.target.value)}>{captionFonts.map((font) => <option value={font.id} key={font.id}>{font.name}</option>)}</select></label><span className="caption-font-note">The verse reference and full text print along the bottom of every scene.</span>{project && <div className="bible-title-card-control">{project.state?.titleCardImageName && <img src={`${apiUrl(effectiveApiBase, `/v1/projects/${encodeURIComponent(project.projectId)}/assets/leader/${encodeURIComponent(project.state.titleCardImageName)}`)}?v=${encodeURIComponent(project.state?.titleCardUpdatedAt || '')}`} alt={`${project.state?.passage || passage} title card background`} />}<button type="button" className="secondary-action" onClick={regenerateTitleCard} disabled={titleCardJob && !['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(titleCardJob.status)}>{titleCardJob && !['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(titleCardJob.status) ? `${stageLabel(titleCardJob.stage)} · ${Math.round((titleCardJob.progress || 0) * 100)}%` : (project.state?.titleCardImageName ? 'Regenerate title card preview' : 'Generate title card preview')}</button>{project.state?.titleCardImageName && <button type="button" className="secondary-action" onClick={rebuildWithTitleCard} disabled={titleCardJob && !['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(titleCardJob.status)}>Save font &amp; rebuild video</button>}<small>Review the passage-specific art first. Rebuilding reuses the approved image with the selected channel mark—never the brokerage contact template.</small></div>}<label>Narrator voice<select value={`${ttsApi}::${voice}`} onChange={selectNarrator}>{voiceProviders.length ? voiceProviders.map((provider) => <optgroup label={provider.label} key={provider.id}>{provider.voices.map((voiceName) => <option value={`${provider.ttsApi}::${voiceName}`} key={`${provider.id}-${voiceName}`}>{voiceName}</option>)}</optgroup>) : <option value="vibevoice-proxy::Carter">Carter</option>}</select></label>{activeVoiceProvider && <span className="voice-source-note">Source: {activeVoiceProvider.label}</span>}</section>
          <button className="primary-action bible-build" type="submit" disabled={isSubmitting || (job && !['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(job.status))}>{isSubmitting ? 'Queuing' : `Generate ${mode === 'motion' ? 'Motion' : 'Still'} Video`}</button>
          {error && <div className="error-box">{error}</div>}
        </aside>

        <section className="bible-storyboard">
          <div className="bible-section-heading"><div><h2>Storyboard</h2><p>{scenes.length ? `${scenes.length} scenes from ${project?.state?.passage || passage}` : 'Scenes appear here as the job completes.'}</p></div><div className="storyboard-actions"><strong>{motionClipCount ? `${motionClipCount} MOTION · ${scenes.length - motionClipCount} STILL` : (mode === 'motion' ? 'MOTION · CONTINUITY PLANNED' : 'STILL')}</strong>{scenes.length > 0 && <button type="button" className="secondary-action" onClick={() => regenerateStills()} disabled={stillRegenerationJob && !['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(stillRegenerationJob.status)}>{stillRegenerationJob && !['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(stillRegenerationJob.status) ? `${stageLabel(stillRegenerationJob.stage)} · ${Math.round((stillRegenerationJob.progress || 0) * 100)}%` : 'Regenerate all stills'}</button>}{scenes.length > 0 && <button type="button" className="primary-action animate-all-action" onClick={animateAllScenes} disabled={isQueuingAllAnimations || activeAnimationCount > 0 || remainingAnimationCount === 0 || (stillRegenerationJob && !['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(stillRegenerationJob.status))}>{isQueuingAllAnimations ? 'Queuing scenes…' : activeAnimationCount > 0 ? `Animating ${activeAnimationCount} scene${activeAnimationCount === 1 ? '' : 's'}…` : remainingAnimationCount === 0 ? 'All scenes animated' : 'Animate all scenes'}</button>}</div></div>
          {scenes.length ? <div className="bible-scene-list">{scenes.map((scene, index) => {
            const sceneIndex = index + 1;
            const image = scene.images?.[0];
            const imageUrl = `${apiUrl(effectiveApiBase, `/v1/projects/${encodeURIComponent(project.projectId)}/assets/images/${encodeURIComponent(image)}`)}?v=${encodeURIComponent(scene.imageUpdatedAt || project.state?.updatedAt || '')}`;
            const clip = scene.timeline?.[0]?.video;
            const motionUrl = clip ? apiUrl(effectiveApiBase, `/v1/projects/${encodeURIComponent(project.projectId)}/assets/motion/${encodeURIComponent(clip)}`) : '';
            const animationJob = animationJobs[sceneIndex];
            const animationBusy = animationJob && !['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(animationJob.status);
            const stillBusy = stillRegenerationJob && !['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(stillRegenerationJob.status) && (stillRegenerationJob.sceneIndexes || []).includes(sceneIndex);
            const animationRejected = animationJob?.status === 'FAILED' || scene.animationQuality?.status === 'rejected';
            const rejectionReason = animationJob?.error || scene.animationQuality?.reason || animationJob?.stage;
            const motionPlan = motionPlans[sceneIndex] || scene.motionPlan;
            const motionPlanningBusy = planningMotionFor === sceneIndex;
            const legacyRegionControl = String(scene.animationQuality?.modelProvider || '').startsWith('wan2.1-vace-region-control') && scene.animationQuality?.controlMode !== 'masked-generative-inpaint';
            const generativeMotion = ['ltx-video-local-2b', 'stable-video-diffusion', 'wan2.2-ti2v-5b', 'wan2.1-vace-region-control'].includes(scene.animationQuality?.modelProvider);
            return <article className="bible-scene" key={`${scene.title}-${index}`}><span className="scene-number">{sceneIndex}</span><div className="scene-media">{motionUrl ? <video controls preload="metadata" poster={imageUrl} src={motionUrl} /> : <img src={imageUrl} alt="" />}<span>{legacyRegionControl ? 'Legacy crop-control clip' : motionUrl ? 'Motion clip' : 'Still image'}</span></div><div><h3>{scene.title}</h3><p>{scene.VO}</p>{scene.action && <dl className="motion-beat"><div><dt>Action</dt><dd>{scene.action}</dd></div><div><dt>Camera</dt><dd>{scene.camera}</dd></div><div><dt>Continuity</dt><dd>{scene.continuity}</dd></div><div><dt>Ends with</dt><dd>{scene.endState}</dd></div></dl>}<div className="scene-animation-tools"><label>Animation prompt<textarea value={animationPrompts[sceneIndex] || ''} onChange={(event) => setAnimationPrompts((previous) => ({ ...previous, [sceneIndex]: event.target.value }))} placeholder="Optional — leave blank and Fortress will write a continuity-safe prompt" /></label><label>Camera behavior<select value={cameraBehaviors[sceneIndex] || 'locked'} onChange={(event) => setCameraBehaviors((previous) => ({ ...previous, [sceneIndex]: event.target.value }))}><option value="locked">Locked composition (recommended)</option><option value="slow-push">Smooth slow push</option><option value="pan-left">Smooth pan left</option><option value="pan-right">Smooth pan right</option></select></label><small>Locked composition keeps the original frame edges, crop, scale, and horizon fixed. Motion comes from the scene rather than camera shake.</small><MotionPlanEditor plan={motionPlan} busy={motionPlanningBusy || animationBusy || stillBusy} onPlan={() => planSceneMotion(sceneIndex, true)} onSave={() => planSceneMotion(sceneIndex, false)} onRegionChange={(regionIndex, patch) => updateMotionRegion(sceneIndex, regionIndex, patch)} /><div><button type="button" className="secondary-action" onClick={() => regenerateStills(sceneIndex)} disabled={stillBusy || animationBusy}>{stillBusy ? 'Regenerating still…' : 'Regenerate still'}</button><button type="button" className="secondary-action" onClick={() => autoWriteAnimationPrompt(sceneIndex)} disabled={writingPromptFor === sceneIndex || animationBusy || stillBusy}>{writingPromptFor === sceneIndex ? 'Writing prompt…' : 'Auto-write prompt'}</button><button type="button" className="primary-action" onClick={() => animationRejected ? retryRejectedScene(sceneIndex) : animateScene(sceneIndex)} disabled={animationBusy || stillBusy || writingPromptFor === sceneIndex}>{animationBusy ? `${stageLabel(animationJob.stage)} · ${Math.round((animationJob.progress || 0) * 100)}%` : writingPromptFor === sceneIndex ? 'Writing prompt…' : animationRejected ? 'Retry animation' : (clip ? 'Re-animate image' : 'Animate image')}</button></div>{animationRejected && <small className="scene-animation-error">Animation rejected: {rejectionReason}. The original still and any previously accepted clip were preserved. Edit the prompt and retry it directly, or clear the prompt to have Fortress write a new one.</small>}{legacyRegionControl && <small className="scene-animation-error">This clip used the retired moving-crop controller. Re-animate it to create model-generated scene motion.</small>}{scene.animationQuality?.status === 'accepted' && !legacyRegionControl && <small className="scene-animation-quality">{generativeMotion ? 'Generative motion accepted' : 'Quality accepted'} · {scene.animationQuality.cameraBehavior === 'locked' ? 'locked framing stabilized' : 'deliberate camera move'} · source fit without cropping</small>}</div><small>{Math.round(scene.duration || 0)} sec · {clip ? 'Motion clip attached; regenerating the still will detach it' : 'Still image ready to animate'}</small></div></article>;
          })}</div> : <div className="storyboard-empty"><div className="empty-frame">16:9</div><h3>Name a passage. Sextant handles the rest.</h3><p>Motion mode plans the whole passage as one continuous sequence, gives every scene a visible action, and carries each scene's final frame into the next shot.</p></div>}
        </section>

        <aside className="bible-output">
          <section><div className="bible-section-heading"><div><h2>Video preview</h2><p>16:9 · 1080p MP4</p></div></div>{videoHref ? <video controls src={videoHref} /> : <div className="video-placeholder"><span>{progress}%</span><p>{job ? stageLabel(job.stage) : 'Waiting for a job'}</p></div>}</section>
          <section className="progress-card"><h2>Render progress</h2><progress value={job?.progress || 0} max="1" /><div><span>{stageLabel(job?.stage)}</span><strong>{progress}%</strong></div></section>
          <section className="health-card"><h2>Worker health</h2>{healthRows.map(([label, value]) => <div className="health-row" key={label}><span>{label}</span><strong className={value?.ok ? 'healthy' : 'offline'}>{value?.ok ? 'Healthy' : 'Unavailable'}</strong></div>)}</section>
          <section className="publish-card"><h2>YouTube publishing</h2><YouTubeChannelSelector channels={youtubeChannels} value={youtubeProfile} onChange={setYoutubeProfile} disabled={isPublishing || isConnectingYoutube} />{selectedYoutubeChannel && !selectedYoutubeChannel.authenticated && <button type="button" className="secondary-action" onClick={connectYoutube} disabled={isConnectingYoutube}>{isConnectingYoutube ? 'Connecting…' : `Connect ${selectedYoutubeChannel.channelName}`}</button>}{selectedYoutubeChannel?.matchesExpectedChannel === false && <p className="youtube-channel-warning">This profile is connected to the wrong YouTube account. Reconnect it before publishing.</p>}<small className="youtube-branding-note">Destination selection does not replace the branding already rendered into the video.</small><label>Title<input value={youtubeTitle} onChange={(event) => setYoutubeTitle(event.target.value)} disabled={!project} /></label><label>Privacy<select value={youtubePrivacy} onChange={(event) => setYoutubePrivacy(event.target.value)} disabled={!project}><option value="private">Private</option><option value="unlisted">Unlisted</option><option value="public">Public</option></select></label><button type="button" className="primary-action" disabled={!project || isPublishing || !canPublishToYoutube} onClick={() => setPublishReview(true)}>Review &amp; Publish</button>{youtubeResult && <a href={youtubeResult} target="_blank" rel="noreferrer">Open published video</a>}</section>
        </aside>
      </form>

      {publishReview && <div className="publish-modal" role="dialog" aria-modal="true"><div><h2>Final publishing review</h2>{selectedYoutubeChannel && <div className="channel-brand"><img src={selectedYoutubeChannel.iconUrl || selectedYoutubeChannel.fallbackIconUrl} alt={`${selectedYoutubeChannel.channelName} channel icon`} /><div><strong>{selectedYoutubeChannel.channelName}</strong><span>{selectedYoutubeChannel.handle || 'YouTube channel'}</span></div></div>}<p>This uploads <strong>{youtubeTitle}</strong> to <strong>{selectedYoutubeChannel?.channelName}</strong> as <strong>{youtubePrivacy}</strong>. Publishing is not automatic.</p><label>Description<textarea value={youtubeDescription} onChange={(event) => setYoutubeDescription(event.target.value)} /></label><div><button type="button" className="secondary-action" onClick={() => setPublishReview(false)}>Cancel</button><button type="button" className="primary-action" onClick={publish} disabled={isPublishing || !canPublishToYoutube}>{isPublishing ? 'Publishing' : 'Confirm YouTube Publish'}</button></div></div></div>}
    </main>
  );
}
