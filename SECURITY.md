# Security

If you find a security problem in release-rollup, report it privately: open the repository's **Security** tab on GitHub and choose **Report a vulnerability**. Please don't open a public issue for it.

Tell us which version you used, your system and Python version, and the steps or files that show the problem.

Fixes go into the latest release and the latest pre-release.

release-rollup reads zips and folders you point it at, writes zips and moves files to the trash. Problems worth reporting include a zip entry that writes outside the target folder, the tool writing or trashing anything outside the folder it was given, and a restore that produces files whose sha256 doesn't match the manifest.
