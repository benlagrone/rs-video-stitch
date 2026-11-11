export function SceneProductionControls({ production, busy, onApprove, onGenerate }) {
  const plan = production?.motionPlan;
  const beats = Array.isArray(plan?.action_beats) ? plan.action_beats : [];
  const actors = Array.isArray(plan?.actors) ? plan.actors : [];
  return (
    <div className="suite-production">
      {plan ? (
        <div className="hint">
          <strong>3D plan.</strong> {plan.framing}
          {actors.length ? ` People and objects: ${actors.map((actor) => actor.role || actor.id).join(', ')}.` : ''}
          {beats.length ? ` ${beats.map((beat) => `${beat.seconds}: ${beat.action}`).join(' ')}` : ''}
        </div>
      ) : <p className="hint">Write the scene package with Ollama before approving a 3D render.</p>}
      <div className="script-actions">
        <button type="button" onClick={onApprove} disabled={busy || !plan}>
          {production?.renderStatus === 'rendered' ? '3D render created' : 'Approve and create 3D render'}
        </button>
        <button type="button" onClick={() => onGenerate('phronesis')} disabled={busy || production?.renderStatus !== 'rendered'}>
          Make scene with Phronesis
        </button>
        <button type="button" onClick={() => onGenerate('grok')} disabled={busy || production?.renderStatus !== 'rendered'}>
          Make scene with Grok
        </button>
      </div>
      {production?.renderFile && <p className="hint">3D render: {production.renderFile}</p>}
      {production?.sceneVideoStatus === 'ready' && <p className="hint">Scene video ready via {production.sceneVideoProvider}.</p>}
    </div>
  );
}
