# Security

If you find a security problem in release-rollup, report it privately: open the repository's **Security** tab on GitHub and choose **Report a vulnerability**. Please don't open a public issue for it.

Tell us which version you used, your system and Python version, and the steps or files that show the problem.

Fixes go into the latest release and the latest pre-release.

release-rollup reads zips and folders you point it at, writes zips and moves files to the trash. Problems worth reporting include a zip entry that writes outside the target folder, the tool writing or trashing anything outside the folder it was given, and a restore that produces files whose sha256 doesn't match the manifest.

## Verify a download

Every release after 0.2.0-beta.2 has its zip built on GitHub from its tag, with a signed attestation that says which commit and which workflow run built it. With the [GitHub CLI](https://cli.github.com/) installed, check a download with:

```sh
gh attestation verify release-rollup-<version>.zip --repo Ghost-Hackers/release-rollup
```

It passes only for the exact file the release workflow built. To check that the download wasn't damaged, run `sha256sum -c SHA256SUMS` in the folder that holds both files.

You can also rebuild the zip yourself from a clone and compare:

```sh
git checkout v<version>
python3 tools/build_release.py --out rebuilt
cat rebuilt/SHA256SUMS
```

0.1.0, 0.2.0-beta.1 and 0.2.0-beta.2 were built locally and only have `SHA256SUMS`, which names the zip as `assets/<zip>`. Compare the zip's sha256 with that line by hand.
