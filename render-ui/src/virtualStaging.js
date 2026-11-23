export function isStagedFilename(name) {
  return /-(?:enhanced|virtually-staged)\.[a-z0-9]+$/i.test(String(name || ''));
}

export function stagingSourceImages(images) {
  return (images || []).filter((image) => image?.name && !isStagedFilename(image.name));
}

export function groupStagingPhotos(photos) {
  const groups = [];
  const index = new Map();
  for (const photo of photos || []) {
    const key = photo.group || 'ungrouped';
    if (!index.has(key)) {
      const group = { id: key, photos: [] };
      index.set(key, group);
      groups.push(group);
    }
    index.get(key).photos.push(photo);
  }
  return groups;
}

export const STAGING_ACTIONS = [
  { id: 'enhance', label: 'Enhance' },
  { id: 'polish', label: 'Light correction' },
  { id: 'leave', label: 'Leave as photographed' },
];
