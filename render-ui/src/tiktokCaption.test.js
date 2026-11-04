import test from 'node:test';
import assert from 'node:assert/strict';
import { captionContextKey, createCaptionGenerationGuard, shouldAutoGenerateCaption } from './tiktokCaption.js';

test('automatic caption generation leaves saved and manually entered copy intact', () => {
  const empty = { loaded: true, caption: '', hasVideo: true, sourceText: 'Original listing', attempted: false };
  assert.equal(shouldAutoGenerateCaption(empty), true);
  for (const override of [{ caption: 'Saved caption' }, { caption: 'Manual edit' }, { loaded: false }, { hasVideo: false }, { sourceText: ' ' }, { attempted: true }]) {
    assert.equal(shouldAutoGenerateCaption({ ...empty, ...override }), false);
  }
});

test('typing before Ollama finishes prevents a delayed result replacing the draft', async () => {
  const guard = createCaptionGenerationGuard();
  guard.context(captionContextKey('property', 'tour.mp4', { script: 'Original facts' }));
  let finish;
  const response = new Promise((resolve) => { finish = resolve; });
  const token = guard.begin();
  let caption = '';
  const pending = response.then((value) => { if (guard.current(token)) caption = value; });
  guard.invalidate();
  caption = 'User draft';
  finish('Late Ollama caption');
  await pending;
  assert.equal(caption, 'User draft');
});

test('only the current project, video, source text and language can receive its result', () => {
  for (const changed of [
    ['other-property', 'tour.mp4', { script: 'Original facts', language: 'en-US' }],
    ['property', 'highlight.mp4', { script: 'Original facts', language: 'en-US' }],
    ['property', 'tour.mp4', { script: 'Corrected facts', language: 'en-US' }],
    ['property', 'tour.mp4', { script: 'Original facts', language: 'zh-CN' }],
  ]) {
    const guard = createCaptionGenerationGuard();
    guard.context(captionContextKey('property', 'tour.mp4', { script: 'Original facts', language: 'en-US' }));
    const pending = guard.begin();
    guard.context(captionContextKey(...changed));
    assert.equal(guard.current(pending), false);
    const latest = guard.begin();
    assert.equal(guard.current(latest), true);
    assert.equal(guard.current(pending), false);
  }
});
