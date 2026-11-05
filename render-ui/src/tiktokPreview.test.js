import test from 'node:test';
import assert from 'node:assert/strict';
import { completedTikTokHighlight, isTikTokOutput, refreshTikTokPreviewVideo, selectTikTokPreviewVideo, tikTokPreviewOutputs } from './tiktokPreview.js';

test('TikTok preview lists only actual video outputs with TikTok versions first', () => {
  assert.deepEqual(tikTokPreviewOutputs(['video.mp4', 'thumbnail.jpg', 'tiktok_highlight.mp4', 'tiktok_video.mp4', 'video.srt']), [
    'tiktok_highlight.mp4', 'tiktok_video.mp4', 'video.mp4',
  ]);
});

test('saved actual video selections remain selected, including the original master', () => {
  const outputs = ['video.mp4', 'tiktok_video.mp4', 'tiktok_highlight.mp4'];
  assert.equal(selectTikTokPreviewVideo(outputs, 'tiktok_highlight.mp4', 'tiktok_video.mp4'), 'tiktok_highlight.mp4');
  assert.equal(selectTikTokPreviewVideo(outputs, 'video.mp4', 'tiktok_video.mp4'), 'video.mp4');
});

test('missing or empty selections prefer a saved existing variant, then a TikTok output', () => {
  const outputs = ['video.mp4', 'tiktok_highlight.mp4', 'tiktok_video.mp4'];
  assert.equal(selectTikTokPreviewVideo(outputs, '', 'tiktok_video.mp4'), 'tiktok_video.mp4');
  assert.equal(selectTikTokPreviewVideo(outputs, 'deleted.mp4', 'deleted-variant.mp4'), 'tiktok_video.mp4');
});

test('latest highlights come from a completed available artifact, never opaque filename order', () => {
  const outputs = ['video.mp4', 'tiktok_highlight_aaa.mp4', 'tiktok_highlight_zzz.mp4'];
  assert.equal(selectTikTokPreviewVideo(outputs), '');
  assert.equal(completedTikTokHighlight({ status: 'RUNNING', filename: outputs[2] }, outputs), '');
  assert.equal(completedTikTokHighlight({ status: 'SUCCEEDED', filename: 'missing.mp4' }, outputs), '');
  const latest = completedTikTokHighlight({ status: 'SUCCEEDED', filename: outputs[2] }, outputs);
  assert.equal(selectTikTokPreviewVideo(outputs, '', '', latest), outputs[2]);
  assert.equal(selectTikTokPreviewVideo(outputs, outputs[1], '', latest), outputs[1]);
});

test('a registered full variant stays the default ahead of a latest highlight', () => {
  const outputs = ['video.mp4', 'tiktok_video.mp4', 'tiktok_highlight_new.mp4'];
  assert.equal(selectTikTokPreviewVideo(outputs, '', outputs[1], outputs[2]), outputs[1]);
});

test('refresh replaces an older selected highlight while preserving full variants and masters', () => {
  const outputs = ['video.mp4', 'tiktok_video.mp4', 'tiktok_highlight_old.mp4', 'tiktok_highlight_new.mp4'];
  assert.equal(refreshTikTokPreviewVideo(outputs, outputs[2], outputs[1], outputs[3]), outputs[3]);
  assert.equal(refreshTikTokPreviewVideo(outputs, outputs[1], outputs[1], outputs[3]), outputs[1]);
  assert.equal(refreshTikTokPreviewVideo(outputs, outputs[0], outputs[1], outputs[3]), outputs[0]);
  assert.equal(refreshTikTokPreviewVideo(outputs, outputs[2], outputs[1], ''), outputs[2]);
});

test('an original master or subtitle alone does not imply a generated TikTok version', () => {
  assert.equal(selectTikTokPreviewVideo(['video.mp4', 'tiktok_video.srt']), '');
  assert.equal(isTikTokOutput('video.mp4'), false);
  assert.equal(isTikTokOutput('tiktok_video.srt'), false);
  assert.equal(selectTikTokPreviewVideo([], 'tiktok_missing.mp4'), '');
});

test('a saved custom variant name is recognized only when it is a video output', () => {
  assert.equal(isTikTokOutput('portrait.mov', 'portrait.mov'), true);
  assert.deepEqual(tikTokPreviewOutputs(['video.mp4', 'portrait.mov'], 'portrait.mov'), ['portrait.mov', 'video.mp4']);
  assert.equal(selectTikTokPreviewVideo(['video.mp4'], '', 'portrait.mov'), '');
});
