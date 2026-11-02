export function ThumbnailStyleSelector({ value, onChange, disabled = false, bible = false }) {
  return (
    <label>Thumbnail style
      <select value={value} onChange={(event) => onChange(event.target.value)} disabled={disabled}>
        <option value="black-agent-card">{bible ? 'Black title card' : 'Black agent card'}</option>
        <option value="first-frame">First frame</option>
      </select>
    </label>
  );
}
