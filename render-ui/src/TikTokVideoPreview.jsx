import { isTikTokOutput, tikTokPreviewOutputs } from './tiktokPreview.js';

export function TikTokVideoPreview({ outputs, filename, variantFilename, videoHref, onChange, latestHighlightFilename = '', hasMaster = false }) {
  const videos = tikTokPreviewOutputs(outputs, variantFilename);
  const hasTikTokVideo = videos.some((file) => isTikTokOutput(file, variantFilename));
  return (
    <section className="tiktok-inline-preview" aria-label="TikTok video preview">
      <div className="output-heading">
        <h3>TikTok video preview</h3>
        {hasMaster && <a href="#tiktok-video-tools">Create or edit TikTok video</a>}
      </div>
      {hasTikTokVideo && videos.includes(filename) ? <>
        {videos.includes(variantFilename) && <button type="button" aria-pressed={filename === variantFilename} onClick={() => onChange(variantFilename)}>Show full-length variant</button>}
        {videos.includes(latestHighlightFilename) && filename !== latestHighlightFilename && <button type="button" onClick={() => onChange(latestHighlightFilename)}>Show latest highlight</button>}
        <label>TikTok preview video
          <select value={filename} onChange={(event) => onChange(event.target.value)}>
            {videos.map((file) => <option key={file} value={file}>{file === variantFilename ? 'Full-length variant · ' : file === latestHighlightFilename ? 'Latest highlight · ' : /^tiktok_highlight(?:[_-]|\.)/i.test(file) ? `${latestHighlightFilename ? 'Earlier highlight' : 'Highlight'} · ` : ''}{file}</option>)}
          </select>
        </label>
        <p className="hint">This is also the selected video in the TikTok publishing controls.</p>
        {latestHighlightFilename && filename !== latestHighlightFilename && /^tiktok_highlight(?:[_-]|\.)/i.test(filename) && <p className="hint" role="status">An earlier highlight is selected. Use Show latest highlight to view the newest completed render.</p>}
        <code className="tiktok-inline-preview__filename">{filename}</code>
        <div className="tiktok-inline-preview__player">
          <video key={videoHref} src={videoHref} controls playsInline preload="metadata" aria-label="Selected TikTok video" />
        </div>
        <div className="button-row">
          <a href={videoHref} target="_blank" rel="noreferrer">Open TikTok video</a>
          <a href={videoHref} download={filename}>Download TikTok video</a>
        </div>
      </> : <p className="hint">No TikTok video has been created for this project yet. {hasMaster
        ? <a href="#tiktok-video-tools">Create a vertical variant or automatic highlight below.</a>
        : 'Render the original video first, then create its TikTok version.'}</p>}
    </section>
  );
}
