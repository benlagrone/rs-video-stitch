import { useEffect, useMemo, useState } from 'react';

function apiUrl(baseUrl, path) {
  return `${baseUrl.replace(/\/+$/, '')}${path}`;
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
      if (!response.ok) throw new Error(await response.text());
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
      if (!response.ok) throw new Error(await response.text());
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
          <p>Compare the narration voices currently available through the Fortress gateway before assigning one to a video.</p>
        </div>
        <div className="header-actions">
          <button className="theme-toggle" type="button" onClick={onOpenProjects}>Projects</button>
        </div>
      </section>

      <section className="voice-samples-content">
        <div className="voice-sample-copy">
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
          <p>Samples are temporary and are not saved to a project or render queue.</p>
          {audioUrl && <audio className="voice-sample-player" controls autoPlay src={audioUrl}>Your browser cannot play this audio sample.</audio>}
          {error && <div className="error-box">{error}</div>}
        </div>

        <div className="voice-provider-list">
          {selectableProviders.map((provider) => (
            <section className="voice-provider" key={provider.id}>
              <header>
                <div>
                  <h2>{provider.label}</h2>
                  <p>{provider.detail || 'Available for narration'}</p>
                </div>
                <span>{provider.voices.length} voice{provider.voices.length === 1 ? '' : 's'}</span>
              </header>
              <div className="voice-card-grid">
                {provider.voices.map((voice) => {
                  const voiceKey = `${provider.ttsApi}:${voice}`;
                  return (
                    <article className="voice-card" key={voiceKey}>
                      <strong>{voice}</strong>
                      <button
                        className="primary-action"
                        type="button"
                        onClick={() => sampleVoice(provider, voice)}
                        disabled={!text.trim() || Boolean(playingVoice)}
                      >
                        {playingVoice === voiceKey ? 'Generating sample…' : 'Play sample'}
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
