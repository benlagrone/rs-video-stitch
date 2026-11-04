import test from 'node:test';
import assert from 'node:assert/strict';
import { tikTokPreferences, resolvedTikTokAi, tikTokVideoKey } from './tiktokSettings.js';

test('LeCrown migrates old unchecked defaults to the requested publishing preferences', () => {
  const policy = { profile: 'lecrown' };
  const old = { tiktokAllowComment: false, tiktokAllowDuet: true, tiktokAllowStitch: true, tiktokBrandOrganic: false, tiktokAigc: true };
  assert.deepEqual(tikTokPreferences(old, policy), {
    profile: 'lecrown', version: 2, allowComment: true, allowDuet: false, allowStitch: false, brandOrganic: true,
  });
});

test('explicit versioned project choices persist only for their matching profile', () => {
  const state = { tiktokDefaultsVersion: 2, tiktokDefaultsProfile: 'lecrown', tiktokAllowComment: false, tiktokAllowDuet: true, tiktokBrandOrganic: false };
  assert.equal(tikTokPreferences(state, { profile: 'lecrown' }).allowDuet, true);
  assert.equal(tikTokPreferences(state, { profile: 'lecrown' }).allowComment, false);
  assert.equal(tikTokPreferences(state, { profile: 'generic' }).allowDuet, false);
  assert.equal(tikTokPreferences(state, { profile: 'generic' }).brandOrganic, false);
});

test('AI labeling comes only from resolved selected-media evidence', () => {
  assert.equal(resolvedTikTokAi({ status: 'original', isAigc: false }), false);
  assert.equal(resolvedTikTokAi({ status: 'ai', isAigc: true }), true);
  assert.equal(resolvedTikTokAi({ status: 'unknown', isAigc: false }), null);
  assert.equal(resolvedTikTokAi(null), null);
  assert.equal(resolvedTikTokAi({ status: 'original' }), null);
});

test('media selections in different projects or files have distinct assessment keys', () => {
  assert.notEqual(tikTokVideoKey('english', 'video.mp4'), tikTokVideoKey('mandarin', 'video.mp4'));
  assert.notEqual(tikTokVideoKey('english', 'video.mp4'), tikTokVideoKey('english', 'highlight.mp4'));
});
