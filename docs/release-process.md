# Release process

Both stable and beta releases run tests and build the portable ZIP, Inno Setup installer, Electron Neo installer, and their three SHA-256 files.

- Push a version tag without a Release: Actions creates and publishes the Release with the six attachments.
- Publish a Release yourself: Actions uploads the attachments to that Release.
- Existing attachments with the same filenames are overwritten. Existing Release titles and descriptions are preserved.
- Packages are also retained as an Actions artifact for 14 days.

## Repair an existing release

Open Actions → Stable Windows tag release → Run workflow.

Select **main** as the branch and enter the existing version tag, such as **v0.210.1**.
Manual runs build the selected branch/ref and write its actual commit into the application.
Tag-push and Release-published runs build the version tag.

CLI equivalent:

```sh
gh workflow run release-stable.yml --repo Martin-soaring-dev/bili-fav-organizer --ref main -f tag=v0.210.1
```

For beta versions, select the Beta Windows tag release workflow instead.
Re-running an old failed run uses its original workflow revision; start a new manual run to use a workflow fix.
