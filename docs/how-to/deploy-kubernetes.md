# Deploy thoth on Kubernetes

This is the Kubernetes way to run thoth, alongside the systemd {doc}`deploy-appliance` runbook.

It assumes a single-node cluster, or a node pinned via `nodeSelector`. thoth's workloads all use `ReadWriteOnce` storage and run one replica each, so there is no multi-node vault sharing here.

The deployment artefacts are a container image and an OCI Helm chart, both published by CI to GitHub Container Registry. The cluster overlay, meaning the ArgoCD `Application` and the sealed secret, lives in a separate repo.

thoth is a single-user, clean-slate project. There is one operator, and the vault and config are re-created from scratch when needed, so these steps describe the one true way to deploy on Kubernetes and there is no migration path to preserve.

```{warning}
Nothing host-specific is committed to this repo. Your `.env` values, Slack member and channel
IDs, the cluster domain and node names, and your kubeconfig live **only** in your private
notes and in the cluster overlay repo, as a SealedSecret, and never in a tracked file here.
Leak-scan before every commit.
```

## 0. What you are building

Four long-running Deployments and three scheduled CronJobs share two persistent volumes, a git-backed Obsidian vault and a `THOTH_HOME` state volume, plus a disposable index volume for Hindsight.

```text
        +-------------------------- namespace: thoth --------------------------+
Slack <-| Deployment thoth-slack ---------(http)--------> Service hindsight    |
(Socket | (capture / retrieve, no Service)            +-> Deployment hindsight |
 Mode)  |                                              |    (semantic index)   |
        | Deployment thoth-mcp --(Service:8765)--<ingress>   PVC hindsight-pg0 |
        | (pkm_* tools, bearer auth)                        (disposable index) |
        |                                                                      |
        | CronJob reindex 06:30 | summary daily 07:00 | summary weekly Mon 07:00|
        |                                                                      |
        |   PVC vault (RWO) <--git push/pull (HTTPS)--> pkm-vault (GitHub)     |
        |   PVC thoth-home (RWO)                                               |
        +----------------------------------------------------------------------+
```

- **Vault is canonical.** Knowledge is Markdown in the `pkm-vault` git repo. The Hindsight index lives on its own disposable PVC and is rebuilt from the vault. See {doc}`recovery`.
- Every workload is `replicas: 1` with `strategy: Recreate`, because an `RWO` volume attaches to only one pod at a time, so the old pod must release the volume before the new one starts.
- **Three CronJobs**, with `Forbid` concurrency and `OnFailure` restart, run on the `Europe/London` timezone: `reindex` at 06:30, `summary daily` at 07:00 and `summary weekly` on Mondays at 07:00. The systemd-only `config-backup` job is dropped on Kubernetes, because the cluster overlay is GitOps-managed so there is nothing to back up.

## 1. Tag a release (CI publishes the image and chart)

Both deliverables are built and published by CI on a version tag. Cut a semver tag on the default branch:

```bash
git tag 1.2.3
git push origin 1.2.3
```

CI then:

- builds the container and pushes it to `ghcr.io/gilesknap/thoth`, tagged `1.2.3` and `latest`. The image carries the `runtime` extra (slack-bolt, mcp, uvicorn, starlette, anthropic, firecrawl, pillow), the `bin/vault-pull` and `bin/vault-commit` git wrappers, plus `git` and `ca-certificates`.
- packages the Helm chart with the tag stamped as both `version` and `appVersion`, and pushes it to the OCI registry `oci://ghcr.io/gilesknap/charts` as chart `thoth`.

Confirm both packages are published and public, because anonymous OCI pull is how ArgoCD fetches them:

```bash
docker pull ghcr.io/gilesknap/thoth:1.2.3
helm pull oci://ghcr.io/gilesknap/charts/thoth --version 1.2.3
```

## 2. Consume the OCI chart from ArgoCD

The cluster overlay is the ArgoCD `Application` pointing at the published chart, the per-cluster `values` (node pin, storage class, ingress host), and the `thoth-env` SealedSecret.

It lives in the tpi-k3s-ansible deploy issue rather than in this repo. That issue is the single place the host-specific wiring is recorded and applied, and this page only describes the chart's contract so that the overlay can be written against it.

In outline, the overlay's `Application` has a single OCI source:

```yaml
# (lives in the cluster overlay repo; shown here for the chart contract only)
source:
  repoURL: ghcr.io/gilesknap/charts
  chart: thoth
  targetRevision: 1.2.3            # matches the image tag
  helm:
    valuesObject:
      image:
        tag: 1.2.3
      secretName: thoth-env        # the SealedSecret below
      # nodeSelector / storage / ingress.host are host-specific (overlay repo)
```

The chart does not template the Secret. Every workload reads its secret environment via `envFrom: secretRef` pointing at `.Values.secretName`, and you provide that Secret out of band as described next.

## 3. The SealedSecret recipe

Put every secret thoth reads into one Kubernetes `Secret` and seal it with [`kubeseal`](https://github.com/bitnami-labs/sealed-secrets), so that the encrypted form is safe to commit to the public overlay repo.

The plaintext `Secret` is built from your `.env` and is never committed.

The keys thoth's `Config` reads are:

```text
ANTHROPIC_API_KEY        GITHUB_PKM_VAULT_TOKEN   SLACK_ALLOWED_USERS
FIRECRAWL_API_KEY        SLACK_BOT_TOKEN          SLACK_CAPTURE_CHANNEL
THOTH_MCP_API_KEYS       SLACK_APP_TOKEN          SLACK_SUMMARY_CHANNEL
```

`SLACK_ALERT_CHANNEL` is optional. It is the unattended error and heartbeat target, and it falls back to the capture channel when unset.

All of the above are sensitive, so they live in the Secret rather than the ConfigMap.

Build the plaintext Secret from a `.env` file, pipe it straight through `kubeseal`, and keep only the sealed output:

```bash
# .env holds the real values; it is never committed.
kubectl create secret generic thoth-env \
  --namespace thoth \
  --from-env-file=.env \
  --dry-run=client -o yaml \
| kubeseal \
    --controller-name sealed-secrets \
    --controller-namespace kube-system \
    --format yaml \
  > thoth-env.sealed.yaml          # this file is safe to commit to the overlay repo
```

The Hindsight pod runs fact-extraction on Anthropic, so it reuses `ANTHROPIC_API_KEY` as its LLM key, pulled from the same Secret via an explicit `secretKeyRef` with no extra key needed.

Channel and member IDs only ever live inside this Secret, never in chart values.

## 4. Storage and node pinning (via values)

All host-specific placement is driven through chart values, so the chart itself stays host-agnostic. The overlay sets:

- **`nodeSelector`** pins every workload to the node that owns the `RWO` volumes. It defaults to `kubernetes.io/arch: amd64`, and the overlay narrows it to a single node.
- **`storage.<vault|thothHome|pg0>.{size,accessMode,storageClassName}`** is one block per PVC. The `vault` and `thothHome` volumes hold durable state, and the vault is also git-backed off-cluster, while `pg0` is the disposable Hindsight index that can be deleted and rebuilt.

```yaml
# overlay values (illustrative; real storageClassName is host-specific)
nodeSelector:
  kubernetes.io/arch: amd64
storage:
  vault:      { size: 5Gi,  accessMode: ReadWriteOnce, storageClassName: <class> }
  thothHome:  { size: 1Gi,  accessMode: ReadWriteOnce, storageClassName: <class> }
  pg0:        { size: 5Gi,  accessMode: ReadWriteOnce, storageClassName: <class> }
```

Non-secret configuration is rendered into a ConfigMap by the chart. That covers `PKM_VAULT`, `THOTH_HOME`, `OBSIDIAN_VAULT_NAME`, `ANTHROPIC_MODEL`, the daily budget, the Git remote and branch, and `THOTH_HINDSIGHT_BASE_URL`.

In-cluster, `THOTH_HINDSIGHT_BASE_URL` defaults to the Hindsight Service at `http://<release>-hindsight:8888`, so the Slack and MCP pods reach the index over the cluster network rather than loopback.

## 5. MCP ingress, bearer auth, and the 421 gotcha

The MCP Deployment runs `thoth mcp --transport http --host 0.0.0.0 --port 8765` and is fronted by a `ClusterIP` Service on `8765`.

Enable the optional ingress to expose it at `https://thoth.<cluster-domain>`:

```yaml
ingress:
  enabled: true
  host: thoth.<cluster-domain>      # host-specific: set in the overlay
```

Remote Claude Code reaches the `pkm_*` tools with a bearer key drawn from `THOTH_MCP_API_KEYS` in the Secret:

```bash
npx mcp-remote https://thoth.<cluster-domain>/mcp/ \
  --header "Authorization: Bearer <key>"
```

````{warning}
Starlette's `TrustedHostMiddleware` validates the `Host` header. Behind an ingress the
inbound `Host` is your public hostname rather than `0.0.0.0`, so without intervention every
real request returns **`421 Misdirected Request`**. The chart exposes both knobs, and you set
them in the overlay values to your public host:

```yaml
config:
  THOTH_MCP_ALLOWED_HOSTS: thoth.<cluster-domain>
  THOTH_MCP_ALLOWED_ORIGINS: https://thoth.<cluster-domain>
```

`THOTH_MCP_ALLOWED_HOSTS` clears the `Host`-header check, and `THOTH_MCP_ALLOWED_ORIGINS`
clears the CORS and origin check for browser-originated requests from the claude.ai
connector. During the live verify, watch `kubectl logs deploy/<release>-mcp` for `421` and
adjust whichever check is still failing.
````

## 6. First light and verify

After ArgoCD syncs the namespace, confirm every pod is Ready on the pinned node, then walk the live boundaries:

```bash
kubectl -n thoth get pods -o wide          # all Running/Ready on the pinned node
kubectl -n thoth logs deploy/<release>-slack --tail=20
kubectl -n thoth logs deploy/<release>-hindsight --tail=20
```

Smoke each seam:

- **Slack capture.** Post a note in the capture channel and watch it land in the vault, get committed, and be pushed to GitHub.
- **Recall.** Query in Slack, or via MCP, and confirm the captured page is cited.
- **MCP.** Hit `https://thoth.<cluster-domain>/mcp/` with the bearer key, as above.
- **CronJob.** Trigger a reindex out of band and confirm it writes the bank:

  ```bash
  kubectl -n thoth create job --from=cronjob/<release>-reindex reindex-smoke
  kubectl -n thoth logs job/reindex-smoke
  ```

For the full per-boundary checklist covering Anthropic, Hindsight, Slack, MCP, Firecrawl and cron, plus the one-command live-smoke suite, work through {doc}`first-light`. The boundaries are the same and only the process supervisor differs.

## See also

- {doc}`deploy-appliance` for the systemd appliance runbook, the non-Kubernetes way.
- {doc}`slack-setup` to create the Slack app and wire the tokens.
- {doc}`first-light` to verify every live boundary after a deploy.
- {doc}`recovery` to rebuild from the two git repos and your secrets.
- {doc}`../explanations/architecture` for how the pieces fit and why.
