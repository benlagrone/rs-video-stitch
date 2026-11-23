import { useState } from 'react';
import { STAGING_ACTIONS, groupStagingPhotos, stagingSourceImages } from './virtualStaging.js';

export function VirtualStagingPanel({
  images,
  propertyAddress,
  initialPlan,
  request,
  projectId,
  onEnsureUploaded,
  onPlan,
  onStaged,
}) {
  const [area, setArea] = useState(initialPlan?.area || '');
  const [plan, setPlan] = useState(initialPlan || null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const sources = stagingSourceImages(images);

  async function readHouse() {
    if (!sources.length || busy) return;
    setError('');
    setBusy('Reading the house and area');
    try {
      if (onEnsureUploaded) await onEnsureUploaded();
      const result = await request(`/v1/projects/${encodeURIComponent(projectId)}/photo-enhancement/plan`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          filenames: sources.map((image) => image.name),
          propertyAddress,
          area,
        }),
      });
      setPlan(result.plan);
      onPlan?.(result.plan);
    } catch (err) {
      setError(err.message || String(err));
    } finally {
      setBusy('');
    }
  }

  function updatePhoto(filename, patch) {
    setPlan((current) => {
      if (!current) return current;
      const next = {
        ...current,
        photos: current.photos.map((photo) => (photo.filename === filename ? { ...photo, ...patch } : photo)),
      };
      onPlan?.(next);
      return next;
    });
  }

  async function stageSelected() {
    if (!plan || busy) return;
    const chosen = plan.photos.filter((photo) => photo.action !== 'leave');
    if (!chosen.length) {
      setError('Every selected photo is marked to leave as photographed.');
      return;
    }
    setError('');
    try {
      for (const photo of chosen) {
        setBusy(`Enhancing ${photo.filename}`);
        const result = await request(`/v1/projects/${encodeURIComponent(projectId)}/photo-enhancement/edits`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ confirmed: true, plan, filenames: [photo.filename] }),
        });
        const staged = result.results?.[0];
        if (staged) onStaged?.(staged);
      }
    } catch (err) {
      setError(err.message || String(err));
    } finally {
      setBusy('');
    }
  }

  return (
    <section className="virtual-staging" aria-label="Photo enhancement">
      <div>
        <h3>Photo enhancement</h3>
        <p>This is the first step. Grok reads the house and the area, then corrects light, windows, and color. Virtual staging and video come later.</p>
      </div>
      <label>Area
        <input
          value={area}
          onChange={(event) => setArea(event.target.value)}
          placeholder="Neighborhood, street, and who the listing is for"
        />
      </label>
      <div className="room-tools">
        <button type="button" onClick={readHouse} disabled={Boolean(busy) || !sources.length}>
          {busy && !plan ? busy : 'Read the house and plan enhancements'}
        </button>
        <button type="button" onClick={stageSelected} disabled={Boolean(busy) || !plan}>
          {busy && plan ? busy : 'Enhance approved photos with Grok'}
        </button>
      </div>
      {error && <div className="error-box">{error}</div>}
      {plan && (
        <div className="virtual-staging-plan">
          <p><strong>{plan.styleName}.</strong> {plan.style}</p>
          <p>{plan.styleReason}</p>
          {groupStagingPhotos(plan.photos).map((group) => (
            <div key={group.id}>
              <h4>{group.id}</h4>
              {group.photos.map((photo) => (
                <div className="virtual-staging-photo" key={photo.filename}>
                  <strong>{photo.filename}</strong>
                  <label>Treatment
                    <select value={photo.action} onChange={(event) => updatePhoto(photo.filename, { action: event.target.value })}>
                      {STAGING_ACTIONS.map((action) => <option key={action.id} value={action.id}>{action.label}</option>)}
                    </select>
                  </label>
                  <label>What to do in this photo
                    <textarea value={photo.brief} onChange={(event) => updatePhoto(photo.filename, { brief: event.target.value })} />
                  </label>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
