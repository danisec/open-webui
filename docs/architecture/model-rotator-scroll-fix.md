# Model Rotator settings tab: scroll fix

## Symptom

Settings -> Model Rotator could not be scrolled vertically. Aliases and the
"Add Candidate" row below the fold were unreachable on laptop-height viewports.

## Root cause

`src/lib/components/admin/Settings/ModelRotator.svelte` was the only admin
settings tab without its own scroll container. Its root element was a plain
`<div class="space-y-5">`, while the shared parent in
`src/lib/components/chat/SettingsModal.svelte` clips overflow:

    <div class="flex-1 min-h-0 overflow-hidden">   <!-- line 803 -->

So content taller than the panel was cut off with no way to reach it.

Every other admin tab uses a two-layer pattern:

    <div class="flex h-full flex-col text-sm">
      <div class="flex-1 min-h-0 overflow-y-auto scrollbar-hover pr-1.5">
        ...

## Fix

Wrap the tab in the same two-layer pattern. `SettingsModal.svelte` is
deliberately untouched: relaxing its `overflow-hidden` would give the other
tabs a second, nested scrollbar.

## Additional robustness in the same component

- Refreshes after an action are `silent`, so the acted-on row is not unmounted
  and the scroll position is not reset to the top.
- `window.confirm` before deleting an alias (shows id and candidate count).
- Duplicate guards for alias id and candidate id before calling the API.
- Failed load shows a `Retry` button instead of a dead error message.
- Loading uses the shared `Spinner`; `aria-label` added to icon-only and
  candidate inputs; `type="button"` on all buttons; `title` on truncated
  candidate ids so long ids remain readable.
- Enter submits in the alias and candidate inputs.

## Verification

- `svelte-check`: the file reports only the pre-existing `i18n` store typing
  errors that every component in this repo has (repo total: 6064). No new
  error category.
- `svelte/compiler`: compiles with 0 warnings.
- Compiled chunk contains the wrapper and the new guards.
- Tailwind emitted `.overflow-y-auto`, `.min-h-0`, `.h-full` in the CSS bundle.

## Image

    v0.12.0-rotator-scroll-arm64-v37
    base: v0.12.0-multiupstream-arm64-v36
    backend md5 rotator.py identical to base -> frontend-only change

Built on proxmox-ubuntu (frontend compile in node:22-alpine3.20, then a
COPY-only derived image). The derived Dockerfile removes `/app/build` before
copying so stale hashed chunks cannot linger.

## Rollback

    ssh ubuntu@100.89.132.36
    cp ~/backups/open-webui.container.bak.<ts> ~/.config/containers/systemd/open-webui.container
    systemctl --user daemon-reload && systemctl --user restart open-webui
