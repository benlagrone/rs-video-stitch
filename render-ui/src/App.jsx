import { Fragment, useEffect, useMemo, useState } from 'react';
import { BibleStudio } from './BibleStudio.jsx';
import {
  leadLinesFromState,
  renderOptionsFromProject,
  sceneAssignmentsFromRoomInfo,
  sanitizeSceneImageAssignments,
  sceneImageAssignmentsFromProject,
  titleCardFromScript,
  titleFromScript,
} from './projectState.js';
import {
  EMPTY_RENDER_TRACKER,
  renderStageLabel,
  renderStepStates,
  renderTrackerProgress,
} from './renderProgress.js';
import { YouTubeChannelSelector } from './YouTubeChannelSelector.jsx';
import { VoiceSamples } from './VoiceSamples.jsx';

const DEFAULT_RENDER_OPTIONS = {
  fps: 30,
  minShot: 2.5,
  maxShot: 8,
  xfade: 0.5,
  crf: 18,
  preset: 'medium',
  tts: null,
  ttsLanguage: 'en-US',
  ttsApi: 'voice-gateway',
  voiceDir: null,
  music: null,
  ducking: false,
  introEnabled: false,
  introTitle: '',
  introLeaderImage: null,
  introDuration: 1,
  thumbnailEnabled: true,
  logoEnabled: true,
  logoImage: 'logophone.png',
  logoCorner: 'top-right',
  logoMargin: 24,
};

const HYPERFRAMES_EDITOR = 'http://localhost:3002/#project/hyperframes-lecrown-preview?v=1&t=7.2&tab=design';

const HYPERFRAMES_PROJECT_ID = 'hyperframes-lecrown-preview';
const HYPERFRAMES_COMPOSITIONS = {
  'lecrown-lead-card': {
    file: 'compositions/lead-card.html',
    elementId: 'lead-headline',
    label: 'Opening title card',
  },
  'lecrown-scene-transition': {
    file: 'compositions/lecrown-scene-transition.html',
    elementId: 'transition-title',
    label: 'Between scene sections',
  },
};

function hyperframesEditUrl(compositionId, variables = {}) {
  const composition = HYPERFRAMES_COMPOSITIONS[compositionId];
  if (!composition) return HYPERFRAMES_EDITOR;
  const variableQuery = new URLSearchParams(Object.entries(variables)
    .filter(([, value]) => typeof value === 'string' && value.trim())
    .map(([key, value]) => [`hf_${key}`, value]));
  const [editorOrigin, editorRoute] = HYPERFRAMES_EDITOR.split('#');
  const editorUrl = `${editorOrigin}${variableQuery.size ? `?${variableQuery}` : ''}#${editorRoute}`;
  return `${editorUrl}&selFile=${encodeURIComponent(composition.file)}&selId=${encodeURIComponent(composition.elementId)}&selSelector=${encodeURIComponent(`#${composition.elementId}`)}`;
}

function hyperframesBinding(leadCard, sceneTransition, transitionTitles, leadCardMode = 'hyperframes', sceneTransitionMode = 'hyperframes') {
  return {
    projectId: HYPERFRAMES_PROJECT_ID,
    leadCard,
    sceneTransition,
    transitionTitles,
    leadCardMode,
    sceneTransitionMode,
  };
}

function slugify(value) {
  return value
    .toString()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64);
}

function splitScript(script) {
  return script
    .split(/\n{2,}/)
    .map((part) => part.trim())
    .filter(Boolean);
}

function sentenceChunks(text) {
  const normalized = text.replace(/\s+/g, ' ').trim();
  if (!normalized) return [];
  const sentences = normalized.match(/[^.!?。！？]+[.!?。！？]+(?:\s+|$)|[^.!?。！？]+$/g) || [normalized];
  return sentences.map((sentence) => sentence.trim()).filter(Boolean);
}

function splitScriptIntoSections(script, sectionCount) {
  const count = Math.max(1, Number(sectionCount) || 1);
  const sentences = sentenceChunks(script);
  if (!sentences.length || count === 1) return script.trim();

  if (sentences.length < count) {
    const normalized = script.replace(/\s+/g, ' ').trim();
    const words = /[\u3400-\u9fff]/.test(normalized)
      ? Array.from(normalized.replace(/\s+/g, ''))
      : normalized.split(/\s+/).filter(Boolean);
    const sections = Array.from({ length: count }, (_, index) => {
      const start = Math.floor((index * words.length) / count);
      const end = Math.floor(((index + 1) * words.length) / count);
      return words.slice(start, end).join(' ').trim();
    }).filter(Boolean);
    return sections.join('\n\n');
  }

  const sentenceWeights = sentences.map((sentence) => sentence.split(/\s+/).filter(Boolean).length);
  const totalWeight = sentenceWeights.reduce((sum, weight) => sum + weight, 0);
  const sections = [];
  let section = [];
  let sectionWeight = 0;
  let sentenceIndex = 0;

  for (let targetIndex = 0; targetIndex < count; targetIndex += 1) {
    const remainingSections = count - targetIndex;
    const remainingSentences = sentences.length - sentenceIndex;
    const targetWeight = totalWeight / count;
    section = [];
    sectionWeight = 0;

    while (sentenceIndex < sentences.length && remainingSentences - section.length > remainingSections - 1) {
      section.push(sentences[sentenceIndex]);
      sectionWeight += sentenceWeights[sentenceIndex];
      sentenceIndex += 1;
      if (sectionWeight >= targetWeight) break;
    }

    sections.push(section.join(' ').trim());
  }

  if (sentenceIndex < sentences.length) {
    sections[sections.length - 1] = `${sections[sections.length - 1]} ${sentences.slice(sentenceIndex).join(' ')}`.trim();
  }

  return sections.filter(Boolean).join('\n\n');
}

function buildScenes({ title, scriptTitle, script, images, imageHeaders, imageLabelEnabled, imageRoomInfo, sceneDurations = [], sceneImageAssignments = [], voice, language, ttsApi, hyperframes }) {
  const imageNames = images.map((image) => image.name).filter(Boolean);
  const scriptContent = titleFromScript(script, scriptTitle || title);
  const assignments = sanitizeSceneImageAssignments(sceneImageAssignments, imageNames);
  const existingParagraphs = splitScript(scriptContent.narration);
  const paragraphs = assignments.length > existingParagraphs.length
    ? splitScript(splitScriptIntoSections(scriptContent.narration, assignments.length))
    : existingParagraphs;
  const scenes = paragraphs.map((paragraph, index) => {
    const assignedImages = assignments[index] || [];
    const sceneTitle = assignedImages.map((image) => imageHeaders[image]).find((header) => header && header.trim()) || '';
    const requestedDuration = Number(sceneDurations[index]);
    return {
      title: sceneTitle,
      // Listing scene names organize the editor; render titles only when explicitly requested.
      showTitle: false,
      description: paragraph,
      VO: paragraph,
      images: assignedImages,
      ...(Number.isFinite(requestedDuration) && requestedDuration > 0 ? { duration: requestedDuration } : {}),
      timeline: assignedImages.map((image) => ({
        image,
        header: imageHeaders[image] || '',
        showLabel: Boolean(imageLabelEnabled[image]),
        roomDescription: imageRoomInfo[image]?.roomDescription || '',
        roomLabel: imageRoomInfo[image]?.label || '',
        confidence: imageRoomInfo[image]?.confidence ?? null,
      })),
    };
  });

  return {
    info: { name: title || titleCardFromScript(script, scriptTitle || title).mainTitle || 'MediaStudio upload', source: 'render-ui' },
    vid: { voice: voice || null, lang: language || 'en-US', api: ttsApi || 'azure' },
    hyperframes,
    scenes,
  };
}

function apiUrl(baseUrl, path) {
  if (/^https?:\/\//i.test(path)) return path;
  return `${baseUrl.replace(/\/+$/, '')}${path}`;
}

function formatBytes(value) {
  if (!Number.isFinite(value) || value < 0) return 'Unknown size';
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function youtubeProfileForLanguage(language) {
  return /^zh(?:-|$)/i.test(String(language || '').trim()) ? 'mandarin' : 'english';
}

function fileItem(file) {
  return { name: file.name, file, persisted: false };
}

function persistedItem(asset, baseUrl) {
  return {
    name: asset.name,
    url: apiUrl(baseUrl, asset.url),
    persisted: true,
  };
}

function stateImageItem(image, assets, subdir, baseUrl) {
  const name = typeof image === 'string' ? image : image?.name;
  if (!name) return null;
  const asset = (assets?.[subdir] || []).find((item) => item.name === name);
  if (asset) return persistedItem(asset, baseUrl);
  return { name, persisted: true, missing: true };
}

function scriptFromProject(state, scenes) {
  if (typeof state.script === 'string' && state.script.trim()) return state.script;
  return (Array.isArray(scenes) ? scenes : [])
    .map((scene) => String(scene?.VO || scene?.description || '').trim())
    .filter(Boolean)
    .join('\n\n');
}

function imageHeadersFromProject(state, scenes) {
  if (state.imageHeaders && Object.keys(state.imageHeaders).length) return state.imageHeaders;
  return (Array.isArray(scenes) ? scenes : []).reduce((headers, scene) => {
    (scene?.images || []).forEach((imageName) => {
      const timelineItem = (scene?.timeline || []).find((item) => item?.image === imageName);
      headers[imageName] = timelineItem?.header || scene?.title || '';
    });
    return headers;
  }, {});
}

function missingImageDataUrl(name) {
  const label = encodeURIComponent(name || 'missing image');
  return `data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='640' height='360' viewBox='0 0 640 360'%3E%3Crect width='640' height='360' fill='%23131820'/%3E%3Crect x='24' y='24' width='592' height='312' rx='14' fill='none' stroke='%23344655' stroke-width='4' stroke-dasharray='14 12'/%3E%3Ctext x='320' y='170' text-anchor='middle' font-family='Arial,sans-serif' font-size='26' fill='%23cbd5e1'%3EMissing image file%3C/text%3E%3Ctext x='320' y='210' text-anchor='middle' font-family='Arial,sans-serif' font-size='20' fill='%2394a3b8'%3E${label}%3C/text%3E%3C/svg%3E`;
}

function versionLabel(label) {
  const labels = {
    update: 'Saved update',
    'before-update': 'Before saved update',
    restore: 'Restored version',
    'before-restore': 'Before restore',
  };
  return labels[label] || label || 'Version';
}

function joinLeadLines(lines) {
  return lines.map((line) => line.trim()).filter(Boolean).join('\n');
}

function updateLeadLineValue(lines, index, value) {
  return lines.map((line, lineIndex) => (lineIndex === index ? value : line));
}

function replaceScriptSection(script, sectionIndex, value) {
  const sections = splitScript(script);
  sections[sectionIndex] = value;
  return sections.map((section) => section.trim()).filter(Boolean).join('\n\n');
}

function updateIndexedValue(values, index, value) {
  const next = [...values];
  next[index] = value;
  return next;
}

function projectStateFromValues(values) {
  return {
    schemaVersion: 1,
    title: values.title,
    scriptTitle: values.scriptTitle,
    propertyAddress: values.propertyAddress,
    projectId: values.resolvedProjectId,
    script: values.script,
    originalScriptText: values.originalScriptText,
    targetSeconds: Number(values.targetSeconds) || 60,
    sceneDurations: values.sceneDurations,
    sceneImageAssignments: values.sceneImageAssignments,
    outputName: values.outputName,
    images: values.images.map((image) => ({ name: image.name })),
    removedImages: values.removedImages.map((image) => ({ name: image.name })),
    imageHeaders: values.imageHeaders,
    imageLabelEnabled: values.imageLabelEnabled,
    imageRoomInfo: values.imageRoomInfo,
    useIntro: values.useIntro,
    introTitle: joinLeadLines(values.introLines),
    introLines: values.introLines,
    leaderImageName: values.leaderImage?.name || null,
    useLogo: values.useLogo,
    logoChoice: values.logoChoice,
    logoImageName: values.logoImage?.name || null,
    logoCorner: values.logoCorner,
    logoMargin: Number(values.logoMargin) || 0,
    voice: values.voice,
    language: values.language,
    ttsApi: values.ttsApi,
    hyperframes: values.hyperframes,
    renderOptions: values.renderOptions,
    youtubeTitle: values.youtubeTitle,
    youtubeDescription: values.youtubeDescription,
    youtubeTags: values.youtubeTags,
    youtubePrivacy: values.youtubePrivacy,
    youtubeProfile: values.youtubeProfile,
    youtubeUpload: values.youtubeVideoId ? {
      videoId: values.youtubeVideoId,
      url: values.youtubeResult || `https://youtu.be/${values.youtubeVideoId}`,
      profile: values.youtubeProfile,
      thumbnailApplied: values.youtubeThumbnailApplied,
      thumbnailFilename: values.youtubeThumbnailFilename || null,
      thumbnailError: values.youtubeThumbnailError || null,
    } : undefined,
  };
}

export function App() {
  const defaultApiBase = window.location.port === '3000'
    ? 'http://localhost:8082'
    : window.location.port === '8082'
      ? window.location.origin
      : `${window.location.origin}/media-studio/api`;
  const [apiBase, setApiBase] = useState(localStorage.getItem('renderUi.apiBase') || defaultApiBase);
  const [authToken, setAuthToken] = useState(localStorage.getItem('renderUi.authToken') || '');
  const [title, setTitle] = useState('Listing video');
  const [scriptTitle, setScriptTitle] = useState('');
  const [propertyAddress, setPropertyAddress] = useState('');
  const [projectId, setProjectId] = useState('');
  const [script, setScript] = useState('Welcome to this property. The opening view should establish the home and the neighborhood.\n\nStep inside to highlight the living spaces, natural light, and everyday comfort.\n\nFinish with the strongest selling points and a clear invitation to schedule a showing.');
  const [originalScriptText, setOriginalScriptText] = useState('');
  const [targetSeconds, setTargetSeconds] = useState(60);
  const [sceneDurations, setSceneDurations] = useState([]);
  const [sceneImageAssignments, setSceneImageAssignments] = useState([]);
  const [isEnhancing, setIsEnhancing] = useState(false);
  const [images, setImages] = useState([]);
  const [removedImages, setRemovedImages] = useState([]);
  const [imageHeaders, setImageHeaders] = useState({});
  const [imageLabelEnabled, setImageLabelEnabled] = useState({});
  const [imageRoomInfo, setImageRoomInfo] = useState({});
  const [imagePreviews, setImagePreviews] = useState([]);
  const [draggedImageName, setDraggedImageName] = useState('');
  const [selectedImageName, setSelectedImageName] = useState('');
  const [isClassifyingRooms, setIsClassifyingRooms] = useState(false);
  const [correctingRoomName, setCorrectingRoomName] = useState('');
  const [useIntro, setUseIntro] = useState(true);
  const [introLines, setIntroLines] = useState(['', '', '', '', '', '']);
  const [isGeneratingIntro, setIsGeneratingIntro] = useState(false);
  const [leaderImage, setLeaderImage] = useState(null);
  const [brandAssets, setBrandAssets] = useState([]);
  const [useLogo, setUseLogo] = useState(true);
  const [logoChoice, setLogoChoice] = useState('logophone.png');
  const [logoImage, setLogoImage] = useState(null);
  const [logoCorner, setLogoCorner] = useState('top-right');
  const [logoMargin, setLogoMargin] = useState(24);
  const [voice, setVoice] = useState('Carter');
  const [language, setLanguage] = useState('en-US');
  const [ttsApi, setTtsApi] = useState('voice-gateway');
  const [hyperframesLeadCard, setHyperframesLeadCard] = useState('lecrown-lead-card');
  const [hyperframesSceneTransition, setHyperframesSceneTransition] = useState('lecrown-scene-transition');
  const [hyperframesTransitionTitles, setHyperframesTransitionTitles] = useState({});
  const [leadCardMode, setLeadCardMode] = useState('hyperframes');
  const [sceneTransitionMode, setSceneTransitionMode] = useState('hyperframes');
  const [generatingTransitionIndex, setGeneratingTransitionIndex] = useState(null);
  const [renderOptions, setRenderOptions] = useState({});
  const [voiceProviders, setVoiceProviders] = useState([]);
  const [outputName, setOutputName] = useState('video.mp4');
  const [status, setStatus] = useState('Idle');
  const [theme, setTheme] = useState(localStorage.getItem('renderUi.theme') || 'dark');
  const [youtubeAuth, setYoutubeAuth] = useState(null);
  const [youtubeTitle, setYoutubeTitle] = useState('');
  const [youtubeDescription, setYoutubeDescription] = useState('');
  const [isWritingYoutubeDescription, setIsWritingYoutubeDescription] = useState(false);
  const [youtubeTags, setYoutubeTags] = useState('');
  const [youtubePrivacy, setYoutubePrivacy] = useState('private');
  const [youtubeProfile, setYoutubeProfile] = useState('english');
  const [youtubeChannels, setYoutubeChannels] = useState([]);
  const [youtubeResult, setYoutubeResult] = useState('');
  const [youtubeVideoId, setYoutubeVideoId] = useState('');
  const [youtubeThumbnailApplied, setYoutubeThumbnailApplied] = useState(false);
  const [youtubeThumbnailFilename, setYoutubeThumbnailFilename] = useState('');
  const [youtubeThumbnailError, setYoutubeThumbnailError] = useState('');
  const [youtubeCallbackUrl, setYoutubeCallbackUrl] = useState('');
  const [isConnectingYoutube, setIsConnectingYoutube] = useState(false);
  const [isUploadingYoutube, setIsUploadingYoutube] = useState(false);
  const [isUpdatingYoutubeDetails, setIsUpdatingYoutubeDetails] = useState(false);
  const [isApplyingYoutubeThumbnail, setIsApplyingYoutubeThumbnail] = useState(false);
  const [job, setJob] = useState(null);
  const [logs, setLogs] = useState('');
  const [videoHref, setVideoHref] = useState('');
  const [outputs, setOutputs] = useState([]);
  const [error, setError] = useState('');
  const [view, setView] = useState('projects');
  const [bibleProjectId, setBibleProjectId] = useState('');
  const [savedProjects, setSavedProjects] = useState([]);
  const [projectFilter, setProjectFilter] = useState('');
  const [isLoadingProjects, setIsLoadingProjects] = useState(false);
  const [listingPipelineRun, setListingPipelineRun] = useState(null);
  const [soundCatalog, setSoundCatalog] = useState({ assets: [], count: 0, status: 'loading' });
  const [isLoadingSoundCatalog, setIsLoadingSoundCatalog] = useState(false);
  const [isLoadingProject, setIsLoadingProject] = useState(false);
  const [isSavingProject, setIsSavingProject] = useState(false);
  const [lastSavedAt, setLastSavedAt] = useState('');
  const [savedProjectId, setSavedProjectId] = useState('');
  const [versions, setVersions] = useState([]);
  const [isRestoringVersion, setIsRestoringVersion] = useState(false);
  const [isRendering, setIsRendering] = useState(false);
  const [renderTracker, setRenderTracker] = useState(EMPTY_RENDER_TRACKER);

  const resolvedProjectId = useMemo(() => {
    if (projectId.trim()) return slugify(projectId.trim());
    return `p_${slugify(title || 'mediastudio-project')}`;
  }, [projectId, title]);
  const filteredProjects = useMemo(() => {
    const query = projectFilter.trim().toLocaleLowerCase();
    if (!query) return savedProjects;
    return savedProjects.filter((project) => (
      `${project.title || ''} ${project.projectId || ''}`.toLocaleLowerCase().includes(query)
    ));
  }, [projectFilter, savedProjects]);
  const newestTenProjects = useMemo(
    () => [...savedProjects]
      .sort((left, right) => Number(right.updatedAt || 0) - Number(left.updatedAt || 0))
      .slice(0, 10),
    [savedProjects],
  );
  const recognizedScriptTitle = useMemo(() => titleFromScript(script, scriptTitle || title), [script, scriptTitle, title]);
  const recognizedTitleCard = useMemo(() => titleCardFromScript(script, scriptTitle || title), [script, scriptTitle, title]);
  const scriptSections = useMemo(() => {
    const paragraphs = splitScript(recognizedScriptTitle.narration);
    const assignmentCount = sanitizeSceneImageAssignments(
      sceneImageAssignments,
      images.map((image) => image.name),
    ).length;
    return assignmentCount > paragraphs.length
      ? splitScript(splitScriptIntoSections(recognizedScriptTitle.narration, assignmentCount))
      : paragraphs;
  }, [images, recognizedScriptTitle, sceneImageAssignments]);
  const selectedHyperframes = useMemo(
    () => hyperframesBinding(
      hyperframesLeadCard,
      hyperframesSceneTransition,
      hyperframesTransitionTitles,
      leadCardMode,
      sceneTransitionMode,
    ),
    [hyperframesLeadCard, hyperframesSceneTransition, hyperframesTransitionTitles, leadCardMode, sceneTransitionMode],
  );

  const scenesPayload = useMemo(
    () => buildScenes({ title, scriptTitle, script, images, imageHeaders, imageLabelEnabled, imageRoomInfo, sceneDurations, sceneImageAssignments, voice, language, ttsApi, hyperframes: selectedHyperframes }),
    [title, scriptTitle, script, images, imageHeaders, imageLabelEnabled, imageRoomInfo, sceneDurations, sceneImageAssignments, voice, language, ttsApi, selectedHyperframes],
  );
  const unassignedImageCount = useMemo(() => {
    const assigned = new Set(scenesPayload.scenes.flatMap((scene) => scene.images || []));
    return images.filter((image) => !assigned.has(image.name)).length;
  }, [images, scenesPayload]);
  const unassignedImagePreviews = useMemo(() => {
    const assigned = new Set(scenesPayload.scenes.flatMap((scene) => scene.images || []));
    return imagePreviews.filter((preview) => !assigned.has(preview.name));
  }, [imagePreviews, scenesPayload]);
  const imagePreviewByName = useMemo(
    () => new Map(imagePreviews.map((preview) => [preview.name, preview])),
    [imagePreviews],
  );
  const selectableVoiceRoutes = useMemo(() => {
    const routes = new Map();
    voiceProviders.filter((provider) => provider.selectable).forEach((provider) => {
      if (!routes.has(provider.ttsApi)) routes.set(provider.ttsApi, provider.label);
    });
    return Array.from(routes, ([value, label]) => ({ value, label }));
  }, [voiceProviders]);
  const activeVoiceProvider = useMemo(
    () => voiceProviders.find((provider) => provider.selectable && provider.ttsApi === ttsApi && provider.voices?.includes(voice))
      || voiceProviders.find((provider) => provider.selectable && provider.ttsApi === ttsApi)
      || null,
    [ttsApi, voice, voiceProviders],
  );
  const roomInfoPayload = useMemo(
    () => images.map((image) => ({
      filename: image.name,
      header: imageHeaders[image.name] || '',
      roomDescription: imageRoomInfo[image.name]?.roomDescription || '',
      label: imageRoomInfo[image.name]?.label || '',
      confidence: imageRoomInfo[image.name]?.confidence ?? null,
      source: imageRoomInfo[image.name]?.source || 'manual',
    })),
    [images, imageHeaders, imageRoomInfo],
  );
  const hasRoomInfo = roomInfoPayload.some((item) => (
    item.header.trim() || item.roomDescription.trim() || item.label.trim()
  ));

  const canRender = scenesPayload.scenes.length > 0
    && images.length > 0
    && scenesPayload.scenes.every((scene) => scene.images.length > 0);
  const resolvedIntroTitle = joinLeadLines(introLines);
  const canEnhance = script.trim().length > 0
    && Number.isFinite(Number(targetSeconds))
    && Number(targetSeconds) >= 5
    && !isEnhancing;
  const hasSavedProject = Boolean(lastSavedAt) && savedProjectId === resolvedProjectId;
  const renderSteps = renderStepStates(renderTracker);
  const selectedImagePreview = useMemo(
    () => imagePreviews.find((preview) => preview.name === selectedImageName) || null,
    [imagePreviews, selectedImageName],
  );
  const previewVideoName = useMemo(
    () => outputs.find((file) => /\.(mp4|mov|mpg|mpeg|webm)$/i.test(file)) || '',
    [outputs],
  );
  const previewPosterName = useMemo(
    () => outputs.find((file) => /\.(jpg|jpeg|png|webp)$/i.test(file)) || '',
    [outputs],
  );
  const previewVideoHref = videoHref || (previewVideoName ? outputHref(previewVideoName) : '');
  const previewPosterHref = previewPosterName ? outputHref(previewPosterName) : '';

  useEffect(() => localStorage.setItem('renderUi.apiBase', apiBase), [apiBase]);
  useEffect(() => localStorage.setItem('renderUi.authToken', authToken), [authToken]);
  useEffect(() => localStorage.setItem('renderUi.theme', theme), [theme]);
  useEffect(() => {
    if ((ttsApi === 'voice-gateway' || ttsApi === 'vibevoice-proxy') && /neural$/i.test(voice)) {
      setVoice('Carter');
    } else if (ttsApi === 'azure-proxy' && (!voice || !/neural$/i.test(voice))) {
      setVoice('en-US-AdamMultilingualNeural');
    } else if (ttsApi === 'flite' && (!voice || /^en-/i.test(voice) || /neural/i.test(voice))) {
      setVoice('kal');
    }
  }, [ttsApi, voice]);
  useEffect(() => {
    if (!activeVoiceProvider?.voices?.length || activeVoiceProvider.voices.includes(voice)) return;
    setVoice(activeVoiceProvider.voices[0]);
  }, [activeVoiceProvider, voice]);
  useEffect(() => {
    if (!hasSavedProject) {
      setOutputs([]);
      return;
    }
    refreshOutputs().catch(() => {});
  }, [apiBase, authToken, resolvedProjectId, hasSavedProject]);
  useEffect(() => {
    if (!hasSavedProject) {
      setVersions([]);
    }
  }, [resolvedProjectId, hasSavedProject]);
  useEffect(() => {
    refreshProjects().catch(() => {});
    refreshSoundCatalog().catch(() => {});
  }, [apiBase, authToken]);
  useEffect(() => {
    request('/v1/brand-assets')
      .then((result) => {
        const files = result.files || [];
        setBrandAssets(files);
        if (!files.some((file) => file.name === logoChoice) && files[0]) {
          setLogoChoice(files[0].name);
        }
      })
      .catch(() => {});
  }, [apiBase, authToken]);
  useEffect(() => {
    request('/v1/voice-options')
      .then((result) => setVoiceProviders(result.providers || []))
      .catch(() => setVoiceProviders([]));
  }, [apiBase, authToken]);
  useEffect(() => {
    refreshYoutubeAuth().catch(() => {});
  }, [apiBase, authToken, youtubeProfile]);
  useEffect(() => {
    request('/v1/youtube/channels')
      .then((result) => setYoutubeChannels(result.channels || []))
      .catch(() => setYoutubeChannels([]));
  }, [apiBase, authToken]);
  useEffect(() => {
    const previews = images.map((image) => ({
      name: image.name,
      url: image.url || (image.file ? URL.createObjectURL(image.file) : missingImageDataUrl(image.name)),
      missing: Boolean(image.missing),
      revoke: !image.url && Boolean(image.file),
    }));
    setImagePreviews(previews);
    return () => previews.forEach((preview) => {
      if (preview.revoke) URL.revokeObjectURL(preview.url);
    });
  }, [images]);

  function headers(extra = {}) {
    return { ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}), ...extra };
  }

  async function request(path, options = {}) {
    const response = await fetch(apiUrl(apiBase, path), {
      ...options,
      headers: headers(options.headers || {}),
    });
    if (!response.ok) {
      const detail = await response.text().catch(() => '');
      throw new Error(`${response.status} ${response.statusText}${detail ? `: ${detail}` : ''}`);
    }
    const contentType = response.headers.get('content-type') || '';
    return contentType.includes('application/json') ? response.json() : response;
  }

  async function pollJob(jobId) {
    let pollFailures = 0;
    for (;;) {
      let current;
      try {
        current = await request(`/v1/jobs/${encodeURIComponent(jobId)}`);
        pollFailures = 0;
      } catch (err) {
        pollFailures += 1;
        if (pollFailures >= 5) throw err;
        setStatus(`Waiting for render status (${pollFailures}/5)`);
        await new Promise((resolve) => setTimeout(resolve, 1500));
        continue;
      }

      setJob(current);
      setLogs((previous) => current.logs || previous);
      setStatus(`${current.status} - ${current.stage || 'waiting'} (${Math.round((current.progress || 0) * 100)}%)`);
      setRenderTracker((previous) => ({
        ...previous,
        phase: current.status === 'SUCCEEDED' ? 'complete' : 'rendering',
        progress: current.status === 'SUCCEEDED'
          ? 1
          : renderTrackerProgress({ phase: 'rendering', jobProgress: current.progress }),
        message: current.status === 'SUCCEEDED'
          ? 'Video and thumbnail are ready'
          : renderStageLabel(current.stage),
        stage: current.stage || '',
        jobId,
      }));
      if (current.status === 'SUCCEEDED') return current;
      if (current.status === 'FAILED' || current.status === 'CANCELLED') {
        throw new Error(current.error || `Render ${current.status.toLowerCase()}`);
      }
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
  }

  function outputHref(filename) {
    return apiUrl(apiBase, `/v1/projects/${encodeURIComponent(resolvedProjectId)}/outputs/video?filename=${encodeURIComponent(filename)}`);
  }

  async function refreshOutputs() {
    const result = await request(`/v1/projects/${encodeURIComponent(resolvedProjectId)}/outputs`);
    setOutputs(result.files || []);
  }

  async function refreshProjects() {
    setIsLoadingProjects(true);
    try {
      const result = await request('/v1/projects');
      setSavedProjects(result.projects || []);
    } finally {
      setIsLoadingProjects(false);
    }
  }

  async function runNewestTenListingPipeline() {
    if (!newestTenProjects.length || listingPipelineRun?.status === 'RUNNING' || listingPipelineRun?.status === 'QUEUED') return;
    setError('');
    setStatus(`Queueing listing pipeline for ${newestTenProjects.length} newest projects`);
    try {
      const run = await request('/v1/listing-pipeline/runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ projectIds: newestTenProjects.map((project) => project.projectId) }),
      });
      setListingPipelineRun(run);
      while (true) {
        await new Promise((resolve) => setTimeout(resolve, 1500));
        const current = await request(`/v1/listing-pipeline/runs/${encodeURIComponent(run.runId)}`);
        setListingPipelineRun(current);
        setStatus(`${current.status}: ${current.results.length}/${current.projectIds.length} listing projects prepared`);
        if (current.status === 'COMPLETED') {
          await refreshProjects();
          return;
        }
      }
    } catch (err) {
      setError(err.message || String(err));
      setStatus('Listing pipeline failed to start');
    }
  }

  async function refreshSoundCatalog() {
    setIsLoadingSoundCatalog(true);
    try {
      const result = await request('/v1/sfx/catalog');
      setSoundCatalog(result);
    } catch (err) {
      setSoundCatalog({ assets: [], count: 0, status: 'unavailable', error: err.message || String(err) });
    } finally {
      setIsLoadingSoundCatalog(false);
    }
  }

  function resetEditor() {
    setTitle('Listing video');
    setScriptTitle('');
    setPropertyAddress('');
    setProjectId('');
    setScript('Welcome to this property. The opening view should establish the home and the neighborhood.\n\nStep inside to highlight the living spaces, natural light, and everyday comfort.\n\nFinish with the strongest selling points and a clear invitation to schedule a showing.');
    setOriginalScriptText('');
    setTargetSeconds(60);
    setSceneDurations([]);
    setSceneImageAssignments([]);
    setImages([]);
    setRemovedImages([]);
    setImageHeaders({});
    setImageLabelEnabled({});
    setImageRoomInfo({});
    setDraggedImageName('');
    setSelectedImageName('');
    setUseIntro(true);
    setIntroLines(['', '', '', '', '', '']);
    setLeaderImage(null);
    setUseLogo(true);
    setLogoImage(null);
    setLogoCorner('top-right');
    setLogoMargin(24);
    setOutputName('video.mp4');
    setRenderOptions({});
    setRenderTracker(EMPTY_RENDER_TRACKER);
    setYoutubeTitle('');
    setYoutubeDescription('');
    setYoutubeTags('');
    setYoutubePrivacy('private');
    setYoutubeProfile('english');
    setYoutubeResult('');
    setYoutubeVideoId('');
    setYoutubeThumbnailApplied(false);
    setYoutubeThumbnailFilename('');
    setYoutubeThumbnailError('');
    setYoutubeCallbackUrl('');
    setJob(null);
    setLogs('');
    setVideoHref('');
    setOutputs([]);
    setError('');
    setLastSavedAt('');
    setSavedProjectId('');
    setVersions([]);
    setStatus('New project');
    setView('editor');
  }

  function assetByName(assets, subdir, name) {
    if (!name) return null;
    return (assets?.[subdir] || []).find((asset) => asset.name === name) || null;
  }

  async function loadProject(pid) {
    setError('');
    setIsLoadingProject(true);
    setRenderTracker(EMPTY_RENDER_TRACKER);
    setStatus(`Loading ${pid}`);
    try {
      const result = await request(`/v1/projects/${encodeURIComponent(pid)}`);
      const state = result.state || {};
      const persistedScenes = result.scenes?.scenes || [];
      if (state.workflow === 'bible-video') {
        setBibleProjectId(pid);
        setView('bible');
        setStatus(`Loaded ${pid}`);
        return;
      }
      const projectImages = state.images?.length
        ? state.images
          .map((image) => stateImageItem(image, result.assets, 'images', apiBase))
          .filter(Boolean)
        : (result.assets?.images || []).map((asset) => persistedItem(asset, apiBase));
      const leaderAsset = assetByName(result.assets, 'leader', state.leaderImageName);
      const logoAsset = assetByName(result.assets, 'logo', state.logoImageName);
      const savedSceneDurations = Array.isArray(state.sceneDurations)
        ? state.sceneDurations
        : persistedScenes.map((scene) => scene.duration || '');
      const savedSceneImageAssignments = sceneImageAssignmentsFromProject(state, persistedScenes);
      const removedProjectImages = (state.removedImages || [])
        .map((image) => stateImageItem(image, result.assets, 'images', apiBase))
        .filter(Boolean)

      setTitle(state.title || pid);
      setScriptTitle(state.scriptTitle || '');
      setPropertyAddress(state.propertyAddress || '');
      setProjectId(pid);
      const loadedScript = scriptFromProject(state, persistedScenes);
      const savedOriginalScript = String(state.originalScriptText || loadedScript).trim();
      setScript(loadedScript);
      setOriginalScriptText(savedOriginalScript);
      setTargetSeconds(state.targetSeconds || 60);
      setSceneDurations(savedSceneDurations);
      setSceneImageAssignments(sanitizeSceneImageAssignments(savedSceneImageAssignments, projectImages.map((image) => image.name)));
      setOutputName(state.outputName || 'video.mp4');
      setImages(projectImages);
      setRemovedImages(removedProjectImages);
      setImageHeaders(imageHeadersFromProject(state, persistedScenes));
      setImageLabelEnabled(state.imageLabelEnabled || {});
      setImageRoomInfo(state.imageRoomInfo || {});
      setUseIntro(state.useIntro ?? true);
      const parsedTitleCard = titleCardFromScript(scriptFromProject(state, persistedScenes), state.scriptTitle || state.title);
      const savedLeadLines = leadLinesFromState(state, '');
      setIntroLines(savedLeadLines.some(Boolean)
        ? savedLeadLines
        : Array.from({ length: 6 }, (_, index) => [parsedTitleCard.mainTitle, ...parsedTitleCard.lines][index] || ''));
      setLeaderImage(leaderAsset ? persistedItem(leaderAsset, apiBase) : null);
      setUseLogo(state.useLogo ?? true);
      setLogoChoice(state.logoChoice || 'logophone.png');
      setLogoImage(logoAsset ? persistedItem(logoAsset, apiBase) : null);
      setLogoCorner(state.logoCorner || 'top-right');
      setLogoMargin(state.logoMargin ?? 24);
      setVoice(state.voice || voice);
      const savedLanguage = state.language || language;
      setLanguage(savedLanguage);
      setTtsApi(state.ttsApi === 'vibevoice-proxy' ? 'voice-gateway' : (state.ttsApi || ttsApi));
      const savedHyperframes = state.hyperframes || persistedScenes?.hyperframes || {};
      setHyperframesLeadCard(savedHyperframes.leadCard || 'lecrown-lead-card');
      setHyperframesSceneTransition(savedHyperframes.sceneTransition || 'lecrown-scene-transition');
      setHyperframesTransitionTitles(savedHyperframes.transitionTitles || {});
      setLeadCardMode(savedHyperframes.leadCardMode || 'hyperframes');
      setSceneTransitionMode(savedHyperframes.sceneTransitionMode || 'hyperframes');
      setRenderOptions(renderOptionsFromProject(state));
      setYoutubeTitle(state.youtubeTitle || '');
      setYoutubeDescription(state.youtubeDescription || savedOriginalScript);
      setYoutubeTags(state.youtubeTags || '');
      setYoutubePrivacy(state.youtubePrivacy || 'private');
      setYoutubeProfile(state.youtubeProfile || youtubeProfileForLanguage(savedLanguage));
      setYoutubeVideoId(state.youtubeUpload?.videoId || '');
      setYoutubeResult(state.youtubeUpload?.url || '');
      setYoutubeThumbnailApplied(Boolean(state.youtubeUpload?.thumbnailApplied));
      setYoutubeThumbnailFilename(state.youtubeUpload?.thumbnailFilename || '');
      setYoutubeThumbnailError(state.youtubeUpload?.thumbnailError || '');
      setOutputs(result.outputs || []);
      setVideoHref('');
      setJob(null);
      setLogs('');
      setLastSavedAt(state.updatedAt ? new Date(state.updatedAt * 1000).toLocaleString() : '');
      setSavedProjectId(pid);
      setVersions(result.versions || []);
      setView('editor');
      setStatus(`Loaded ${pid}`);
    } catch (err) {
      setError(err.message || String(err));
      setStatus('Project load failed');
    } finally {
      setIsLoadingProject(false);
    }
  }

  function handleImageSelection(files) {
    const selected = Array.from(files || []);
    const selectedItems = selected.map(fileItem);
    setImages((current) => {
      const incomingNames = new Set(selectedItems.map((image) => image.name));
      return [
        ...selectedItems,
        ...current.filter((image) => !incomingNames.has(image.name)),
      ];
    });
    setRemovedImages((current) => current.filter((image) => (
      !selectedItems.some((selectedImage) => selectedImage.name === image.name)
    )));
    setImageHeaders((current) => {
      const next = { ...current };
      selectedItems.forEach((image) => {
        next[image.name] = current[image.name] || '';
      });
      return next;
    });
    setImageRoomInfo((current) => {
      const next = { ...current };
      selectedItems.forEach((image) => {
        next[image.name] = current[image.name] || { roomDescription: '', label: '', confidence: null, source: 'manual' };
      });
      return next;
    });
    if (selectedItems.length) {
      setStatus(`${selectedItems.length} image${selectedItems.length === 1 ? '' : 's'} added to top`);
    }
  }

  function updateImageHeader(imageName, header) {
    setImageHeaders((current) => ({ ...current, [imageName]: header }));
  }

  function setImageLabelVisibility(imageName, enabled) {
    setImageLabelEnabled((current) => ({ ...current, [imageName]: enabled }));
  }

  function updateImageRoomInfo(imageName, values) {
    setImageRoomInfo((current) => ({
      ...current,
      [imageName]: {
        roomDescription: '',
        label: '',
        confidence: null,
        source: 'manual',
        ...(current[imageName] || {}),
        ...values,
      },
    }));
  }

  function addImageToScene(sceneIndex, imageName) {
    if (!imageName) return;
    setSceneImageAssignments((current) => {
      const next = current.map((sceneImages) => (
        Array.isArray(sceneImages) ? sceneImages.filter((name) => name !== imageName) : []
      ));
      const currentSceneImages = next[sceneIndex] || [];
      if (currentSceneImages.length >= 3) return current;
      next[sceneIndex] = [...currentSceneImages, imageName];
      return next;
    });
    setStatus(`Added ${imageName} to scene ${sceneIndex + 1}`);
  }

  function removeImageFromScene(sceneIndex, imageName) {
    setSceneImageAssignments((current) => {
      const next = current.map((sceneImages) => (Array.isArray(sceneImages) ? [...sceneImages] : []));
      next[sceneIndex] = (next[sceneIndex] || []).filter((name) => name !== imageName);
      return next;
    });
    setStatus(`Removed ${imageName} from scene ${sceneIndex + 1}`);
  }

  function removeImage(imageName) {
    setImages((current) => {
      const removed = current.find((image) => image.name === imageName);
      if (removed) {
        setRemovedImages((existing) => (
          existing.some((image) => image.name === removed.name) ? existing : [...existing, removed]
        ));
      }
      return current.filter((image) => image.name !== imageName);
    });
    setSceneImageAssignments((current) => current.map((sceneImages) => (
      Array.isArray(sceneImages) ? sceneImages.filter((name) => name !== imageName) : []
    )));
    if (selectedImageName === imageName) {
      setSelectedImageName('');
    }
    setStatus('Image removed');
  }

  function restoreRemovedImage(imageName) {
    setRemovedImages((current) => {
      const restored = current.find((image) => image.name === imageName);
      if (restored) {
        setImages((existing) => (
          existing.some((image) => image.name === restored.name) ? existing : [...existing, restored]
        ));
      }
      return current.filter((image) => image.name !== imageName);
    });
    setStatus('Image restored');
  }

  function moveImageBefore(sourceName, targetName) {
    if (!sourceName || !targetName || sourceName === targetName) return;
    setImages((current) => {
      const source = current.find((image) => image.name === sourceName);
      if (!source) return current;
      const withoutSource = current.filter((image) => image.name !== sourceName);
      const targetIndex = withoutSource.findIndex((image) => image.name === targetName);
      if (targetIndex < 0) return current;
      const next = [...withoutSource];
      next.splice(targetIndex, 0, source);
      return next;
    });
    setStatus('Image order changed');
  }

  function moveImageToPosition(sourceName, targetIndex) {
    if (!sourceName) return;
    setImages((current) => {
      const sourceIndex = current.findIndex((image) => image.name === sourceName);
      const source = current[sourceIndex];
      if (!source) return current;
      const withoutSource = current.filter((image) => image.name !== sourceName);
      const adjustedIndex = sourceIndex < targetIndex ? targetIndex - 1 : targetIndex;
      const boundedIndex = Math.max(0, Math.min(adjustedIndex, withoutSource.length));
      const next = [...withoutSource];
      next.splice(boundedIndex, 0, source);
      return next;
    });
    setStatus('Image order changed');
  }

  async function saveRoomAnnotations() {
    if (!images.length) return;
    await request(`/v1/projects/${encodeURIComponent(resolvedProjectId)}/room-annotations`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ annotations: roomInfoPayload }),
    });
  }

  function applyRoomNamingResults(results = []) {
    setImageRoomInfo((current) => {
      const next = { ...current };
      results.forEach((item) => {
        next[item.filename] = {
          ...(next[item.filename] || {}),
          label: item.label || '',
          displayLabel: item.displayLabel || '',
          confidence: item.confidence ?? null,
          source: item.source || 'room-renamer',
        };
      });
      return next;
    });
    setImageHeaders((current) => {
      const next = { ...current };
      results.forEach((item) => {
        if (item.displayLabel && !String(next[item.filename] || '').trim()) {
          next[item.filename] = item.displayLabel;
        }
      });
      return next;
    });
  }

  async function classifyRooms() {
    if (!images.length || isClassifyingRooms) return;
    setError('');
    setIsClassifyingRooms(true);
    setStatus('Classifying rooms');
    try {
      const form = new FormData();
      for (const image of images) {
        if (image.file) {
          form.append('files', image.file, image.name);
        } else if (image.url) {
          const blob = await fetch(image.url).then((response) => response.blob());
          form.append('files', blob, image.name);
        }
      }
      const result = await request(`/v1/projects/${encodeURIComponent(resolvedProjectId)}/room-classify`, {
        method: 'POST',
        body: form,
      });
      applyRoomNamingResults(result.results || []);
      setStatus('Room classification complete');
    } catch (err) {
      setStatus('Room classification failed');
      setError(err.message || String(err));
    } finally {
      setIsClassifyingRooms(false);
    }
  }

  async function saveRoomCorrection(filename) {
    const correctedLabel = String(imageRoomInfo[filename]?.label || '').trim();
    if (!correctedLabel || correctingRoomName) return;
    setError('');
    setCorrectingRoomName(filename);
    setStatus(`Saving ${filename} correction for training`);
    try {
      const result = await request(`/v1/projects/${encodeURIComponent(resolvedProjectId)}/room-corrections`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename, correctedLabel }),
      });
      applyRoomNamingResults(result.result ? [result.result] : []);
      setStatus('Room correction saved and added to Room Renamer training data');
    } catch (err) {
      setStatus('Room correction failed');
      setError(err.message || String(err));
    } finally {
      setCorrectingRoomName('');
    }
  }

  function handleLeaderSelection(files) {
    const selected = Array.from(files || []);
    setLeaderImage(selected[0] ? fileItem(selected[0]) : null);
  }

  function handleLogoSelection(files) {
    const selected = Array.from(files || []);
    setLogoImage(selected[0] ? fileItem(selected[0]) : null);
  }

  async function uploadAssetItems(items, subdir) {
    const pending = items.filter((item) => item?.file);
    if (!pending.length) return;
    const form = new FormData();
    pending.forEach((item) => form.append('files', item.file, item.name));
    form.append('subdir', subdir);
    await request(`/v1/projects/${encodeURIComponent(resolvedProjectId)}/assets`, { method: 'POST', body: form });
  }

  async function saveProject(overrides = {}) {
    if (overrides?.preventDefault) overrides = {};
    setError('');
    setIsSavingProject(true);
    try {
      const sourceScript = originalScriptText || script;
      const descriptionFromSource = youtubeDescription || sourceScript;
      if (!originalScriptText.trim()) setOriginalScriptText(sourceScript);
      if (!youtubeDescription.trim() && sourceScript.trim()) setYoutubeDescription(sourceScript);
      setStatus('Saving project');
      await request('/healthz');
      await uploadAssetItems(images, 'images');
      if (leaderImage) await uploadAssetItems([leaderImage], 'leader');
      if (logoImage) await uploadAssetItems([logoImage], 'logo');
      const scenesAreRenderable = scenesPayload.scenes.length > 0
        && scenesPayload.scenes.every((scene) => scene.images.length > 0);
      if (scenesAreRenderable) {
        await saveRoomAnnotations();
        await request(`/v1/projects/${encodeURIComponent(resolvedProjectId)}/scenes`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(scenesPayload),
        });
      }
      const result = await request(`/v1/projects/${encodeURIComponent(resolvedProjectId)}/state`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          state: projectStateFromValues({
            title,
            scriptTitle,
            propertyAddress,
            resolvedProjectId,
            script,
            originalScriptText: sourceScript,
            targetSeconds,
            sceneDurations,
            sceneImageAssignments,
            outputName,
            images,
            removedImages,
            imageHeaders,
            imageLabelEnabled,
            imageRoomInfo,
            useIntro,
            introLines,
            leaderImage,
            useLogo,
            logoChoice,
            logoImage,
            logoCorner,
            logoMargin,
            voice,
            language,
            ttsApi,
            hyperframes: overrides.hyperframes ?? selectedHyperframes,
            renderOptions,
            youtubeTitle,
            youtubeDescription: overrides.youtubeDescription ?? descriptionFromSource,
            youtubeTags,
            youtubePrivacy,
            youtubeProfile,
            youtubeVideoId,
            youtubeResult,
            youtubeThumbnailApplied,
            youtubeThumbnailFilename,
            youtubeThumbnailError,
          }),
        }),
      });
      setLastSavedAt(new Date((result.state?.updatedAt || Date.now() / 1000) * 1000).toLocaleString());
      setSavedProjectId(resolvedProjectId);
      setStatus('Project saved');
      await refreshVersions({ force: true });
      await refreshProjects();
      return result;
    } catch (err) {
      setStatus('Save failed');
      setError(err.message || String(err));
      throw err;
    } finally {
      setIsSavingProject(false);
    }
  }

  async function refreshYoutubeAuth() {
    const result = await request(`/v1/youtube/auth/status?profile=${encodeURIComponent(youtubeProfile)}`);
    setYoutubeAuth(result);
  }

  async function refreshVersions({ force = false } = {}) {
    if (!force && !hasSavedProject) {
      setVersions([]);
      return;
    }
    const result = await request(`/v1/projects/${encodeURIComponent(resolvedProjectId)}/versions`);
    setVersions(result.versions || []);
  }

  async function restoreVersion(versionId) {
    if (!versionId || isRestoringVersion || !hasSavedProject) return;
    setError('');
    setIsRestoringVersion(true);
    try {
      setStatus('Restoring version');
      await request(`/v1/projects/${encodeURIComponent(resolvedProjectId)}/versions/${encodeURIComponent(versionId)}/restore`, {
        method: 'POST',
      });
      await loadProject(resolvedProjectId);
      setStatus('Version restored');
    } catch (err) {
      setStatus('Restore failed');
      setError(err.message || String(err));
    } finally {
      setIsRestoringVersion(false);
    }
  }

  async function startYoutubeAuthorization() {
    const result = await request(`/v1/youtube/auth/start?profile=${encodeURIComponent(youtubeProfile)}`, { method: 'POST' });
    setYoutubeAuth((current) => ({ ...(current || {}), ...result }));
    return result;
  }

  async function copyYoutubeAuthorizationLink(authUrl) {
    const copyUrl = authUrl || youtubeAuth?.authUrl;
    if (!copyUrl) return;
    if (navigator.clipboard?.writeText && window.isSecureContext) {
      await navigator.clipboard.writeText(copyUrl);
      return;
    }
    const temporaryField = document.createElement('textarea');
    temporaryField.value = copyUrl;
    temporaryField.setAttribute('readonly', '');
    temporaryField.style.position = 'fixed';
    temporaryField.style.opacity = '0';
    document.body.appendChild(temporaryField);
    temporaryField.select();
    const copied = document.execCommand('copy');
    document.body.removeChild(temporaryField);
    if (!copied) throw new Error('Browser blocked clipboard access');
  }

  async function connectYoutube() {
    setError('');
    setIsConnectingYoutube(true);
    try {
      const result = await startYoutubeAuthorization();
      window.open(result.authUrl, '_blank', 'noopener,noreferrer');
      setStatus(result.manualCallback ? 'Authorize YouTube, then paste the callback URL' : 'Authorize YouTube in the new browser tab');
      if (!result.manualCallback) {
        for (let attempt = 0; attempt < 60; attempt += 1) {
          await new Promise((resolve) => setTimeout(resolve, 2000));
          const statusResult = await request(`/v1/youtube/auth/status?profile=${encodeURIComponent(youtubeProfile)}`);
          setYoutubeAuth((current) => ({ ...(current || {}), ...statusResult, authUrl: result.authUrl }));
          if (statusResult.authenticated) {
            const catalog = await request('/v1/youtube/channels?force=true');
            setYoutubeChannels(catalog.channels || []);
            setStatus('YouTube connected');
            return;
          }
        }
        setStatus('YouTube auth pending');
      }
    } catch (err) {
      setError(err.message || String(err));
    } finally {
      setIsConnectingYoutube(false);
    }
  }

  async function connectYoutubeInExternalBrowser() {
    setError('');
    setIsConnectingYoutube(true);
    try {
      const result = await startYoutubeAuthorization();
      await copyYoutubeAuthorizationLink(result.authUrl);
      setStatus('YouTube authorization link copied for your external browser');
    } catch (err) {
      setError(`Could not prepare the authorization link: ${err.message || String(err)}`);
    } finally {
      setIsConnectingYoutube(false);
    }
  }

  async function completeYoutubeAuth() {
    setError('');
    try {
      const result = await request(`/v1/youtube/auth/complete?profile=${encodeURIComponent(youtubeProfile)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ callbackUrl: youtubeCallbackUrl.trim() }),
      });
      setYoutubeAuth(result);
      setYoutubeCallbackUrl('');
      setStatus('YouTube connected');
    } catch (err) {
      setError(err.message || String(err));
      setStatus('YouTube auth failed');
    }
  }

  async function uploadYoutube() {
    setError('');
    setYoutubeResult('');
    setIsUploadingYoutube(true);
    try {
      const selectedOutput = videoHref ? outputName : (outputs.find((file) => file.endsWith('.mp4') || file.endsWith('.mpg') || file.endsWith('.mov')) || outputName);
      const description = youtubeDescription || originalScriptText || script;
      const result = await request(`/v1/projects/${encodeURIComponent(resolvedProjectId)}/youtube/upload`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          filename: selectedOutput,
          title: youtubeTitle.trim() || title || selectedOutput,
          description,
          tags: youtubeTags.split(',').map((tag) => tag.trim()).filter(Boolean),
          privacyStatus: youtubePrivacy,
          categoryId: '22',
          madeForKids: false,
          profile: youtubeProfile,
        }),
      });
      setYoutubeResult(result.url);
      setYoutubeVideoId(result.videoId);
      setYoutubeThumbnailApplied(Boolean(result.thumbnailApplied));
      setYoutubeThumbnailFilename(result.thumbnailFilename || '');
      setYoutubeThumbnailError(result.thumbnailError || '');
      setStatus(result.thumbnailApplied ? 'YouTube upload and thumbnail complete' : 'YouTube upload complete; thumbnail needs attention');
    } catch (err) {
      setError(err.message || String(err));
      setStatus('YouTube upload failed');
    } finally {
      setIsUploadingYoutube(false);
      refreshYoutubeAuth().catch(() => {});
    }
  }

  async function applyYoutubeThumbnail() {
    const videoId = youtubeVideoId.trim();
    if (!videoId || !previewPosterName || isApplyingYoutubeThumbnail) return;

    setError('');
    setIsApplyingYoutubeThumbnail(true);
    setStatus('Applying YouTube thumbnail');
    try {
      const result = await request(`/v1/projects/${encodeURIComponent(resolvedProjectId)}/youtube/thumbnail`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          videoId,
          filename: previewPosterName,
          profile: youtubeProfile,
        }),
      });
      setYoutubeVideoId(result.videoId);
      setYoutubeResult(result.url);
      setYoutubeThumbnailApplied(true);
      setYoutubeThumbnailFilename(result.thumbnailFilename);
      setYoutubeThumbnailError('');
      setStatus('YouTube thumbnail applied');
    } catch (err) {
      setYoutubeThumbnailApplied(false);
      setYoutubeThumbnailError(err.message || String(err));
      setError(err.message || String(err));
      setStatus('YouTube thumbnail failed');
    } finally {
      setIsApplyingYoutubeThumbnail(false);
    }
  }

  async function updateYoutubeDetails() {
    const videoId = youtubeVideoId.trim();
    if (!videoId || isUpdatingYoutubeDetails) return;

    setError('');
    setIsUpdatingYoutubeDetails(true);
    setStatus('Updating YouTube details');
    try {
      const description = youtubeDescription || originalScriptText || script;
      const result = await request(`/v1/projects/${encodeURIComponent(resolvedProjectId)}/youtube/metadata`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          videoId,
          title: youtubeTitle.trim() || title || outputName,
          description,
          tags: youtubeTags.split(',').map((tag) => tag.trim()).filter(Boolean),
          privacyStatus: youtubePrivacy,
          categoryId: '22',
          madeForKids: false,
          profile: youtubeProfile,
        }),
      });
      setYoutubeResult(result.url);
      setStatus('YouTube details updated');
    } catch (err) {
      setError(err.message || String(err));
      setStatus('YouTube details update failed');
    } finally {
      setIsUpdatingYoutubeDetails(false);
    }
  }

  async function writeYoutubeDescription() {
    if (isWritingYoutubeDescription || (!script.trim() && !youtubeDescription.trim())) return;

    setError('');
    setIsWritingYoutubeDescription(true);
    setStatus('Writing YouTube description');
    try {
      const result = await request('/v1/youtube/description/enhance', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: youtubeTitle.trim() || title || outputName,
          script: originalScriptText || script,
          currentDescription: youtubeDescription,
          roomInfo: roomInfoPayload,
        }),
      });
      const nextDescription = result.description || youtubeDescription;
      setYoutubeDescription(nextDescription);
      await saveProject({ youtubeDescription: nextDescription });
      setStatus(`YouTube description written and saved with ${result.model || 'Ollama'}`);
    } catch (err) {
      setError(err.message || String(err));
      setStatus('YouTube description failed');
    } finally {
      setIsWritingYoutubeDescription(false);
    }
  }

  async function generateLeadCard() {
    if (isGeneratingIntro || (!title.trim() && !script.trim())) return;

    setError('');
    setIsGeneratingIntro(true);
    setStatus('Generating lead card with Fortress Ollama');
    try {
      const result = await request('/v1/lead-card/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, script, currentLines: introLines }),
      });
      const lines = Array.isArray(result.lines) ? result.lines.slice(0, 6) : [];
      setIntroLines(Array.from({ length: 6 }, (_, index) => lines[index] || ''));
      setUseIntro(true);
      setStatus(`Lead card generated with ${result.model || 'Fortress Ollama'}`);
    } catch (err) {
      setError(err.message || String(err));
      setStatus('Lead card generation failed');
    } finally {
      setIsGeneratingIntro(false);
    }
  }

  async function openGeneratedHyperframesTransition(index, scene, nextScene, variables) {
    if (!nextScene || generatingTransitionIndex !== null) return;

    const hyperframesWindow = window.open('', '_blank');
    setError('');
    setGeneratingTransitionIndex(index);
    setStatus(`Generating transition title ${index + 1} with Fortress Ollama`);
    try {
      const result = await request('/v1/hyperframes/transition-title/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          outgoingTitle: scene?.title || `Scene ${index + 1}`,
          incomingTitle: nextScene.title || `Scene ${index + 2}`,
          narration: nextScene.narration || '',
          language,
        }),
      });
      const transitionTitle = result.title || nextScene.title || `Scene ${index + 2}`;
      const nextTransitionTitles = { ...hyperframesTransitionTitles, [index]: transitionTitle };
      setHyperframesTransitionTitles(nextTransitionTitles);
      await saveProject({
        hyperframes: hyperframesBinding(
          hyperframesLeadCard,
          hyperframesSceneTransition,
          nextTransitionTitles,
          leadCardMode,
          sceneTransitionMode,
        ),
      });
      const url = hyperframesEditUrl(hyperframesSceneTransition, { ...variables, transition_title: transitionTitle });
      if (hyperframesWindow) hyperframesWindow.location.assign(url);
      else window.open(url, '_blank', 'noopener,noreferrer');
      setStatus(`Transition title generated with ${result.model || 'Fortress Ollama'}`);
    } catch (err) {
      if (hyperframesWindow && !hyperframesWindow.closed) hyperframesWindow.close();
      setError(err.message || String(err));
      setStatus('Transition title generation failed');
    } finally {
      setGeneratingTransitionIndex(null);
    }
  }

  async function enhanceScript({ withRoomInfo = false } = {}) {
    if (!canEnhance) return;

    setError('');
    setIsEnhancing(true);
    setStatus(withRoomInfo ? 'Enhancing script with room info' : 'Enhancing script');
    try {
      if (!originalScriptText.trim()) setOriginalScriptText(script);
      const result = await request('/v1/script/enhance', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          script,
          targetSeconds: Number(targetSeconds),
          roomInfo: withRoomInfo ? roomInfoPayload : [],
        }),
      });
      setScript(result.script || script);
      setStatus(`Enhanced with ${result.model || 'Ollama'}`);
    } catch (err) {
      setStatus('Enhance failed');
      setError(err.message || String(err));
    } finally {
      setIsEnhancing(false);
    }
  }

  async function splitScriptForImages() {
    if (!script.trim() || images.length < 1) return;
    setError('');
    setIsEnhancing(true);
    setStatus('Rewriting scene copy with room information');
    try {
      const result = await request('/v1/script/enhance', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          script: recognizedScriptTitle.narration,
          targetSeconds: Number(targetSeconds),
          roomInfo: roomInfoPayload,
          scenePlan: roomInfoPayload.map((room) => ({
            filename: room.filename,
            room: room.header || room.label || 'Property detail',
            notes: room.roomDescription || '',
          })),
        }),
      });
      const sceneTexts = Array.isArray(result.sceneTexts) ? result.sceneTexts.filter(Boolean) : [];
      if (sceneTexts.length !== images.length) {
        throw new Error('Ollama did not return narration for every image scene.');
      }
      const nextNarration = sceneTexts.join('\n\n');
      const titlePrefix = recognizedScriptTitle.title ? `${recognizedScriptTitle.title}\n\n` : '';
      const nextScript = `${titlePrefix}${nextNarration}`.trim();
      setScript(nextScript);
      setSceneImageAssignments(images.map((image) => [image.name]));
      setStatus(`Rewrote and split the script into ${splitScript(nextNarration).length} image scenes with ${result.model || 'Ollama'}`);
    } catch (err) {
      setStatus('Image scene rewrite failed');
      setError(err.message || String(err));
    } finally {
      setIsEnhancing(false);
    }
  }

  function buildScenesFromRoomLabels() {
    if (!script.trim() || !images.length) return;
    const assignments = sceneAssignmentsFromRoomInfo(images, imageRoomInfo);
    if (!assignments.length || assignments.every((scene) => !scene.length)) {
      setError('Run Auto-name rooms before building scenes from room labels.');
      return;
    }
    const nextScript = splitScriptIntoSections(script, assignments.length);
    setScript(nextScript);
    setSceneImageAssignments(assignments);
    setStatus(`Built ${assignments.length} room-based scenes from ${images.length} images`);
  }

  async function submitRender() {
    if (isRendering) return;
    setError('');
    setLogs('');
    setVideoHref('');
    if (!canRender) {
      setError('Add images and script paragraphs. Every generated scene needs an image.');
      return;
    }

    try {
      setIsRendering(true);
      setJob(null);
      const willDelete = hasSavedProject && outputs.includes(outputName);
      setRenderTracker({
        ...EMPTY_RENDER_TRACKER,
        phase: 'saving',
        progress: renderTrackerProgress({ phase: 'saving' }),
        message: 'Saving the current project settings',
        willDelete,
      });
      await saveProject();

      setStatus('Naming rooms before render');
      setRenderTracker((previous) => ({
        ...previous,
        phase: 'naming',
        progress: renderTrackerProgress({ phase: 'naming' }),
        message: 'Classifying images and localizing rolling titles',
      }));
      const roomNaming = await request(`/v1/projects/${encodeURIComponent(resolvedProjectId)}/room-name-project`, {
        method: 'POST',
      });
      applyRoomNamingResults(roomNaming.results || []);

      if (willDelete) {
        setStatus(`Room names verified; deleting previous ${outputName}`);
        setRenderTracker((previous) => ({
          ...previous,
          phase: 'deleting',
          progress: renderTrackerProgress({ phase: 'deleting' }),
          message: `Deleting previous ${outputName}`,
        }));
        await request(`/v1/projects/${encodeURIComponent(resolvedProjectId)}/outputs/video?filename=${encodeURIComponent(outputName)}`, {
          method: 'DELETE',
        });
        setOutputs((current) => current.filter((filename) => filename !== outputName));
      }

      setStatus('Queueing verified render');
      setRenderTracker((previous) => ({
        ...previous,
        phase: 'queueing',
        progress: renderTrackerProgress({ phase: 'queueing' }),
        message: 'Queueing the verified project for rendering',
      }));
      const renderResponse = await request(`/v1/projects/${encodeURIComponent(resolvedProjectId)}/render`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          outputName,
          renderOptions: {
            ...DEFAULT_RENDER_OPTIONS,
            ...renderOptions,
            tts: voice || null,
            ttsLanguage: language || null,
            ttsApi,
            hyperframes: selectedHyperframes,
            introEnabled: useIntro,
            introTitle: resolvedIntroTitle,
            introLeaderImage: leaderImage ? leaderImage.name : null,
            introDuration: 1,
            thumbnailEnabled: true,
            logoEnabled: useLogo,
            logoImage: logoImage ? logoImage.name : logoChoice,
            logoCorner,
            logoMargin: Number(logoMargin) || 0,
          },
        }),
      });

      applyRoomNamingResults(renderResponse.roomNaming?.results || []);

      setRenderTracker((previous) => ({
        ...previous,
        phase: 'rendering',
        progress: renderTrackerProgress({ phase: 'rendering', jobProgress: 0 }),
        message: 'Waiting for the render worker',
        stage: 'QUEUED',
        jobId: renderResponse.jobId,
      }));

      const finishedJob = await pollJob(renderResponse.jobId);
      setStatus(`Completed ${finishedJob.jobId}`);
      setVideoHref(apiUrl(apiBase, `/v1/projects/${encodeURIComponent(resolvedProjectId)}/outputs/video?filename=${encodeURIComponent(outputName)}`));
      await refreshOutputs();
    } catch (err) {
      setStatus('Failed');
      setError(err.message || String(err));
      setRenderTracker((previous) => ({
        ...previous,
        phase: 'failed',
        failedFrom: previous.phase,
        message: err.message || String(err),
      }));
    } finally {
      setIsRendering(false);
    }
  }

  function formatProjectTime(value) {
    if (!value) return 'Never saved';
    return new Date(value * 1000).toLocaleString();
  }

  if (view === 'bible') {
    return <BibleStudio authToken={authToken} theme={theme} initialProjectId={bibleProjectId} onBack={() => setView('projects')} onOpenProjects={() => setView('projects')} />;
  }

  if (view === 'voice-samples') {
    return <VoiceSamples apiBase={apiBase} authToken={authToken} theme={theme} onOpenProjects={() => setView('projects')} />;
  }

  if (view === 'projects') {
    return (
      <main className="studio-shell" data-theme={theme}>
        <section className="studio-header">
          <div>
            <h1>MediaStudio Projects</h1>
            <p>Open a saved video project, update the script and assets, then re-render from persisted server storage.</p>
          </div>
          <div className="header-actions">
            <button className="primary-action" type="button" onClick={() => { setBibleProjectId(''); setView('bible'); }}>Bible Video Studio</button>
            <button className="theme-toggle" type="button" onClick={() => setView('voice-samples')}>Voice samples</button>
            <button className="theme-toggle" type="button" onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}>
              {theme === 'dark' ? 'Light mode' : 'Dark mode'}
            </button>
            <button className="theme-toggle" type="button" onClick={resetEditor}>New Project</button>
            <div className="status-panel">
              <span>Status</span>
              <strong>{status}</strong>
            </div>
          </div>
        </section>

        <section className="project-list-shell">
          <div className="project-list-toolbar">
            <div>
              <h2>Saved Projects</h2>
              <p>{filteredProjects.length} of {savedProjects.length} project{savedProjects.length === 1 ? '' : 's'} on this server</p>
            </div>
            <div className="project-list-controls">
              <label className="project-filter">
                <span className="sr-only">Filter projects</span>
                <input
                  type="search"
                  value={projectFilter}
                  onChange={(event) => setProjectFilter(event.target.value)}
                  placeholder="Filter projects"
                />
              </label>
              {projectFilter && (
                <button className="secondary-action" type="button" onClick={() => setProjectFilter('')}>Clear</button>
              )}
              <button className="secondary-action" type="button" onClick={refreshProjects} disabled={isLoadingProjects}>
                {isLoadingProjects ? 'Refreshing' : 'Refresh'}
              </button>
              <button
                className="primary-action"
                type="button"
                onClick={runNewestTenListingPipeline}
                disabled={!newestTenProjects.length || listingPipelineRun?.status === 'RUNNING' || listingPipelineRun?.status === 'QUEUED'}
              >
                {listingPipelineRun?.status === 'RUNNING' || listingPipelineRun?.status === 'QUEUED'
                  ? `Preparing ${listingPipelineRun.results.length}/${listingPipelineRun.projectIds.length}`
                  : `Prepare newest ${newestTenProjects.length}`}
              </button>
            </div>
          </div>
          {listingPipelineRun && (
            <p className="hint">
              Listing pipeline {listingPipelineRun.status.toLowerCase()}: {listingPipelineRun.results.length}/{listingPipelineRun.projectIds.length} complete.
              {listingPipelineRun.currentProjectId ? ` Working on ${listingPipelineRun.currentProjectId}.` : ''}
            </p>
          )}
          {error && <div className="error-box">{error}</div>}
          {savedProjects.length === 0 ? (
            <div className="empty-project-list">
              <h3>No saved projects yet</h3>
              <p>Create a project, add images and script text, then Save Project or Start Render.</p>
              <button className="primary-action" type="button" onClick={resetEditor}>Create Project</button>
            </div>
          ) : filteredProjects.length === 0 ? (
            <div className="empty-project-list">
              <h3>No matching projects</h3>
              <p>Try a title or project ID, such as “LeCrown”.</p>
              <button className="secondary-action" type="button" onClick={() => setProjectFilter('')}>Clear filter</button>
            </div>
          ) : (
            <div className="project-card-grid">
              {filteredProjects.map((project) => (
                <article className="project-card" key={project.projectId}>
                  <div>
                    <h3>{project.propertyAddress || project.title || project.projectId}</h3>
                    {project.propertyAddress && project.title && <p className="project-video-title">Video: {project.title}</p>}
                    <p>{project.projectId}</p>
                  </div>
                  <dl>
                    <div><dt>Updated</dt><dd>{formatProjectTime(project.updatedAt)}</dd></div>
                    <div><dt>Images</dt><dd>{project.imageCount || 0}</dd></div>
                    <div><dt>Latest output</dt><dd>{project.latestOutput || 'None'}</dd></div>
                  </dl>
                  <div className="project-card-actions">
                    <button className="primary-action" type="button" onClick={() => loadProject(project.projectId)} disabled={isLoadingProject}>
                      {isLoadingProject ? 'Opening' : 'Open'}
                    </button>
                    {project.latestOutput && (
                      <a href={apiUrl(apiBase, `/v1/projects/${encodeURIComponent(project.projectId)}/outputs/video?filename=${encodeURIComponent(project.latestOutput)}`)} download={project.latestOutput}>
                        Download
                      </a>
                    )}
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>

        <section className="project-list-shell sound-library-shell">
          <div className="project-list-toolbar">
            <div>
              <div className="sound-library-heading">
                <h2>No-attribution sound library</h2>
                <span className="sound-policy-badge">YouTube + TikTok ready</span>
              </div>
              <p>{soundCatalog.count || 0} approved sound effect{soundCatalog.count === 1 ? '' : 's'} available from Pixabay and Mixkit</p>
            </div>
            <button className="secondary-action" type="button" onClick={refreshSoundCatalog} disabled={isLoadingSoundCatalog}>
              {isLoadingSoundCatalog ? 'Refreshing' : 'Refresh'}
            </button>
          </div>
          {soundCatalog.status === 'unavailable' ? (
            <div className="error-box">Sound catalog unavailable: {soundCatalog.error}</div>
          ) : soundCatalog.assets?.length ? (
            <div className="sound-card-grid">
              {soundCatalog.assets.map((asset) => (
                <article className="sound-card" key={asset.id}>
                  <div>
                    <h3>{asset.title}</h3>
                    <p>{asset.providerLabel}</p>
                  </div>
                  <div className="sound-tags">
                    {(asset.tags || []).map((tag) => <span key={tag}>{tag}</span>)}
                  </div>
                  <dl>
                    <div><dt>File</dt><dd>{asset.format?.toUpperCase() || 'Audio'} · {formatBytes(asset.bytes)}</dd></div>
                    <div><dt>Usage</dt><dd>No attribution · Commercial and social</dd></div>
                    <div><dt>Status</dt><dd>{asset.available ? 'Ready on Sextant' : 'Cataloged; file unavailable'}</dd></div>
                  </dl>
                  <div className="sound-card-links">
                    {asset.sourceUrl && <a href={asset.sourceUrl} target="_blank" rel="noreferrer">Source</a>}
                    {asset.licenseUrl && <a href={asset.licenseUrl} target="_blank" rel="noreferrer">License</a>}
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <div className="empty-project-list">
              <h3>No approved sounds imported yet</h3>
              <p>Use the MediaStudio SFX MCP to import individual Pixabay or Mixkit sounds. Only no-attribution assets appear here.</p>
            </div>
          )}
        </section>
      </main>
    );
  }

  return (
    <main className="studio-shell" data-theme={theme}>
      <section className="studio-header">
        <div>
          <h1>MediaStudio Render Desk</h1>
          <p>Upload stills, paste a script, generate scene JSON, and send the job to the Render API worker.</p>
        </div>
        <div className="header-actions">
          <button className="primary-action" type="button" onClick={() => { setBibleProjectId(''); setView('bible'); }}>Bible Studio</button>
          <button className="theme-toggle" type="button" onClick={() => setView('voice-samples')}>Voice samples</button>
          <button className="theme-toggle" type="button" onClick={() => { setView('projects'); refreshProjects().catch(() => {}); }}>
            Projects
          </button>
          <button className="theme-toggle" type="button" onClick={resetEditor}>
            New Project
          </button>
          <button className="theme-toggle" type="button" onClick={saveProject} disabled={isSavingProject}>
            {isSavingProject ? 'Saving' : (hasSavedProject ? 'Update Project' : 'Save Project')}
          </button>
          <button className="theme-toggle" type="button" onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}>
            {theme === 'dark' ? 'Light mode' : 'Dark mode'}
          </button>
          <div className="status-panel">
            <span>Status</span>
            <strong>{status}</strong>
          </div>
        </div>
      </section>

      <section className="workspace-grid">
        <aside className="settings-panel">
          <h2>Connection</h2>
          <label>Render API<input value={apiBase} onChange={(event) => setApiBase(event.target.value)} /></label>
          <label>Bearer token<input value={authToken} onChange={(event) => setAuthToken(event.target.value)} placeholder="optional for local development" type="password" /></label>

          <h2>Project</h2>
          <label>Property address<input value={propertyAddress} onChange={(event) => setPropertyAddress(event.target.value)} placeholder="Street address, city, state ZIP" /></label>
          <label>Video title<input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Editable video title" /></label>
          {recognizedScriptTitle.title && <p className="hint">Title detected: <strong>{recognizedTitleCard.mainTitle}</strong>. Each segment after <strong>|</strong> becomes a title-card line and is excluded from narration.</p>}
          <label>Project ID<input value={projectId} onChange={(event) => setProjectId(event.target.value)} placeholder={resolvedProjectId} /></label>
          <label>Output file<input value={outputName} onChange={(event) => setOutputName(event.target.value)} /></label>
          <div className="project-save-meta">
            <span>Server project</span>
            <strong>{resolvedProjectId}</strong>
            <small>{hasSavedProject ? `Last saved ${lastSavedAt}` : 'Not saved yet'}</small>
          </div>

          <h2>Narration</h2>
          <label>Narration route<select value={ttsApi} onChange={(event) => setTtsApi(event.target.value)}>
            {selectableVoiceRoutes.length
              ? selectableVoiceRoutes.map((route) => <option key={route.value} value={route.value}>{route.label}</option>)
              : <><option value="voice-gateway">Fortress Voice Gateway</option><option value="flite">Built-in fallback voice</option><option value="none">None / timed silence</option></>}
          </select></label>
          <label>Voice<select value={voice} onChange={(event) => setVoice(event.target.value)}>
            {voiceProviders.length
              ? voiceProviders.map((provider) => (
                <optgroup key={provider.id} label={`${provider.label}${provider.selectable ? '' : ' · provider routing pending'}`}>
                  {(provider.voices || []).map((voiceName) => (
                    <option
                      key={`${provider.id}-${voiceName}`}
                      value={voiceName}
                      disabled={!provider.selectable || provider.ttsApi !== ttsApi}
                    >
                      {voiceName}
                    </option>
                  ))}
                </optgroup>
              ))
              : <option value={voice}>{voice}</option>}
          </select></label>
          {activeVoiceProvider && <span className="voice-source-note">Source: {activeVoiceProvider.label}</span>}
          <label>Language<input value={language} onChange={(event) => { setLanguage(event.target.value); setYoutubeProfile(youtubeProfileForLanguage(event.target.value)); }} /></label>

          <div className="settings-section-heading">
            <h2>Intro</h2>
            <button type="button" onClick={generateLeadCard} disabled={isGeneratingIntro || (!title.trim() && !script.trim())}>
              {isGeneratingIntro ? 'Generating' : 'Generate with Ollama'}
            </button>
          </div>
          <div className="hyperframes-binding">
            <strong>Opening and transition style</strong>
            <label>Opening card
              <select value={leadCardMode} onChange={(event) => setLeadCardMode(event.target.value)}>
                <option value="traditional">Traditional card</option>
                <option value="hyperframes">HyperFrames animated card</option>
              </select>
            </label>
            <label>Scene transitions
              <select value={sceneTransitionMode} onChange={(event) => setSceneTransitionMode(event.target.value)}>
                <option value="traditional">Traditional cut</option>
                <option value="hyperframes">HyperFrames animated transition</option>
              </select>
            </label>
            {leadCardMode === 'hyperframes' && (
              <label>HyperFrames opening composition
                <select value={hyperframesLeadCard} onChange={(event) => setHyperframesLeadCard(event.target.value)}>
                  <option value="lecrown-lead-card">lecrown-lead-card</option>
                </select>
              </label>
            )}
            {sceneTransitionMode === 'hyperframes' && (
              <label>HyperFrames transition composition
                <select value={hyperframesSceneTransition} onChange={(event) => setHyperframesSceneTransition(event.target.value)}>
                  <option value="lecrown-scene-transition">lecrown-scene-transition</option>
                </select>
              </label>
            )}
            <p>The selected style is saved with the project and applied to the rendered video.</p>
            {leadCardMode === 'hyperframes' && <a className="secondary-action hyperframes-launch" href={hyperframesEditUrl(hyperframesLeadCard)} target="_blank" rel="noreferrer">Edit animated opening</a>}
          </div>
          <label className="check-row"><input type="checkbox" checked={useIntro} onChange={(event) => setUseIntro(event.target.checked)} />Use 1 second leader card</label>
          <label>Lead card line 1<input value={introLines[0] || ''} onChange={(event) => setIntroLines((current) => updateLeadLineValue(current, 0, event.target.value))} placeholder="Primary title line" /></label>
          <label>Lead card line 2<input value={introLines[1] || ''} onChange={(event) => setIntroLines((current) => updateLeadLineValue(current, 1, event.target.value))} placeholder="Subtitle or property detail" /></label>
          <label>Lead card line 3<input value={introLines[2] || ''} onChange={(event) => setIntroLines((current) => updateLeadLineValue(current, 2, event.target.value))} placeholder="Location, offer, or callout" /></label>
          <label>Lead card line 4<input value={introLines[3] || ''} onChange={(event) => setIntroLines((current) => updateLeadLineValue(current, 3, event.target.value))} placeholder="Optional additional line" /></label>
          <label>Lead card line 5<input value={introLines[4] || ''} onChange={(event) => setIntroLines((current) => updateLeadLineValue(current, 4, event.target.value))} placeholder="Optional additional line" /></label>
          <label>Lead card line 6<input value={introLines[5] || ''} onChange={(event) => setIntroLines((current) => updateLeadLineValue(current, 5, event.target.value))} placeholder="Optional additional line" /></label>
          <div className="compact-upload">
            <input id="leader-upload" type="file" accept="image/*" onChange={(event) => handleLeaderSelection(event.target.files)} />
            <label htmlFor="leader-upload">Leader Image</label>
            <span>{leaderImage ? leaderImage.name : 'Default leader.png'}</span>
          </div>

          <h2>Logo</h2>
          <label className="check-row"><input type="checkbox" checked={useLogo} onChange={(event) => setUseLogo(event.target.checked)} />Overlay logo</label>
          <label>Brand image<select value={logoChoice} onChange={(event) => setLogoChoice(event.target.value)} disabled={!!logoImage}>{brandAssets.length ? brandAssets.map((asset) => <option key={asset.name} value={asset.name}>{asset.name}</option>) : <option value="logophone.png">logophone.png</option>}</select></label>
          <div className="compact-upload">
            <input id="logo-upload" type="file" accept="image/*" onChange={(event) => handleLogoSelection(event.target.files)} />
            <label htmlFor="logo-upload">Upload Logo</label>
            <span>{logoImage ? logoImage.name : 'Using selected brand image'}</span>
            {logoImage && <button type="button" onClick={() => setLogoImage(null)}>Use selected brand image</button>}
          </div>
          <label>Corner<select value={logoCorner} onChange={(event) => setLogoCorner(event.target.value)}><option value="top-right">Top right</option><option value="top-left">Top left</option><option value="bottom-right">Bottom right</option><option value="bottom-left">Bottom left</option></select></label>
          <label>Margin<input min="0" step="4" type="number" value={logoMargin} onChange={(event) => setLogoMargin(event.target.value)} /></label>
        </aside>

        <section className="editor-panel">
          <div className="panel-heading"><h2>Script</h2><span>{scenesPayload.scenes.length} scene{scenesPayload.scenes.length === 1 ? '' : 's'}</span></div>
          <textarea value={script} onChange={(event) => setScript(event.target.value)} spellCheck="true" />
          <div className="script-actions">
            <label>Spoken length
              <span className="inline-input">
                <input
                  min="5"
                  step="5"
                  type="number"
                  value={targetSeconds}
                  onChange={(event) => setTargetSeconds(event.target.value)}
                />
                <span>seconds</span>
              </span>
            </label>
            <button className="secondary-action" type="button" onClick={enhanceScript} disabled={!canEnhance}>
              {isEnhancing ? 'Enhancing' : 'Enhance'}
            </button>
            <button className="secondary-action" type="button" onClick={() => enhanceScript({ withRoomInfo: true })} disabled={!canEnhance || !hasRoomInfo}>
              {isEnhancing ? 'Enhancing' : 'Enhance with room info'}
            </button>
            <button className="secondary-action" type="button" onClick={splitScriptForImages} disabled={!script.trim() || images.length < 1 || isEnhancing}>
              {isEnhancing ? 'Rewriting image scenes' : 'Rewrite & split into image scenes'}
            </button>
            <button className="secondary-action" type="button" onClick={buildScenesFromRoomLabels} disabled={!script.trim() || images.length < 1}>
              Build scenes from room labels
            </button>
          </div>
          <p className="hint">Build scenes from room labels groups Room Renamer results into reviewable 1–3-photo scenes and splits the narration to match.</p>
          {unassignedImageCount > 0 && (
            <p className="hint warning-hint">
              {unassignedImageCount} image{unassignedImageCount === 1 ? ' is' : 's are'} still in the loading zone. Add them to scene sections before rendering.
            </p>
          )}
          {scriptSections.length > 0 && (
            <div className="script-section-panel">
              <div className="output-heading compact-heading">
                <h3>Script Scene Sections</h3>
                <span>{scriptSections.length} section{scriptSections.length === 1 ? '' : 's'}</span>
              </div>
              <div className="script-section-list">
                {scriptSections.map((section, index) => {
                  const scene = scenesPayload.scenes[index];
                  const nextScene = scenesPayload.scenes[index + 1];
                  const assignedImages = scene?.images || [];
                  const nextSceneImages = nextScene?.images || [];
                  const transitionVariables = {
                    outgoing_image: imagePreviewByName.get(assignedImages.at(-1))?.url || '',
                    incoming_image: imagePreviewByName.get(nextSceneImages[0])?.url || '',
                    outgoing_title: scene?.title || `Scene ${index + 1}`,
                    incoming_title: nextScene?.title || '',
                    transition_title: hyperframesTransitionTitles[index] || nextScene?.title || `Scene ${index + 2}`,
                  };
                  return (
                    <div className="script-section-card" key={`${index}-${assignedImages.join('-')}`}>
                      <div className="script-section-meta">
                        <span>
                          Scene {index + 1}
                          {assignedImages.length ? ` - ${assignedImages.join(', ')}` : ''}
                        </span>
                        <span className="scene-card-controls">
                          {nextScene && sceneTransitionMode === 'hyperframes' && (
                            <button
                              type="button"
                              className="secondary-action hyperframes-launch"
                              onClick={() => openGeneratedHyperframesTransition(index, scene, nextScene, transitionVariables)}
                              disabled={generatingTransitionIndex !== null}
                            >
                              {generatingTransitionIndex === index ? 'Generating Ollama title…' : `Generate & edit transition to scene ${index + 2}`}
                            </button>
                          )}
                          <span className="scene-duration-control">
                            <span>Min length</span>
                            <input
                              aria-label={`Scene ${index + 1} length in seconds`}
                              min="1"
                              step="1"
                              type="number"
                              value={sceneDurations[index] || ''}
                              onChange={(event) => setSceneDurations((current) => updateIndexedValue(current, index, event.target.value))}
                              placeholder="Auto"
                            />
                            <span>sec</span>
                          </span>
                        </span>
                      </div>
                      {nextScene && sceneTransitionMode === 'hyperframes' && (
                        <label className="hyperframes-transition-title">
                          <span>HyperFrames transition title</span>
                          <input
                            value={transitionVariables.transition_title}
                            onChange={(event) => setHyperframesTransitionTitles((current) => ({ ...current, [index]: event.target.value }))}
                            placeholder={`Transition to scene ${index + 2}`}
                          />
                        </label>
                      )}
                      <div className="scene-image-assignment-row">
                        <div className="scene-image-preview-list">
                          {assignedImages.length ? assignedImages.map((imageName) => {
                            const preview = imagePreviewByName.get(imageName);
                            return (
                              <div className="scene-image-preview-card" key={imageName}>
                                <button
                                  className="scene-image-thumbnail"
                                  type="button"
                                  onClick={() => setSelectedImageName(imageName)}
                                  aria-label={`Open ${imageName} preview for scene ${index + 1}`}
                                >
                                  <img src={preview?.url || missingImageDataUrl(imageName)} alt="" draggable="false" />
                                  <span>Expand</span>
                                </button>
                                <div className="scene-image-preview-meta">
                                  <span title={imageName}>{imageName}</span>
                                  <button type="button" aria-label={`Remove ${imageName} from scene ${index + 1}`} onClick={() => removeImageFromScene(index, imageName)}>x</button>
                                </div>
                              </div>
                            );
                          }) : <span className="scene-empty-note">No pictures assigned</span>}
                        </div>
                        <select
                          aria-label={`Add picture to scene ${index + 1}`}
                          disabled={assignedImages.length >= 3 || unassignedImagePreviews.length === 0}
                          value=""
                          onChange={(event) => {
                            addImageToScene(index, event.target.value);
                            event.target.value = '';
                          }}
                        >
                          <option value="">{assignedImages.length >= 3 ? 'Scene full' : 'Add picture'}</option>
                          {unassignedImagePreviews.map((preview) => (
                            <option key={preview.name} value={preview.name}>{preview.name}</option>
                          ))}
                        </select>
                      </div>
                      <textarea
                        className="script-section-textarea"
                        value={section}
                        onChange={(event) => setScript(replaceScriptSection(scriptSections.join('\n\n'), index, event.target.value))}
                      />
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          <div className="upload-box">
            <input id="image-upload" type="file" accept="image/*" multiple onChange={(event) => handleImageSelection(event.target.files)} />
            <label htmlFor="image-upload">Add Images</label>
            <span>{images.length ? `${unassignedImageCount} in loading zone / ${images.length} uploaded` : 'No images selected'}</span>
          </div>

          {imagePreviews.length > 0 && (
            <>
            <div className="room-tools">
              <button className="secondary-action" type="button" onClick={saveProject} disabled={isSavingProject}>
                {isSavingProject ? 'Saving project' : (hasSavedProject ? 'Update Project' : 'Save Project')}
              </button>
              <button className="secondary-action" type="button" onClick={classifyRooms} disabled={isClassifyingRooms || !images.length}>
                {isClassifyingRooms ? 'Naming rooms' : 'Auto-name rooms'}
              </button>
              <button className="secondary-action" type="button" onClick={() => saveRoomAnnotations().then(() => setStatus('Room info saved')).catch((err) => setError(err.message || String(err)))} disabled={!images.length}>
                Save room info
              </button>
            </div>
            {unassignedImagePreviews.length > 0 ? <>
              <div className="loading-zone-heading">
                <h3>Image Loading Zone</h3>
                <span>{unassignedImageCount} unassigned image{unassignedImageCount === 1 ? '' : 's'}</span>
              </div>
              <div className="image-order-list">
              {unassignedImagePreviews.map((preview, loadingIndex) => {
                const index = imagePreviews.findIndex((item) => item.name === preview.name);
                return (
                <Fragment key={`${preview.name}-${index}`}>
                  <div
                    className="image-drop-slot"
                    onDragOver={(event) => {
                      event.preventDefault();
                      event.dataTransfer.dropEffect = 'move';
                    }}
                    onDrop={(event) => {
                      event.preventDefault();
                      const sourceName = event.dataTransfer.getData('text/plain') || draggedImageName;
                      moveImageToPosition(sourceName, index);
                      setDraggedImageName('');
                    }}
                  >
                    <span>{loadingIndex === 0 ? 'Drop here for beginning' : 'Drop here'}</span>
                  </div>
                  <div
                    className={`image-header-item ${draggedImageName === preview.name ? 'is-dragging' : ''}`}
                  >
                    <div className="image-card-top">
                      <span
                        className="drag-handle"
                        draggable
                        aria-label={`Drag ${preview.name}`}
                        role="button"
                        title="Drag to reorder"
                        onDragStart={(event) => {
                          setDraggedImageName(preview.name);
                          event.dataTransfer.effectAllowed = 'move';
                          event.dataTransfer.setData('text/plain', preview.name);
                        }}
                        onDragEnd={() => setDraggedImageName('')}
                      >
                        ::
                      </span>
                      <span className="image-order-number">{index + 1}</span>
                      <span className="image-file-name" title={preview.name}>{preview.name}</span>
                      <span className="image-assignment-badge">Loading zone</span>
                      {preview.missing && <span className="missing-file-badge">Missing file</span>}
                      <button type="button" aria-label={`Remove ${preview.name}`} onClick={() => removeImage(preview.name)}>
                        x
                      </button>
                    </div>
                    <div className="image-card-body">
                      <button
                        className="image-preview-button"
                        type="button"
                        onClick={() => setSelectedImageName(preview.name)}
                        aria-label={`Open ${preview.name} preview`}
                      >
                        <img src={preview.url} alt="" draggable="false" />
                        <span>Expand</span>
                      </button>
                      <div className="image-card-fields">
                        <label className="check-row"><input type="checkbox" checked={Boolean(imageLabelEnabled[preview.name])} onChange={(event) => setImageLabelVisibility(preview.name, event.target.checked)} />Show label in video</label>
                        <label>Header
                          <input
                            value={imageHeaders[preview.name] || ''}
                            onChange={(event) => updateImageHeader(preview.name, event.target.value)}
                            placeholder="Text shown at top of this image"
                          />
                        </label>
                        <label>Room info
                          <textarea
                            className="room-info-textarea"
                            value={imageRoomInfo[preview.name]?.roomDescription || ''}
                            onChange={(event) => updateImageRoomInfo(preview.name, { roomDescription: event.target.value, source: 'manual' })}
                            placeholder="Kitchen with island, primary bath, lobby reception..."
                          />
                        </label>
                        <label>Correct room type
                          <input
                            value={imageRoomInfo[preview.name]?.label || ''}
                            onChange={(event) => updateImageRoomInfo(preview.name, { label: event.target.value, source: 'manual' })}
                            placeholder="kitchen, living_room, street_view..."
                          />
                        </label>
                        <button
                          type="button"
                          onClick={() => saveRoomCorrection(preview.name)}
                          disabled={!hasSavedProject || !imageRoomInfo[preview.name]?.label?.trim() || Boolean(correctingRoomName)}
                          title={hasSavedProject ? 'Save this correction as Room Renamer training data' : 'Save the project before submitting training data'}
                        >
                          {correctingRoomName === preview.name ? 'Saving correction' : 'Save correction & train'}
                        </button>
                        {imageRoomInfo[preview.name]?.confidence !== null && imageRoomInfo[preview.name]?.confidence !== undefined && (
                          <span>{imageRoomInfo[preview.name]?.source || 'room-renamer'} confidence {Number(imageRoomInfo[preview.name]?.confidence || 0).toFixed(2)}</span>
                        )}
                      </div>
                    </div>
                  </div>
                </Fragment>
                );
              })}
              <div
                className="image-drop-slot image-drop-slot-end"
                onDragOver={(event) => {
                  event.preventDefault();
                  event.dataTransfer.dropEffect = 'move';
                }}
                onDrop={(event) => {
                  event.preventDefault();
                  const sourceName = event.dataTransfer.getData('text/plain') || draggedImageName;
                  moveImageToPosition(sourceName, imagePreviews.length);
                  setDraggedImageName('');
                }}
              >
                <span>Drop at end</span>
              </div>
              </div>
            </> : <p className="all-images-assigned">All {imagePreviews.length} pictures are assigned and shown in their scene cards above.</p>}
            </>
          )}

          {removedImages.length > 0 && (
            <div className="removed-images-panel">
              <h3>Removed images</h3>
              <div className="removed-image-list">
                {removedImages.map((image) => (
                  <div className="removed-image-row" key={image.name}>
                    <span>{image.name}</span>
                    <button type="button" onClick={() => restoreRemovedImage(image.name)}>Restore</button>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="version-panel">
            <div className="output-heading">
              <h3>Version History</h3>
              <button type="button" onClick={refreshVersions}>Refresh</button>
            </div>
            {versions.length ? (
              <div className="version-list">
                {versions.slice(0, 8).map((version) => (
                  <div className="version-row" key={version.versionId}>
                    <div>
                      <strong>{versionLabel(version.label)}</strong>
                      <span>{new Date((version.createdAt || 0) * 1000).toLocaleString()}</span>
                      <small>{version.imageCount || 0} active / {version.removedImageCount || 0} removed</small>
                    </div>
                    <button type="button" onClick={() => restoreVersion(version.versionId)} disabled={isRestoringVersion}>
                      {isRestoringVersion ? 'Restoring' : 'Restore'}
                    </button>
                  </div>
                ))}
              </div>
            ) : (
              <p className="hint">Versions appear after the first project update.</p>
            )}
          </div>

          <button className="primary-action" type="button" onClick={submitRender} disabled={!canRender || isRendering}>
            {isRendering
              ? `${({ saving: 'Saving', deleting: 'Deleting', naming: 'Naming rooms', queueing: 'Queueing' }[renderTracker.phase] || 'Rendering')} ${Math.round(renderTracker.progress * 100)}%`
              : hasSavedProject ? 'Delete & Re-render Video' : 'Start Render'}
          </button>
          {hasSavedProject && (
            <p className="hint">Deletes the selected local MP4, then renders it again from the saved scenes and current settings. YouTube uploads, thumbnails, source images, and project history are not deleted. Change the output filename first to keep the previous MP4.</p>
          )}
          {renderTracker.phase !== 'idle' && (
            <div className={`render-progress-tracker render-progress-${renderTracker.phase}`} aria-live="polite">
              <div className="render-progress-heading">
                <div>
                  <span>{renderTracker.phase === 'complete' ? 'Render complete' : renderTracker.phase === 'failed' ? 'Render failed' : 'Render in progress'}</span>
                  <strong>{renderTracker.message}</strong>
                </div>
                <b>{Math.round(renderTracker.progress * 100)}%</b>
              </div>
              <progress aria-label="Video render progress" aria-valuetext={`${Math.round(renderTracker.progress * 100)} percent, ${renderTracker.message}`} value={renderTracker.progress} max="1" />
              <ol className="render-progress-steps">
                {['Save project', 'Name rooms', 'Delete old MP4', 'Queue job', 'Build video', 'Complete'].map((label, index) => {
                  const state = renderSteps[index];
                  return <li className={`render-step-${state}`} key={label}><span aria-hidden="true" />{label}{state === 'skipped' ? ' (not needed)' : ''}</li>;
                })}
              </ol>
              {renderTracker.jobId && <small>Job {renderTracker.jobId}{renderTracker.stage ? ` · ${renderTracker.stage}` : ''}</small>}
            </div>
          )}
          {error && <div className="error-box">{error}</div>}
          {(previewVideoHref || outputs.length > 0) && (
            <div className="output-panel">
              {previewVideoHref && (
                <div className="video-preview-frame">
                  <video src={previewVideoHref} controls preload="metadata" poster={previewPosterHref || undefined} />
                </div>
              )}
              <div className="output-heading">
                <h3>Downloads</h3>
                <button type="button" onClick={refreshOutputs}>Refresh</button>
              </div>
              <div className="download-list">
                {(outputs.length ? outputs : [outputName]).map((filename) => (
                  <a key={filename} href={outputHref(filename)} download={filename}>{filename}</a>
                ))}
              </div>
              <div className="youtube-panel">
                <div className="output-heading">
                  <h3>YouTube</h3>
                  <button type="button" onClick={refreshYoutubeAuth}>Check auth</button>
                </div>
                <p>{youtubeAuth?.authenticated ? 'Connected on server' : 'Not connected on server'}</p>
                <YouTubeChannelSelector channels={youtubeChannels} value={youtubeProfile} onChange={setYoutubeProfile} disabled={isConnectingYoutube || isUploadingYoutube} />
                <div className="youtube-actions">
                  <button type="button" onClick={connectYoutube} disabled={isConnectingYoutube}>
                    {isConnectingYoutube ? 'Connecting' : 'Connect YouTube'}
                  </button>
                  <button type="button" onClick={connectYoutubeInExternalBrowser} disabled={isConnectingYoutube}>
                    Connect in external browser
                  </button>
                  <button type="button" onClick={uploadYoutube} disabled={isUploadingYoutube || !youtubeAuth?.authenticated}>
                    {isUploadingYoutube ? 'Uploading' : 'Upload to YouTube'}
                  </button>
                  <button type="button" onClick={applyYoutubeThumbnail} disabled={isApplyingYoutubeThumbnail || !youtubeAuth?.authenticated || !youtubeVideoId.trim() || !previewPosterName}>
                    {isApplyingYoutubeThumbnail ? 'Applying thumbnail' : 'Apply thumbnail'}
                  </button>
                  <button type="button" onClick={updateYoutubeDetails} disabled={isUpdatingYoutubeDetails || !youtubeAuth?.authenticated || !youtubeVideoId.trim()}>
                    {isUpdatingYoutubeDetails ? 'Updating details' : 'Update YouTube details'}
                  </button>
                </div>
                {youtubeAuth?.authUrl && !youtubeAuth?.authenticated && (
                  <div className="youtube-auth-link">
                    <p>Use your external browser profile if the in-app Google tab cannot sign in.</p>
                    <button type="button" onClick={() => copyYoutubeAuthorizationLink().then(() => setStatus('YouTube authorization link copied for your external browser')).catch((err) => setError(`Could not copy the authorization link: ${err.message || String(err)}`))}>Copy authorization link</button>
                  </div>
                )}
                {youtubeAuth?.manualCallback && !youtubeAuth?.authenticated && (
                  <div className="manual-auth-panel">
                    <p>After Google redirects to localhost, copy the full callback URL from that tab and paste it here.</p>
                    <textarea
                      className="short-textarea"
                      value={youtubeCallbackUrl}
                      onChange={(event) => setYoutubeCallbackUrl(event.target.value)}
                      placeholder="http://localhost/?state=...&code=..."
                    />
                    <button type="button" onClick={completeYoutubeAuth} disabled={!youtubeCallbackUrl.trim()}>
                      Save YouTube auth
                    </button>
                  </div>
                )}
                {youtubeAuth?.authenticated && youtubeAuth?.metadataAuthorized === false && (
                  <p>Reconnect this channel once to update details on existing YouTube videos.</p>
                )}
                <label>Title<input value={youtubeTitle} onChange={(event) => setYoutubeTitle(event.target.value)} placeholder={title || outputName} /></label>
                <div className="output-heading compact-heading">
                  <h4>Description</h4>
                  <span className="button-row">
                    <button type="button" onClick={() => setYoutubeDescription(originalScriptText || script)} disabled={!(originalScriptText || script).trim()}>
                      Use original text
                    </button>
                    <button type="button" onClick={writeYoutubeDescription} disabled={isWritingYoutubeDescription || (!(originalScriptText || script).trim() && !youtubeDescription.trim())}>
                      {isWritingYoutubeDescription ? 'Writing' : 'Enhance with Ollama'}
                    </button>
                  </span>
                </div>
                <p className="hint">Starts with the original intake text. Edit it directly, or enhance that source with Ollama.</p>
                <label>Description<textarea className="short-textarea youtube-description-textarea" value={youtubeDescription} onChange={(event) => setYoutubeDescription(event.target.value)} /></label>
                <label>Tags<input value={youtubeTags} onChange={(event) => setYoutubeTags(event.target.value)} placeholder="comma, separated, tags" /></label>
                <label>Privacy<select value={youtubePrivacy} onChange={(event) => setYoutubePrivacy(event.target.value)}><option value="private">Private</option><option value="unlisted">Unlisted</option><option value="public">Public</option></select></label>
                <label>YouTube video ID<input value={youtubeVideoId} onChange={(event) => { setYoutubeVideoId(event.target.value.trim()); setYoutubeThumbnailApplied(false); }} placeholder="Filled automatically after upload" /></label>
                {youtubeThumbnailApplied && <p className="success-text">Thumbnail applied{youtubeThumbnailFilename ? `: ${youtubeThumbnailFilename}` : ''}</p>}
                {!youtubeThumbnailApplied && youtubeThumbnailError && <p>Thumbnail pending: {youtubeThumbnailError}</p>}
                {!previewPosterName && <p>Generate or refresh a thumbnail before applying it.</p>}
                {youtubeResult && <a href={youtubeResult} target="_blank" rel="noreferrer">{youtubeResult}</a>}
              </div>
            </div>
          )}
        </section>

        <section className="preview-panel">
          <div className="panel-heading"><h2>Generated Scenes</h2><span>{resolvedProjectId}</span></div>
          <pre>{JSON.stringify(scenesPayload, null, 2)}</pre>
          {(job || logs) && <div className="logs-panel"><h3>Logs</h3><pre>{logs || 'Waiting for render logs...'}</pre></div>}
          <div className="roadmap-panel">
            <h3>Roadmap</h3>
            <h4>Cleanup and Bugs</h4>
            <ul>
              <li>Support multiline text in the title section and generated thumbnail instead of a single line.</li>
            </ul>
            <h4>Active</h4>
            <ul>
              <li>Room training CSV from saved per-image labels.</li>
              <li>One-click room naming through the room_renamer service.</li>
              <li>Script enhancement grounded in room notes.</li>
              <li>YouTube publish metadata reuse from project fields.</li>
            </ul>
          </div>
        </section>
      </section>
      {selectedImagePreview && (
        <div className="image-modal-backdrop" role="dialog" aria-modal="true" aria-label={`${selectedImagePreview.name} image editor`}>
          <div className="image-modal">
            <div className="image-modal-heading">
              <div>
                <h2>{selectedImagePreview.name}</h2>
                <p>Expanded preview and image fields</p>
              </div>
              <button type="button" onClick={() => setSelectedImageName('')} aria-label="Close image preview">x</button>
            </div>
            <div className="image-modal-body">
              <div className="image-modal-preview">
                <img src={selectedImagePreview.url} alt="" />
              </div>
              <div className="image-modal-fields">
                <label className="check-row"><input type="checkbox" checked={Boolean(imageLabelEnabled[selectedImagePreview.name])} onChange={(event) => setImageLabelVisibility(selectedImagePreview.name, event.target.checked)} />Show label in video</label>
                <label>Header
                  <input
                    value={imageHeaders[selectedImagePreview.name] || ''}
                    onChange={(event) => updateImageHeader(selectedImagePreview.name, event.target.value)}
                    placeholder="Text shown at top of this image"
                  />
                </label>
                <label>Room info
                  <textarea
                    className="room-info-textarea image-modal-textarea"
                    value={imageRoomInfo[selectedImagePreview.name]?.roomDescription || ''}
                    onChange={(event) => updateImageRoomInfo(selectedImagePreview.name, { roomDescription: event.target.value, source: 'manual' })}
                    placeholder="Kitchen with island, primary bath, lobby reception..."
                  />
                </label>
                <label>Correct room type
                  <input
                    value={imageRoomInfo[selectedImagePreview.name]?.label || ''}
                    onChange={(event) => updateImageRoomInfo(selectedImagePreview.name, { label: event.target.value, source: 'manual' })}
                    placeholder="kitchen, living_room, street_view..."
                  />
                </label>
                <div className="image-modal-actions">
                  <button
                    type="button"
                    onClick={() => saveRoomCorrection(selectedImagePreview.name)}
                    disabled={!hasSavedProject || !imageRoomInfo[selectedImagePreview.name]?.label?.trim() || Boolean(correctingRoomName)}
                    title={hasSavedProject ? 'Save this correction as Room Renamer training data' : 'Save the project before submitting training data'}
                  >
                    {correctingRoomName === selectedImagePreview.name ? 'Saving correction' : 'Save correction & train'}
                  </button>
                  <button type="button" onClick={() => removeImage(selectedImagePreview.name)}>Remove image</button>
                  <button type="button" onClick={() => setSelectedImageName('')}>Done</button>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </main>
  );
}
