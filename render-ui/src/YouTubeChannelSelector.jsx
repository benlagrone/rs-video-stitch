export function YouTubeChannelSelector({ channels, value, onChange, disabled = false }) {
  if (!channels.length) {
    return <p className="youtube-channel-loading">Loading YouTube channels…</p>;
  }

  return (
    <fieldset className="youtube-channel-selector" disabled={disabled}>
      <legend>Publishing channel</legend>
      {channels.map((channel) => {
        const selected = channel.profile === value;
        const mismatched = channel.matchesExpectedChannel === false;
        const status = mismatched ? 'Wrong account connected' : channel.authenticated ? 'Connected' : 'Not connected';
        return (
          <label className={`youtube-channel-option${selected ? ' selected' : ''}${mismatched ? ' mismatch' : ''}`} key={channel.profile}>
            <input
              type="radio"
              name="youtube-publishing-channel"
              value={channel.profile}
              checked={selected}
              onChange={() => onChange(channel.profile)}
            />
            <img
              src={channel.iconUrl || channel.fallbackIconUrl}
              alt={`${channel.channelName || channel.label} channel icon`}
              onError={(event) => {
                if (channel.fallbackIconUrl && event.currentTarget.dataset.fallbackApplied !== 'true') {
                  event.currentTarget.dataset.fallbackApplied = 'true';
                  event.currentTarget.src = channel.fallbackIconUrl;
                }
              }}
            />
            <span className="youtube-channel-copy">
              <strong>{channel.channelName || channel.label}</strong>
              {channel.handle && <small>{channel.handle}</small>}
              <small className={channel.authenticated && !mismatched ? 'connected' : 'disconnected'}>{status}</small>
            </span>
            <span className="youtube-channel-check" aria-hidden="true">{selected ? '✓' : ''}</span>
          </label>
        );
      })}
    </fieldset>
  );
}
