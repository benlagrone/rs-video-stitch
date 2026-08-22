import test from 'node:test';
import assert from 'node:assert/strict';

import {
  leadLinesFromState,
  renderOptionsFromProject,
  sanitizeSceneImageAssignments,
  sceneImageAssignmentsFromProject,
} from './projectState.js';

test('preserves a saved image reused by a later scene', () => {
  const assignments = sanitizeSceneImageAssignments(
    [['front.jpg'], ['kitchen.jpg'], ['front.jpg']],
    ['front.jpg', 'kitchen.jpg'],
  );

  assert.deepEqual(assignments, [['front.jpg'], ['kitchen.jpg'], ['front.jpg']]);
});

test('falls back to persisted scene images when saved assignments are empty', () => {
  const assignments = sceneImageAssignmentsFromProject(
    { sceneImageAssignments: [] },
    [{ images: ['front.jpg'] }, { images: ['kitchen.jpg', 'dining.jpg'] }],
  );

  assert.deepEqual(assignments, [['front.jpg'], ['kitchen.jpg', 'dining.jpg']]);
});

test('keeps complete saved scene assignments', () => {
  const assignments = sceneImageAssignmentsFromProject(
    { sceneImageAssignments: [['saved-front.jpg'], ['saved-kitchen.jpg']] },
    [{ images: ['old-front.jpg'] }, { images: ['old-kitchen.jpg'] }],
  );

  assert.deepEqual(assignments, [['saved-front.jpg'], ['saved-kitchen.jpg']]);
});

test('preserves up to five saved intro lines', () => {
  const lines = leadLinesFromState({
    introLines: ['7131 Harmony Cove', 'LeCrown Properties', 'Jie Huang', '经纪人'],
  });

  assert.deepEqual(lines, ['7131 Harmony Cove', 'LeCrown Properties', 'Jie Huang', '经纪人', '']);
});

test('restores the Mandarin title font when saved render options are missing', () => {
  const options = renderOptionsFromProject({ language: 'zh-CN' });

  assert.equal(options.titleStyle.fontFile, 'input/logo/Arial-Unicode.ttf');
  assert.equal(options.titleStyle.position, 'top-center');
});

test('preserves saved Mandarin title style overrides', () => {
  const options = renderOptionsFromProject({
    language: 'zh-CN',
    renderOptions: {
      crf: 20,
      titleStyle: { fontSize: 52, fill: '#f0f0f0' },
    },
  });

  assert.equal(options.crf, 20);
  assert.equal(options.titleStyle.fontFile, 'input/logo/Arial-Unicode.ttf');
  assert.equal(options.titleStyle.fontSize, 52);
  assert.equal(options.titleStyle.fill, '#f0f0f0');
});
