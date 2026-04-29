# Release Process

This is the end-to-end release checklist for Thoth.

## Versioning

Thoth uses semantic versioning:

- Patch: `3.17.1` for bug fixes
- Minor: `3.18.0` for new backwards-compatible features
- Major: `4.0.0` for breaking changes
- Beta/RC: `3.18.0-beta.1`, `3.18.0-rc.1`

## Before release

1. Make sure all feature/fix PRs are merged to `main`.
2. Run the full suite locally:

   ```bash
   python test_suite.py
   ```

3. Cut a release-prep branch:

   ```bash
   git checkout main
   git pull --ff-only
   git checkout -b chore/release-vX.Y.Z
   ```

4. Bump versions with:

   ```bash
   python scripts/cut_release.py X.Y.Z
   ```

   This updates `version.py`, `installer/thoth_setup.iss`,
   `.github/workflows/release.yml`, the macOS app `Info.plist`, and the bug
   report version placeholder.

5. Update `RELEASE_NOTES.md` with human-readable notes.
6. Open and merge the release-prep PR.

## Build artifacts

1. Tag the release commit:

   ```bash
   git checkout main
   git pull --ff-only
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```

2. Run GitHub Actions -> `Release - Build & Sign Installers` manually. This
   produces workflow artifacts; final release assets are uploaded manually after
   signing and smoke testing.
3. Download the Windows setup exe from the workflow artifact.
4. Sign it locally with the Certum certificate. Windows signing is intentionally
   not done in CI:

   ```powershell
   $signtool = "C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe"
   $exe = "dist\ThothSetup_X.Y.Z.exe"
   & $signtool sign /sha1 2341B4B36A21DF948E538A88BB194FAE4D1CAE51 /fd SHA256 /tr http://time.certum.pl /td SHA256 /d "Thoth" /du "https://github.com/siddsachar/Thoth" $exe
   & $signtool verify /pa /v $exe
   ```

5. Upload the signed exe to the draft GitHub Release.
6. Run notarization workflows for macOS when needed and upload the stapled DMG.
7. Smoke-test the final Windows and macOS artifacts.
8. Publish the GitHub Release.
9. Confirm `.github/workflows/update-manifest.yml` patches SHA256 hashes into
   the release body.
10. Test the packaged updater from the previous stable version.

## Post-release

- Post release notes and announcement.
- Open a tracking issue for the next patch/minor release.
- Label any follow-up bugs with the released version.
