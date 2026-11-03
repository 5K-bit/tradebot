# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-02

First tagged release. See [docs/releases/v0.1.0.md](docs/releases/v0.1.0.md).

### Added

- `VERSION` file and a release workflow: runs the full CI, checks the tag matches `VERSION` and is on the default branch, packages the source as a zip with SHA256SUMS, and publishes a prerelease.
