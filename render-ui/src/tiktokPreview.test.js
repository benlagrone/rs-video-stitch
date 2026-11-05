import test from 'node:test';
import assert from 'node:assert/strict';
import { isTikTokOutput, selectTikTokPreviewVideo, tikTokPreviewOutputs } from './tiktokPreview.js';

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
  assert.equal(selectTikTokPreviewVideo(outputs, 'deleted.mp4', 'deleted-variant.mp4'), 'tiktok_highlight.mp4');
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
