#!/usr/bin/env bash
# Bootstrap Flux to an explicit, existing GitLab repository, once.
#
#   flux.sh --inputs FILE < token
#
# Runs on the control VM after bootstrap.sh, and only when the install
# driver was given TALOS_FLUX=on with GITLAB_HOST, GITLAB_OWNER,
# GITOPS_REPOSITORY, GITOPS_BRANCH and GITOPS_CLUSTER_PATH (GITLAB_PERSONAL
# true for a personal namespace). The GitLab API credential arrives on stdin,
# one line, and lives only in this process's environment as GITLAB_TOKEN for
# the calls that need it: never in argv, a file or the log.
#
#   - Flux already bootstrapped (the flux-system GitRepository exists): its
#     URL, branch and path must be the requested ones, or the run stops as
#     drift; nothing is rewritten.
#   - Otherwise the repository must already exist (checked through the
#     GitLab API): flux bootstrap would create a missing one, and v0 never
#     creates a project. Then `flux bootstrap gitlab` with a read-only deploy
#     token for Flux's own access (--deploy-token-auth).
#   - Either way: `flux check`, then the root GitRepository and Kustomization
#     must be Ready within TALOS_FLUX_TIMEOUT (600 s).
#
# Exit status: 0 Flux healthy; 1 a step failed; 3 Flux points elsewhere.
set -euo pipefail
PROG=flux
# shellcheck source=control-lib.sh
. "$(dirname -- "$0")/control-lib.sh"

parse_args "$@"
[ ${#ARGS[@]} -eq 0 ] || die "unknown argument: ${ARGS[0]}"
load_inputs
[ "${TALOS_FLUX:-off}" = on ] || die "TALOS_FLUX is not on; Flux is opt-in"
for var in GITLAB_HOST GITLAB_OWNER GITOPS_REPOSITORY GITOPS_BRANCH GITOPS_CLUSTER_PATH; do
    [ -n "${!var:-}" ] || die "$var is not set"
done
IFS= read -r GITLAB_TOKEN || true
[ -n "${GITLAB_TOKEN:-}" ] || die "no GitLab credential on stdin"
[ -s "$STATE/kubeconfig" ] || die "no kubeconfig in $STATE; run bootstrap.sh first"
TIMEOUT=${TALOS_FLUX_TIMEOUT:-600}
URL=https://$GITLAB_HOST/$GITLAB_OWNER/$GITOPS_REPOSITORY.git
want_path=${GITOPS_CLUSTER_PATH#./}

if current=$(kc -n flux-system get gitrepository flux-system -o jsonpath='{.spec.url} {.spec.ref.branch}' 2>/dev/null); then
    path=$(kc -n flux-system get kustomization flux-system -o jsonpath='{.spec.path}') \
        || die "flux: the flux-system GitRepository exists but its Kustomization cannot be read"
    read -r have_url have_branch <<< "$current"
    if [ "${have_url%.git}" != "${URL%.git}" ] || [ "$have_branch" != "$GITOPS_BRANCH" ] || [ "${path#./}" != "$want_path" ]; then
        drift "flux: already bootstrapped from $have_url $have_branch ${path:-?}, not $URL $GITOPS_BRANCH $GITOPS_CLUSTER_PATH; nothing was changed"
    fi
    log "flux: already bootstrapped from $have_url $have_branch $path"
else
    project=$(printf '%s/%s' "$GITLAB_OWNER" "$GITOPS_REPOSITORY" | sed 's|/|%2F|g')
    # the token reaches curl as a header on stdin, never in argv
    printf 'PRIVATE-TOKEN: %s\n' "$GITLAB_TOKEN" \
        | curl -fsS --max-time 30 --header @- "https://$GITLAB_HOST/api/v4/projects/$project" >/dev/null \
        || die "flux: $GITLAB_HOST/$GITLAB_OWNER/$GITOPS_REPOSITORY is not an existing project this credential can read; v0 never creates one"
    args=(--hostname="$GITLAB_HOST" --owner="$GITLAB_OWNER" --repository="$GITOPS_REPOSITORY"
          --branch="$GITOPS_BRANCH" --path="$GITOPS_CLUSTER_PATH" --deploy-token-auth
          --kubeconfig="$STATE/kubeconfig")
    [ "${GITLAB_PERSONAL:-false}" != true ] || args+=(--personal)
    log "flux: bootstrapping from $URL $GITOPS_BRANCH $GITOPS_CLUSTER_PATH"
    GITLAB_TOKEN=$GITLAB_TOKEN flux bootstrap gitlab "${args[@]}" >&2 \
        || die "flux: bootstrap failed (see above); a rerun checks what exists before bootstrapping again"
fi
unset GITLAB_TOKEN

flux check --kubeconfig="$STATE/kubeconfig" >&2 || die "flux: flux check failed"
kc -n flux-system wait --for=condition=Ready gitrepository/flux-system kustomization/flux-system \
    --timeout="${TIMEOUT}s" >&2 \
    || die "flux: the root GitRepository/Kustomization is not Ready within ${TIMEOUT}s"
echo "flux: healthy"
