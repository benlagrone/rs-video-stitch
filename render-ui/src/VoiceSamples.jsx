import { useEffect, useMemo, useState } from 'react';

function apiUrl(baseUrl, path) {
  return `${baseUrl.replace(/\/+$/, '')}${path}`;
}

async function responseError(response) {
  const body = await response.text();
  try {
    return JSON.parse(body).detail || body;
  } catch {
    return body || `Request failed (${response.status})`;
  }
}

const languageNames = {
  de: 'German',
  en: 'English',
  fr: 'French',
  in: 'Indian English',
  it: 'Italian',
  jp: 'Japanese',
  kr: 'Korean',
  nl: 'Dutch',
  pl: 'Polish',
  pt: 'Portuguese',
  sp: 'Spanish',
};

const fliteVoiceNames = {
  kal: { name: 'Kal', detail: 'US English · Male' },
  awb: { name: 'Alan', detail: 'Scottish English · Male' },
  rms: { name: 'Roger', detail: 'US English · Male' },
  slt: { name: 'Sally', detail: 'US English · Female' },
};

function voicePresentation(provider, voice) {
  if (provider.id === 'flite') {
    return fliteVoiceNames[voice] || { name: voice, detail: 'Built-in voice' };
  }

  const gatewayVoice = voice.match(/^([a-z]{2})-(.+)_(man|woman)$/i);
  if (gatewayVoice) {
    const [, languageCode, suppliedName, gender] = gatewayVoice;
    const name = /^Spk\d+$/i.test(suppliedName)
      ? `Voice ${Number(suppliedName.slice(3)) + 1}`
      : suppliedName;
    return {
      name,
      detail: `${languageNames[languageCode.toLowerCase()] || languageCode.toUpperCase()} · ${gender === 'woman' ? 'Female' : 'Male'}`,
    };
  }

  const azureVoice = voice.match(/^([a-z]{2})-([A-Z]{2})-([A-Za-z]+?)(Multilingual)?Neural$/);
  if (azureVoice) {
    const [, languageCode, region, suppliedName, multilingual] = azureVoice;
    return {
      name: suppliedName,
      detail: `${languageNames[languageCode] || languageCode.toUpperCase()} (${region}) · ${multilingual ? 'Multilingual ' : ''}Neural`,
    };
  }

  if (voice === 'Carter') return { name: 'Carter', detail: 'English · Male' };
  return { name: voice, detail: 'Voice details supplied by provider' };
}

function providerPresentation(provider) {
  if (provider.id === 'vibevoice') {
    return { label: 'VibeVoice', detail: 'GPU narration voices' };
  }
  if (provider.id === 'azure_voice') {
    return { label: 'Azure Speech', detail: 'Neural narration voices' };
  }
  if (provider.id === 'flite') {
    return { label: 'Built-in voices', detail: 'Local fallback voices' };
  }
  return { label: provider.label, detail: provider.detail || 'Available for narration' };
}

export function VoiceSamples({ apiBase, authToken, onOpenProjects, theme }) {
  const [providers, setProviders] = useState([]);
  const [text, setText] = useState('Welcome to MediaStudio. This is a narration voice sample.');
  const [audioUrl, setAudioUrl] = useState('');
  const [playingVoice, setPlayingVoice] = useState('');
  const [error, setError] = useState('');

  const selectableProviders = useMemo(
    () => providers.filter((provider) => provider.selectable && provider.ttsApi !== 'none'),
    [providers],
  );

  async function refreshCatalog() {
    setError('');
    try {
      const response = await fetch(apiUrl(apiBase, '/v1/voice-options'), {
        headers: authToken ? { Authorization: `Bearer ${authToken}` } : {},
      });
      if (!response.ok) throw new Error(await responseError(response));
      const catalog = await response.json();
      setProviders(catalog.providers || []);
    } catch (err) {
      setProviders([]);
      setError(`Voice catalog unavailable: ${err.message || String(err)}`);
    }
  }

  useEffect(() => {
    refreshCatalog();
  }, [apiBase, authToken]);

  useEffect(() => () => {
    if (audioUrl) URL.revokeObjectURL(audioUrl);
  }, [audioUrl]);

  async function sampleVoice(provider, voice) {
    if (!text.trim() || playingVoice) return;
    setError('');
    setPlayingVoice(`${provider.ttsApi}:${voice}`);
    try {
      const response = await fetch(apiUrl(apiBase, '/v1/voice-samples'), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
        },
        body: JSON.stringify({ text: text.trim(), voice, ttsApi: provider.ttsApi, language: 'en-US' }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const nextAudioUrl = URL.createObjectURL(await response.blob());
      setAudioUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return nextAudioUrl;
      });
    } catch (err) {
      setError(`Voice sample failed: ${err.message || String(err)}`);
    } finally {
      setPlayingVoice('');
    }
  }

  return (
    <main className="studio-shell voice-samples-shell" data-theme={theme}>
      <section className="studio-header">
        <div>
          <h1>Voice samples</h1>
          <p>Listen first, then choose a voice for your video.</p>
        </div>
        <div className="header-actions">
          <button className="theme-toggle" type="button" onClick={onOpenProjects}>Projects</button>
        </div>
      </section>

      <section className="voice-samples-content">
        <aside className="voice-sample-copy">
          <label htmlFor="voice-sample-text">Sample script</label>
          <textarea
            id="voice-sample-text"
            value={text}
            maxLength={500}
            onChange={(event) => setText(event.target.value)}
          />
          <div className="voice-sample-copy-meta">
            <span>{text.length}/500 characters</span>
            <button className="secondary-action" type="button" onClick={refreshCatalog}>Refresh voices</button>
          </div>
          <p>Previews are temporary. Nothing is saved to a project or render queue.</p>
          {audioUrl && <audio className="voice-sample-player" controls autoPlay src={audioUrl}>Your browser cannot play this audio sample.</audio>}
          {error && <div className="error-box">{error}</div>}
        </aside>

        <div className="voice-provider-list">
          {selectableProviders.map((provider) => (
            <section className="voice-provider" key={provider.id} data-provider={provider.id}>
              <header>
                <div>
                  <h2>{providerPresentation(provider).label}</h2>
                  <p>{providerPresentation(provider).detail}</p>
                </div>
                <span>{provider.voices.length} available</span>
              </header>
              <div className="voice-card-grid">
                {provider.voices.map((voice) => {
                  const voiceKey = `${provider.ttsApi}:${voice}`;
                  const presentation = voicePresentation(provider, voice);
                  return (
                    <article className="voice-card" key={voiceKey}>
                      <div className="voice-card-copy">
                        <strong>{presentation.name}</strong>
                        <span>{presentation.detail}</span>
                        <small>Provider ID: {voice}</small>
                      </div>
                      <button
                        className="voice-play-button"
                        type="button"
                        onClick={() => sampleVoice(provider, voice)}
                        disabled={!text.trim() || Boolean(playingVoice)}
                      >
                        {playingVoice === voiceKey ? 'Loading…' : 'Preview'}
                      </button>
                    </article>
                  );
                })}
              </div>
            </section>
          ))}
          {!error && !selectableProviders.length && <div className="empty-project-list"><h2>No voices are available</h2><p>Refresh the catalog after the Fortress voice gateway is healthy.</p></div>}
        </div>
      </section>
    </main>
  );
}
