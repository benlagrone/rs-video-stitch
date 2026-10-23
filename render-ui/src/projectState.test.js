import test from 'node:test';
import assert from 'node:assert/strict';

import {
  leadLinesFromState,
  renderOptionsFromProject,
  sceneAssignmentsFromRoomInfo,
  sanitizeSceneImageAssignments,
  sceneImageAssignmentsFromProject,
  titleCardFromScript,
  titleFromScript,
} from './projectState.js';

test('recognizes an all-caps opening script line as the video title', () => {
  const parsed = titleFromScript('RARE OVERSIZED LOT | NO HOA\n\nWelcome to the property.');

  assert.equal(parsed.title, 'RARE OVERSIZED LOT | NO HOA');
  assert.equal(parsed.narration, 'Welcome to the property.');
});

test('keeps ordinary mixed-case script openings in narration', () => {
  const parsed = titleFromScript('Welcome to this Heights property.\n\nEnjoy the outdoor space.');

  assert.equal(parsed.title, '');
  assert.equal(parsed.narration, 'Welcome to this Heights property.\n\nEnjoy the outdoor space.');
});

test('recognizes an explicit mixed-case title from intake metadata', () => {
  const parsed = titleFromScript('Heights Home | No HOA\n\nWelcome to the property.', 'Heights Home | No HOA');

  assert.equal(parsed.title, 'Heights Home | No HOA');
  assert.equal(parsed.narration, 'Welcome to the property.');
});

test('does not consume Mandarin narration after an explicit title', () => {
  const parsed = titleFromScript('休斯顿 Heights 稀缺大地块 | 无 HOA\n\n欢迎来到这套房产。', '休斯顿 Heights 稀缺大地块 | 无 HOA');

  assert.equal(parsed.narration, '欢迎来到这套房产。');
});

test('uses text before the first pipe as the video title and later segments as title-card lines', () => {
  const script = 'RARE OVERSIZED LOT IN THE HEIGHTS | 16,281 SQ.\n\nFT.\n\n| NO HOA | GARAGE APARTMENT\n\nWelcome to the property.';

  assert.deepEqual(titleCardFromScript(script), {
    mainTitle: 'RARE OVERSIZED LOT IN THE HEIGHTS',
    lines: ['16,281 SQ. FT.', 'NO HOA', 'GARAGE APARTMENT'],
  });
  assert.equal(titleFromScript(script).narration, 'Welcome to the property.');
});

test('groups Room Renamer labels into three-image scene assignments', () => {
  const assignments = sceneAssignmentsFromRoomInfo(
    [{ name: 'front-1.jpg' }, { name: 'front-2.jpg' }, { name: 'front-3.jpg' }, { name: 'front-4.jpg' }, { name: 'kitchen.jpg' }],
    {
      'front-1.jpg': { label: 'street_view' },
      'front-2.jpg': { label: 'street_view' },
      'front-3.jpg': { label: 'street_view' },
      'front-4.jpg': { label: 'street_view' },
      'kitchen.jpg': { label: 'kitchen' },
    },
  );

  assert.deepEqual(assignments, [
    ['front-1.jpg', 'front-2.jpg', 'front-3.jpg'],
    ['front-4.jpg'],
    ['kitchen.jpg'],
  ]);
});

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

test('preserves up to six saved intro lines', () => {
  const lines = leadLinesFromState({
    introLines: ['7131 Harmony Cove', 'LeCrown Properties', 'Jie Huang', '经纪人', '832-798-3991', '预约看房'],
  });

  assert.deepEqual(lines, ['7131 Harmony Cove', 'LeCrown Properties', 'Jie Huang', '经纪人', '832-798-3991', '预约看房']);
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
