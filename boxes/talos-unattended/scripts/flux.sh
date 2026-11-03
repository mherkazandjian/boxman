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
# What exists is established positively, never inferred from a failed read:
#   - the cluster: the Flux GitRepository CRD and the flux-system
#     GitRepository are read with --ignore-not-found; a read that fails stops
#     the run. An existing bootstrap must point at the requested URL, branch
#     and path, or the run stops as drift; nothing is rewritten.
#   - the repository, read-only through the GitLab API before a first
#     bootstrap: the project and GITOPS_BRANCH must exist (v0 never creates
#     either; flux bootstrap would), and GITOPS_CLUSTER_PATH on that branch
#     must be new, unless this cluster's journal records its own interrupted
#     bootstrap to exactly this target (journal/flux.intent). A path another
#     installation occupies, or one the API cannot answer for, stops the run.
# Then `flux bootstrap gitlab` with a read-only deploy token for Flux's own
# access (--deploy-token-auth), `flux check`, and the root GitRepository and
# Kustomization must be Ready within TALOS_FLUX_TIMEOUT (600 s).
#
# Exit status: 0 Flux healthy; 1 a step failed; 3 Flux, or the destination,
# belongs to something else.
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
need_state
lock
TIMEOUT=${TALOS_FLUX_TIMEOUT:-600}
URL=https://$GITLAB_HOST/$GITLAB_OWNER/$GITOPS_REPOSITORY.git
want_path=${GITOPS_CLUSTER_PATH#./}
want_path=${want_path%/}
TARGET="$URL $GITOPS_BRANCH $want_path"

# api PATH: GET the GitLab API; prints "<http status>" then the body. The
# token reaches curl as a header on stdin, never in argv.
api() {
    printf 'PRIVATE-TOKEN: %s\n' "$GITLAB_TOKEN" \
        | curl -sS --max-time 30 --header @- -w '\n%{http_code}' "https://$GITLAB_HOST/api/v4/$1"
}
urlenc() { python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }

# ---- the cluster: Flux there or positively absent ---------------------------------
crd=$(kc get crd gitrepositories.source.toolkit.fluxcd.io --ignore-not-found -o name) \
    || die "flux: cannot tell whether Flux is installed (reading its CRD failed); nothing was changed"
current=""
if [ -n "$crd" ]; then
    current=$(kc -n flux-system get gitrepository flux-system --ignore-not-found \
        -o jsonpath='{.spec.url} {.spec.ref.branch}') \
        || die "flux: cannot read the flux-system GitRepository; nothing was changed"
fi

if [ -n "$current" ]; then
    path=$(kc -n flux-system get kustomization flux-system -o jsonpath='{.spec.path}') \
        || die "flux: the flux-system GitRepository exists but its Kustomization cannot be read"
    read -r have_url have_branch <<< "$current"
    path=${path#./}
    if [ "${have_url%.git}" != "${URL%.git}" ] || [ "$have_branch" != "$GITOPS_BRANCH" ] || [ "${path%/}" != "$want_path" ]; then
        drift "flux: already bootstrapped from $have_url $have_branch ${path:-?}, not $URL $GITOPS_BRANCH $GITOPS_CLUSTER_PATH; nothing was changed"
    fi
    log "flux: already bootstrapped from $have_url $have_branch $path"
else
    # ---- the destination, read-only -----------------------------------------------
    project=$(urlenc "$GITLAB_OWNER/$GITOPS_REPOSITORY")
    out=$(api "projects/$project") || die "flux: cannot reach the GitLab API at $GITLAB_HOST"
    [ "${out##*$'\n'}" = 200 ] \
        || die "flux: $GITLAB_HOST/$GITLAB_OWNER/$GITOPS_REPOSITORY is not an existing project this credential can read (HTTP ${out##*$'\n'}); v0 never creates one"
    out=$(api "projects/$project/repository/branches/$(urlenc "$GITOPS_BRANCH")") \
        || die "flux: cannot reach the GitLab API at $GITLAB_HOST"
    case "${out##*$'\n'}" in
        200) ;;
        404) die "flux: the branch $GITOPS_BRANCH does not exist in $GITLAB_OWNER/$GITOPS_REPOSITORY; v0 never creates one" ;;
        *)   die "flux: cannot tell whether the branch $GITOPS_BRANCH exists (HTTP ${out##*$'\n'})" ;;
    esac
    out=$(api "projects/$project/repository/tree?path=$(urlenc "$want_path")&ref=$(urlenc "$GITOPS_BRANCH")&per_page=1") \
        || die "flux: cannot reach the GitLab API at $GITLAB_HOST"
    status=${out##*$'\n'} body=${out%$'\n'*}
    if [ "$status" = 404 ] || { [ "$status" = 200 ] && [ "$(tr -d '[:space:]' <<< "$body")" = "[]" ]; }; then
        log "flux: $want_path on $GITOPS_BRANCH is a new path"
    elif [ "$status" = 200 ]; then
        intent=$(journal_get flux.intent)
        [ "$intent" = "$TARGET" ] \
            || drift "flux: $want_path on $GITOPS_BRANCH of $GITLAB_OWNER/$GITOPS_REPOSITORY already holds files, and this cluster has no record of bootstrapping there; pick a new GITOPS_CLUSTER_PATH. Nothing was changed"
        log "flux: $want_path holds the files of this cluster's own interrupted bootstrap; resuming it"
    else
        die "flux: cannot tell whether $want_path exists on $GITOPS_BRANCH (HTTP $status); nothing was changed"
    fi

    args=(--hostname="$GITLAB_HOST" --owner="$GITLAB_OWNER" --repository="$GITOPS_REPOSITORY"
          --branch="$GITOPS_BRANCH" --path="$GITOPS_CLUSTER_PATH" --deploy-token-auth
          --kubeconfig="$STATE/kubeconfig")
    [ "${GITLAB_PERSONAL:-false}" != true ] || args+=(--personal)
    journal_put flux.intent "$TARGET"
    log "flux: bootstrapping from $URL $GITOPS_BRANCH $GITOPS_CLUSTER_PATH"
    GITLAB_TOKEN=$GITLAB_TOKEN timeout --kill-after=10 $((TIMEOUT + CALL_TIMEOUT)) \
        flux bootstrap gitlab "${args[@]}" --timeout="${TIMEOUT}s" >&2 \
        || die "flux: bootstrap failed (see above); a rerun checks what exists before bootstrapping again"
fi
unset GITLAB_TOKEN

timeout --kill-after=10 $((CALL_TIMEOUT * 2)) flux check --kubeconfig="$STATE/kubeconfig" >&2 \
    || die "flux: flux check failed"
kcw "$TIMEOUT" -n flux-system wait --for=condition=Ready gitrepository/flux-system kustomization/flux-system >&2 \
    || die "flux: the root GitRepository/Kustomization is not Ready within ${TIMEOUT}s"
journal_put flux.done "$TARGET $(now)"
echo "flux: healthy"
