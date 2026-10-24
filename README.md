# HACS Docker images

This repository turns pinned [HACS](https://hacs.xyz/) integrations into data-only OCI images for Kubernetes [image volumes](https://kubernetes.io/docs/tasks/configure-pod-container/image-volumes/). Each image contains one integration at its filesystem root, ready to mount at `/config/custom_components/<domain>` in a Home Assistant pod.

## Published image

The initial configuration publishes:

```text
ghcr.io/isonami/hacs-docker/ha-nl-weather:v0.13.1
```

A successful publication also creates the repository tag and GitHub Release `ha-nl-weather-v0.13.1`. The release notes record the mutable version reference, multi-platform manifest digest, immutable digest reference, upstream tag, and resolved upstream commit. Releases have no attached archives.

Images target `linux/amd64` and `linux/arm64`. They are `scratch` data images: they contain plugin files but no shell, package manager, entrypoint, or runtime.

## Add or update a plugin

Create `plugins/<plugin-name>/config.json`:

```json
{
  "repository": "owner/repository",
  "version": "v1.2.3",
  "artifact": "auto",
  "asset_pattern": "integration-*.zip",
  "source_path": "custom_components/integration_domain",
  "domain": "integration_domain"
}
```

Fields:

- `repository`: upstream GitHub `owner/repository`.
- `version`: exact upstream release/tag and OCI image tag. It must contain only letters, digits, `.`, `_`, and `-`, and cannot be silently normalized.
- `artifact`: optional, defaults to `auto`. Supported values are `auto`, `release-asset`, `release-source`, and `tag-source`.
- `asset_pattern`: optional case-sensitive glob for a release ZIP asset. It is valid only with `auto` or `release-asset`. Without it, a release must contain exactly one ZIP asset or selection fails as ambiguous.
- `source_path`: integration path inside an upstream source archive.
- `domain`: expected `domain` in the integration's `manifest.json`.

`auto` tries, in order:

1. A matching uploaded ZIP release asset.
2. The GitHub source ZIP for the named release.
3. The GitHub source archive for the tag.

Fallback occurs only when a release or artifact is absent. Authentication, network, corrupt archive, unsafe path, and extraction errors stop publication. Upstream code is never executed. Archive traversal and symlinks are rejected, and the manifest domain must match the configuration. Repository-level license/notice files are copied to `.hacs-docker/licenses/` inside the image.

Run local checks with:

```bash
python3 -m unittest discover -s tests
python3 scripts/plugins.py validate
python3 scripts/plugins.py check-tags
```

To prepare one integration locally:

```bash
python3 scripts/plugins.py prepare \
  --tag ha-nl-weather-v0.13.1 \
  --output build/plugin \
  --provenance build/provenance.json
```

### Immutable versions

Repository tags and published plugin versions are immutable. Once `<plugin-name>-<version>` exists, changing that version's configuration is rejected. Update `version` to publish changed configuration or a new upstream version; existing tags are never moved.

Tag parsing does not split on hyphens. It matches the complete configured `<plugin-name>-<version>` string, so hyphens in either component are safe.

## Automation

- `.github/workflows/sync-tags.yml` validates configurations on pull requests. On pushes to `main`, it compares the complete desired tag set with exact local Git tags, creates missing annotated tags at that commit, then calls the publisher once per new tag in a parallel, non-fail-fast matrix.
- `.github/workflows/publish-plugin.yml` can also run for manually pushed tags or by manual dispatch. It checks out the exact tag, validates its configuration, prepares the plugin, pushes the multi-platform GHCR image, and creates or repairs the GitHub Release.

Tags created with `GITHUB_TOKEN` do not start another workflow, so the synchronization workflow invokes the reusable publisher directly. No PAT or GitHub App secret is required.

The workflows request only `contents: write` and `packages: write` where publication needs them. In **Settings → Actions → General**, allow GitHub Actions to create and approve pull requests only if desired; it is not needed here. Ensure workflow `GITHUB_TOKEN` permissions are not restricted below read/write access required by the jobs.

GHCR packages may initially be private. In the package settings, link the package to this repository and choose public visibility for unauthenticated clusters, or configure an `imagePullSecret` for private pulls. Each plugin is a separate package below `ghcr.io/isonami/hacs-docker/`.

### Recovery

Use **Actions → Publish plugin image → Run workflow** and enter the exact repository tag (for example `ha-nl-weather-v0.13.1`). The workflow is idempotent:

- if both image and release exist, it exits without republishing;
- if the image exists but the release is missing, it creates the release using the existing digest;
- if the release exists but the image is missing, it rebuilds the image and repairs the release notes.

An arbitrary tag that does not exactly match a configuration fails validation.

## Kubernetes image volume

See [`examples/home-assistant.yaml`](examples/home-assistant.yaml). The image root is mounted read-only at the integration directory. Image volumes require Kubernetes/runtime support for the `image` volume source (beta and enabled by default in Kubernetes 1.33). Confirm support in the Kubernetes version and container runtime used by your cluster.

Image volumes do **not** install Python packages declared in a Home Assistant integration manifest. Home Assistant must be able to install those requirements itself, or its runtime image must already provide them.

## Post-publication verification

After merging a version change to `main`:

1. Confirm the synchronization run created only the missing `<plugin>-<version>` tag and that every matrix publication completed independently.
2. Run `docker buildx imagetools inspect ghcr.io/isonami/hacs-docker/<plugin>:<version>` and confirm both `linux/amd64` and `linux/arm64` manifests. Compare its top-level digest with the immutable reference in the matching GitHub Release.
3. Confirm the release has no attached plugin archive and records the expected upstream tag and commit.
4. Re-run the synchronization workflow and confirm the tag matrix is empty. Use manual dispatch on the exact tag to confirm completed publications are skipped.
5. Apply the example on an image-volume-capable test cluster, inspect `/config/custom_components/<domain>` from the Home Assistant container, verify it is read-only, and confirm Home Assistant discovers the integration.

These publication and cluster checks require repository write permissions, GHCR, and a suitable Kubernetes cluster; the local test suite cannot claim them as completed.

For a private GHCR package, create a registry secret and reference it from the pod:

```bash
kubectl create secret docker-registry ghcr \
  --docker-server=ghcr.io \
  --docker-username=YOUR_GITHUB_USER \
  --docker-password=YOUR_READ_PACKAGES_TOKEN
```

Then enable the `imagePullSecrets` block shown in the example.
