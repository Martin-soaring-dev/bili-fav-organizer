'use strict';

const PROJECT_RELEASE_PATH = /^\/Martin-soaring-dev\/bili-fav-organizer\/releases(?:\/|$)/i;

function isProjectReleaseUrl(value) {
  try {
    const parsed = value instanceof URL ? value : new URL(String(value));
    return parsed.protocol === 'https:' &&
      parsed.hostname === 'github.com' &&
      PROJECT_RELEASE_PATH.test(parsed.pathname);
  } catch (_err) {
    return false;
  }
}

module.exports = { isProjectReleaseUrl };
