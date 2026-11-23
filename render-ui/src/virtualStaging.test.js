import test from 'node:test';
import assert from 'node:assert/strict';
import { groupStagingPhotos, isStagedFilename, stagingSourceImages } from './virtualStaging.js';

test('staging uses original listing photos and keeps generated copies out of the plan', () => {
  const sources = stagingSourceImages([
    { name: 'living.jpg' },
    { name: 'living-enhanced.jpg' },
    { name: 'living-virtually-staged.jpg' },
    { name: '' },
  ]);
  assert.deepEqual(sources.map((image) => image.name), ['living.jpg']);
  assert.equal(isStagedFilename('living-enhanced.jpg'), true);
  assert.equal(isStagedFilename('bath-virtually-staged.png'), true);
});

test('photos from the same area stay together', () => {
  const groups = groupStagingPhotos([
    { filename: 'living.jpg', group: 'main-living' },
    { filename: 'bath.jpg', group: 'bath' },
    { filename: 'living-far.jpg', group: 'main-living' },
  ]);
  assert.deepEqual(groups.map((group) => group.id), ['main-living', 'bath']);
  assert.deepEqual(groups[0].photos.map((photo) => photo.filename), ['living.jpg', 'living-far.jpg']);
});
