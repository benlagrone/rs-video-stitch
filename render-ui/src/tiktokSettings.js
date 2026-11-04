export const TIKTOK_DEFAULTS_VERSION = 2;
export const LECROWN_TIKTOK_DEFAULTS = Object.freeze({
  allowComment: true, allowDuet: false, allowStitch: false, brandOrganic: true,
});
const GENERIC_DEFAULTS = Object.freeze({
  allowComment: false, allowDuet: false, allowStitch: false, brandOrganic: false,
});

export function tikTokPreferences(state = {}, policy = {}) {
  const profile = policy.profile || state.tiktokDefaultsProfile || 'lecrown';
  const defaults = { ...(profile === 'lecrown' ? LECROWN_TIKTOK_DEFAULTS : GENERIC_DEFAULTS), ...policy.defaults };
  const saved = state.tiktokDefaultsVersion === TIKTOK_DEFAULTS_VERSION
    && state.tiktokDefaultsProfile === profile;
  const preference = (key, stateKey) => saved && typeof state[stateKey] === 'boolean'
    ? state[stateKey] : Boolean(defaults[key]);
  return {
    profile, version: TIKTOK_DEFAULTS_VERSION,
    allowComment: preference('allowComment', 'tiktokAllowComment'),
    allowDuet: preference('allowDuet', 'tiktokAllowDuet'),
    allowStitch: preference('allowStitch', 'tiktokAllowStitch'),
    brandOrganic: preference('brandOrganic', 'tiktokBrandOrganic'),
  };
}

export function resolvedTikTokAi(assessment) {
  if (!assessment || assessment.status === 'unknown') return null;
  return typeof assessment.isAigc === 'boolean' ? assessment.isAigc : null;
}

export const tikTokVideoKey = (projectId, filename) => `${projectId}\u0000${filename}`;
