import test from 'node:test';
import assert from 'node:assert/strict';

import {
  renderStageLabel,
  renderStepStates,
  renderTrackerProgress,
} from './renderProgress.js';

test('reserves early progress for save, delete, room naming, and queue phases', () => {
  assert.equal(renderTrackerProgress({ phase: 'saving' }), 0.03);
  assert.equal(renderTrackerProgress({ phase: 'naming' }), 0.08);
  assert.equal(renderTrackerProgress({ phase: 'deleting' }), 0.12);
  assert.equal(renderTrackerProgress({ phase: 'queueing' }), 0.18);
  assert.ok(Math.abs(renderTrackerProgress({ phase: 'rendering', jobProgress: 0.5 }) - 0.58) < 1e-9);
  assert.equal(renderTrackerProgress({ phase: 'complete' }), 1);
});

test('makes deletion visible only when an existing MP4 will be replaced', () => {
  assert.deepEqual(renderStepStates({ phase: 'deleting', willDelete: true }), [
    'complete', 'complete', 'active', 'pending', 'pending', 'pending',
  ]);
  assert.deepEqual(renderStepStates({ phase: 'queueing', willDelete: false }), [
    'complete', 'complete', 'skipped', 'active', 'pending', 'pending',
  ]);
});

test('translates worker stages into operator-friendly labels', () => {
  assert.equal(renderStageLabel('AUDIO_PREP'), 'Preparing narration and scene media');
  assert.equal(renderStageLabel('SCENE_BUILD[04]'), 'Building scene 5');
  assert.equal(renderStageLabel('CONCAT'), 'Assembling the final video');
});

test('keeps completed phases visible when a later phase fails', () => {
  assert.deepEqual(renderStepStates({ phase: 'failed', failedFrom: 'rendering', willDelete: true }), [
    'complete', 'complete', 'complete', 'complete', 'failed', 'pending',
  ]);
});
