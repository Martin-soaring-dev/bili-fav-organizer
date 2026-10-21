'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { isProjectReleaseUrl } = require('../electron/external-links');

test('allows only HTTPS pages in this project’s GitHub releases', () => {
  assert.equal(isProjectReleaseUrl(
    'https://github.com/Martin-soaring-dev/bili-fav-organizer/releases/latest'), true);
  assert.equal(isProjectReleaseUrl(
    'https://github.com/Martin-soaring-dev/bili-fav-organizer/releases/tag/v0.210.4'), true);
  assert.equal(isProjectReleaseUrl(
    'http://github.com/Martin-soaring-dev/bili-fav-organizer/releases/latest'), false);
  assert.equal(isProjectReleaseUrl(
    'https://github.com/Martin-soaring-dev/another-repo/releases/latest'), false);
  assert.equal(isProjectReleaseUrl(
    'https://github.com.evil.example/Martin-soaring-dev/bili-fav-organizer/releases/latest'), false);
  assert.equal(isProjectReleaseUrl(
    'https://github.com/Martin-soaring-dev/bili-fav-organizer/issues/12'), false);
  assert.equal(isProjectReleaseUrl('not a URL'), false);
});
