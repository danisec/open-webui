# LightRAG sync for terminal files

A file uploaded into a chat (or the terminal file browser) is indexed into
LightRAG automatically, in the uploader's own workspace, and removed again when
it leaves the workspace. The user no longer has to ask the model to call the
`insert_document` tool, and a deleted file does not keep answering queries.

Two mechanisms work together; the second is the safety net for the first.

## 1. Immediate hook (this fork)

`POST /api/v1/terminals/{server_id}/files/upload` is proxied to the user's Open
Terminal container. When that upload succeeds, `routers/terminals.py` schedules a
background task that forwards the same multipart body to LightRAG's
`POST /documents/upload` with a `LIGHTRAG-WORKSPACE` header naming the uploader's
user id. LightRAG then parses, chunks and embeds the document on its own host.

The client's payload is reused unchanged: both endpoints take a single `file`
part, so no re-encoding is needed. The upload response is not delayed — the
forward runs after it is sent, and a failure only logs.

Configuration (container environment):

| Variable | Meaning |
| --- | --- |
| `LIGHTRAG_INDEX_URL` | LightRAG base URL, e.g. the bearer-auth proxy. Empty disables the hook. |
| `LIGHTRAG_INDEX_TOKEN` | Bearer token the proxy expects. |
| `LIGHTRAG_INDEX_TIMEOUT` | Per-request timeout, seconds (default 600). |
| `LIGHTRAG_INDEX_EXTS` | Comma-separated extensions to forward (default: common documents). |

The workspace is the user id, which is the same key the per-user MCP servers and
the LightRAG terminal connections already use.

## 2. Bridge sweep (safety net)

`lightrag-bridge.py` runs on a systemd timer. Besides the legacy Open WebUI
uploads dir, it now sweeps each Open Terminal workspace
(`LRB_TERMINAL_ROOT/<folder>`) and forwards the files the upload proxy recorded
in Open WebUI's `terminal_upload` table. That table is the same one the cleanup
event uses, so only real uploads are indexed — files the agent generates in the
workspace are not.

The folder to workspace map is read from Open WebUI's own config (each
`open-terminal-<folder>` connection is granted to one user). `LRB_WORKSPACES`
can override it.

Before forwarding, the worker asks LightRAG which filenames the workspace
already holds and skips those, so the usual case — the hook already indexed the
file — costs one list call instead of a second parse and LLM extraction.

This path exists because the hook has no retry: if LightRAG is unreachable when
the upload happens, the bridge picks the file up on its next run.

## 3. Deletion

A document has to leave LightRAG when its file leaves the workspace, or the graph
keeps answering from something the user removed. Three paths cover it:

| Path | Covered by |
| --- | --- |
| File deleted in the file browser | the proxy's `files/delete` hook (option 1) |
| Chat deleted | the `chat_file_cleanup` event function (option 2) |
| Agent `rm`, directory delete, rename | the bridge's reconciliation sweep (option 3) |

The proxy hook runs the same way the upload hook does: after a successful
`DELETE /files/delete`, it looks up documents by filename in the uploader's
workspace and deletes them. A directory delete is left to the sweep, which still
knows which files were inside.

`chat_file_cleanup` deletes the documents for every path it removes from the
workspace. It calls the terminal directly, not through the proxy, so the proxy
hook would not see those deletes — hence the second path.

The sweep is limited to files it can prove were uploads: the entries in its own
state and the basenames in Open WebUI's `terminal_upload` registry. Documents
with no workspace copy at all — the ones migrated from Open WebUI's uploads dir,
for example — are deliberately left alone. A delete can lose a race with an
in-flight extraction, which rewrites the document afterwards; when the document
survives, its state key is kept and the next run retries.

`LRB_RECONCILE=0` turns the sweep off.

## Deploying a change to the hook

The hook lives in the backend, so it needs a new image. A backend-only change
does not need a full source rebuild:

```bash
# overlay the changed files on the current image and push
podman build --platform linux/arm64 --tls-verify=false \
  -t <registry-host>:5000/open-webui-custom:<tag> .
podman push --tls-verify=false <registry-host>:5000/open-webui-custom:<tag>
```

Then point the deployment's Quadlet `Image=` at the new tag, `systemctl --user
daemon-reload`, pull, and restart `open-webui.service`. A full `docker buildx`
build from source produces the same tree.
