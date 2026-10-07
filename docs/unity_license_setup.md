# Unity licence setup — `UNITY_LICENSE` for CI

**Scope: this document is for the separate Unity / Petrichor project.** It is kept
here, next to the rest of the project reference material, because the setup is
the same problem in both repos and the gotchas are expensive to rediscover. It is
**documentation only** — nothing in the AI-native Kali image runs Unity, and
nothing here was executed as part of the Kali work.

---

## What `UNITY_LICENSE` actually is

It is the **contents of the `.ulf` file** (Unity licence file) — not a path, not a
key, not the `.alf`. Both the raw text and its base64 encoding are accepted by
GameCI, but the raw text is the documented default and is what the community
tooling expects.

Three secrets are used together:

| Secret | What it is | Required |
|---|---|---|
| `UNITY_LICENSE` | Contents of the `.ulf` | Always |
| `UNITY_EMAIL` | The Unity account email | Always |
| `UNITY_PASSWORD` | The Unity account password | Always |
| `UNITY_SERIAL` | Serial number — **Pro/Plus only** | Only for a serial licence |

## Path A — Personal licence (the fiddly one)

The Personal licence is bound to the **machine** that minted the `.alf`, so the
activation file has to be generated *on the CI runner*, not on your laptop. A
`.ulf` minted from your desktop's `.alf` will not activate a GitHub runner.

1. Add a bootstrap workflow that runs `game-ci/unity-activate@v4` and uploads the
   resulting `.alf` as an artifact.
2. Run it once. Download the `Unity_vXXXX.alf` artifact from that run.
3. Upload the `.alf` at <https://license.unity3d.com/manual>, choose **Unity
   Personal**, and download the `.ulf`.
4. Put the `.ulf` contents into the repository secret `UNITY_LICENSE`:
   ```bash
   gh secret set UNITY_LICENSE < Unity_vXXXX.ulf
   ```
5. Add `UNITY_EMAIL` and `UNITY_PASSWORD` secrets.

## Path B — Pro / Plus serial licence

Simpler and stable: set `UNITY_SERIAL` (from the Unity account), plus
`UNITY_EMAIL` and `UNITY_PASSWORD`. There is no `.alf`/`.ulf` round trip and no
expiry dance.

## The two gotchas that cost the most time

**1. A Personal `.ulf` expires and CI stops working with no code change.**
Unlike Pro/Plus licensing, the Personal licence has a limited lifetime that
varies by region. The build does not "break" — the *activation* fails, usually at
the least convenient moment. Budget for a renewal job and expect to redo steps
2–4 periodically. This is a licensing-system property, not a misconfiguration.

**2. The `.alf` must be minted on the runner that will use the `.ulf`.**
This is the cause of the classic `Serial number is not valid` /
`No valid Unity Editor license found` failure on a freshly-configured repo. Use
the same `game-ci` activate step in the same job (or a job with the same
`HardwareId`) as the build, or re-mint.

## Workflow shape

```yaml
name: unity-ci
on: [push, workflow_dispatch]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          lfs: true
      - uses: actions/cache@v4
        with:
          path: Library
          key: Library-${{ hashFiles('Assets/**', 'Packages/**', 'ProjectSettings/**') }}
          restore-keys: Library-
      - uses: game-ci/unity-builder@v4
        env:
          UNITY_LICENSE: ${{ secrets.UNITY_LICENSE }}
          UNITY_EMAIL: ${{ secrets.UNITY_EMAIL }}
          UNITY_PASSWORD: ${{ secrets.UNITY_PASSWORD }}
        with:
          targetPlatform: Android   # or StandaloneLinux64 / WebGL
```

For the activation-file bootstrap, add `game-ci/unity-activate@v4` as an earlier
step (or a separate `workflow_dispatch` job) and upload the `.alf`:

```yaml
      - uses: game-ci/unity-activate@v4
        id: activate
        env:
          UNITY_LICENSE: ${{ secrets.UNITY_LICENSE }}
          UNITY_EMAIL: ${{ secrets.UNITY_EMAIL }}
          UNITY_PASSWORD: ${{ secrets.UNITY_PASSWORD }}
        continue-on-error: true
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: unity-activation-file
          path: ${{ steps.activate.outputs.alfPath }}
```

## Verification

A licence is working when the activate/build step logs a licence line and the
build produces an artifact — not merely when the job is green. Check for:

- `Next license update` logged early in the job (activation happened);
- no `No valid Unity Editor license found` in the build step;
- the build artifact actually exists.

## Security rules

- **Never commit the `.ulf`.** It is a credential; treat it like a private key.
- Store it only as a GitHub secret. If it is pasted anywhere (a log, an issue,
  a chat), revoke and re-mint.
- Prefer an environment-scoped secret with required reviewers for any workflow
  that can publish a build.
- Do not put `UNITY_LICENSE`, `UNITY_EMAIL` or `UNITY_PASSWORD` in a workflow that
  runs on `pull_request` from forks — that is where secrets leak.

## Sources

- GameCI: `game-ci/unity-activate`, `game-ci/unity-builder` — <https://game.ci/docs>
- Unity manual licence upload — <https://license.unity3d.com/manual>
- Community walk-throughs of the `.alf` → `.ulf` → secret flow (GameCI docs and
  the `game-ci/unity-license-activate` repository) document the Personal-licence
  expiry and the runner-bound `.alf` explicitly.
