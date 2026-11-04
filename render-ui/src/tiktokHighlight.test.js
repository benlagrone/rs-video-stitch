import test from 'node:test';
import assert from 'node:assert/strict';
import { highlightDuration, highlightPlanErrors, normalizeHighlightPlan, mergeHighlightJob } from './tiktokHighlight.js';

const plan = () => ({
  title: '2438 White Oak', hook: 'Step inside this Heights home', sourceDuration: 120,
  graphics: { hookSeconds: 2, outroSeconds: 4, transitionSeconds: 0.4 },
  contact: { phone: ' 832-798-3991 ' },
  segments: [
    { sourceStart: 10, sourceEnd: 29.3, caption: 'Welcome home' },
    { sourceStart: 50, sourceEnd: 69.3, caption: 'Explore the living spaces' },
  ],
});

test('highlight duration includes the opening, closing and transition cards', () => {
  assert.equal(highlightDuration(plan()), 45);
  assert.deepEqual(highlightPlanErrors(plan()), []);
});

test('editing clip times updates the complete exported duration without replacing other settings', () => {
  const edited = plan();
  edited.segments[0].sourceStart = '12';
  edited.captionSource = 'project-text';
  edited.spokenCaptionsEnabled = true;
  const normalized = normalizeHighlightPlan(edited);
  assert.equal(normalized.durationSeconds, 43);
  assert.equal(normalized.segments[0].sourceStart, 12);
  assert.equal(normalized.contact.phone, '832-798-3991');
  assert.equal(normalized.spokenCaptionsEnabled, true);
  assert.equal(normalized.captionSource, 'project-text');
  assert.equal(edited.contact.phone, ' 832-798-3991 ');
});

test('invalid, out-of-bounds and overly long edits cannot be rendered', () => {
  const edited = plan();
  edited.segments[0].sourceStart = '';
  edited.segments[1].sourceEnd = 150;
  const errors = highlightPlanErrors(edited);
  assert.ok(errors.some((error) => error.includes('Clip 1')));
  assert.ok(errors.some((error) => error.includes('source video')));
  assert.ok(errors.some((error) => error.includes('30 and 60')));
});

test('a render progress update never moves backwards, while a new job starts afresh', () => {
  const previous = { jobId: 'first', status: 'RUNNING', progress: 0.7 };
  assert.equal(mergeHighlightJob(previous, { jobId: 'first', status: 'RUNNING', progress: 0.3 }).progress, 0.7);
  assert.equal(mergeHighlightJob(previous, { jobId: 'second', status: 'QUEUED', progress: 0.01 }).progress, 0.01);
  assert.equal(mergeHighlightJob(previous, { jobId: 'first', status: 'SUCCEEDED', progress: 0.97 }).progress, 1);
});
