import test from 'node:test';
import assert from 'node:assert/strict';

import { leadLinesFromState, sceneImageAssignmentsFromProject } from './projectState.js';

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

