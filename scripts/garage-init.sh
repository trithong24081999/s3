#!/bin/sh
# Idempotent Garage bootstrap for the cold storage tier.
#
# Ported from the side_project stack; the only change is the default bucket,
# which here is the archive the tiering job writes into.
#
# Brings a fresh single-node Garage from "running but unusable" to "ready to
# accept S3 traffic": layout assigned, bucket created, application key present
# and authorised. Every step checks the current state first, so re-running this
# against a configured cluster is a no-op rather than a reset.
#
# Credentials are NOT generated here. The access key and secret come from the
# environment (`.env`) and are *imported* into Garage, which is what makes the
# result reproducible: the same values keep working after a volume wipe, and
# the backend never has to be told a new secret out of band.
#
# Secrets are never echoed. Only the access key id -- a public identifier --
# appears in the log.

set -eu

GARAGE_ADMIN_URL="${GARAGE_ADMIN_URL:-http://garage:3903}"
GARAGE_BUCKET="${GARAGE_BUCKET:-uploads-archive}"
GARAGE_KEY_NAME="${GARAGE_KEY_NAME:-tiering-key}"
GARAGE_ZONE="${GARAGE_ZONE:-dc1}"
# Advertised capacity of the single node, in bytes. Garage refuses to assign a
# layout without one.
GARAGE_NODE_CAPACITY="${GARAGE_NODE_CAPACITY:-10000000000}"
WAIT_ATTEMPTS="${WAIT_ATTEMPTS:-60}"
WAIT_INTERVAL="${WAIT_INTERVAL:-2}"

log() { echo "[garage-init] $*"; }
fail() { log "ERROR: $*"; exit 1; }

for required in GARAGE_ADMIN_TOKEN GARAGE_ACCESS_KEY_ID GARAGE_SECRET_ACCESS_KEY; do
    eval "value=\${$required:-}"
    [ -n "$value" ] || fail "$required must be set (see .env.example)."
done

# `-s` keeps curl quiet, `-w` appends the status code so a single call gives us
# both body and status without a second request.
api() {
    method="$1"
    path="$2"
    payload="${3:-}"

    if [ -n "$payload" ]; then
        curl -sS -X "$method" \
            -H "Authorization: Bearer ${GARAGE_ADMIN_TOKEN}" \
            -H "Content-Type: application/json" \
            -d "$payload" \
            -w '\n%{http_code}' \
            "${GARAGE_ADMIN_URL}${path}"
    else
        curl -sS -X "$method" \
            -H "Authorization: Bearer ${GARAGE_ADMIN_TOKEN}" \
            -w '\n%{http_code}' \
            "${GARAGE_ADMIN_URL}${path}"
    fi
}

status_of() { printf '%s' "$1" | tail -n 1; }
body_of() { printf '%s' "$1" | sed '$d'; }

wait_for_garage() {
    attempt=1
    while [ "$attempt" -le "$WAIT_ATTEMPTS" ]; do
        if response="$(api GET /v1/status 2>/dev/null)" && [ "$(status_of "$response")" = "200" ]; then
            log "Garage admin API is up."
            return 0
        fi
        log "Waiting for Garage admin API (${attempt}/${WAIT_ATTEMPTS})..."
        attempt=$((attempt + 1))
        sleep "$WAIT_INTERVAL"
    done
    fail "Garage admin API did not become available at ${GARAGE_ADMIN_URL}."
}

ensure_layout() {
    response="$(api GET /v1/status)"
    [ "$(status_of "$response")" = "200" ] || fail "Could not read cluster status."
    status_body="$(body_of "$response")"

    node_id="$(printf '%s' "$status_body" | jq -r '.node')"
    [ -n "$node_id" ] && [ "$node_id" != "null" ] || fail "Could not determine the node id."

    # A node that already carries a role means the layout was applied on an
    # earlier run; leave it exactly as it is.
    has_role="$(printf '%s' "$status_body" \
        | jq -r --arg id "$node_id" '[.nodes[]? | select(.id == $id) | .role] | map(select(. != null)) | length')"
    if [ "${has_role:-0}" != "0" ]; then
        log "Layout already assigned for node ${node_id}; leaving it untouched."
        return 0
    fi

    log "Assigning layout role to node ${node_id}..."
    stage_payload="$(jq -n \
        --arg id "$node_id" \
        --arg zone "$GARAGE_ZONE" \
        --argjson capacity "$GARAGE_NODE_CAPACITY" \
        '[{id: $id, zone: $zone, capacity: $capacity, tags: []}]')"
    response="$(api POST /v1/layout "$stage_payload")"
    case "$(status_of "$response")" in
        200|204) ;;
        *) fail "Staging the layout failed: $(body_of "$response")" ;;
    esac

    response="$(api GET /v1/layout)"
    [ "$(status_of "$response")" = "200" ] || fail "Could not read the staged layout."
    current_version="$(body_of "$response" | jq -r '.version // 0')"
    next_version=$((current_version + 1))

    response="$(api POST /v1/layout/apply "$(jq -n --argjson v "$next_version" '{version: $v}')")"
    case "$(status_of "$response")" in
        200|204) log "Layout version ${next_version} applied." ;;
        *) fail "Applying the layout failed: $(body_of "$response")" ;;
    esac
}

ensure_bucket() {
    response="$(api GET "/v1/bucket?globalAlias=${GARAGE_BUCKET}")"
    if [ "$(status_of "$response")" = "200" ]; then
        BUCKET_ID="$(body_of "$response" | jq -r '.id')"
        log "Bucket '${GARAGE_BUCKET}' already exists."
        return 0
    fi

    log "Creating bucket '${GARAGE_BUCKET}'..."
    response="$(api POST /v1/bucket "$(jq -n --arg alias "$GARAGE_BUCKET" '{globalAlias: $alias}')")"
    case "$(status_of "$response")" in
        200|201) BUCKET_ID="$(body_of "$response" | jq -r '.id')" ;;
        *) fail "Creating the bucket failed: $(body_of "$response")" ;;
    esac
    [ -n "$BUCKET_ID" ] && [ "$BUCKET_ID" != "null" ] || fail "Garage returned no bucket id."
}

ensure_key() {
    response="$(api GET "/v1/key?id=${GARAGE_ACCESS_KEY_ID}")"
    if [ "$(status_of "$response")" = "200" ]; then
        log "Access key ${GARAGE_ACCESS_KEY_ID} already registered."
        return 0
    fi

    log "Importing access key ${GARAGE_ACCESS_KEY_ID}..."
    import_payload="$(jq -n \
        --arg id "$GARAGE_ACCESS_KEY_ID" \
        --arg secret "$GARAGE_SECRET_ACCESS_KEY" \
        --arg name "$GARAGE_KEY_NAME" \
        '{accessKeyId: $id, secretAccessKey: $secret, name: $name}')"
    response="$(api POST /v1/key/import "$import_payload")"
    case "$(status_of "$response")" in
        200|201) log "Access key imported." ;;
        *)
            # The response body can echo the request, so it is not logged here.
            log "Importing the key failed (HTTP $(status_of "$response"))."
            fail "Run the documented manual key setup in README.md, then put the
                  resulting credentials in .env."
            ;;
    esac
}

grant_access() {
    log "Granting read/write on '${GARAGE_BUCKET}' to ${GARAGE_ACCESS_KEY_ID}..."
    allow_payload="$(jq -n \
        --arg bucket "$BUCKET_ID" \
        --arg key "$GARAGE_ACCESS_KEY_ID" \
        '{bucketId: $bucket, accessKeyId: $key, permissions: {read: true, write: true, owner: false}}')"
    response="$(api POST /v1/bucket/allow "$allow_payload")"
    case "$(status_of "$response")" in
        200|204) log "Permissions granted." ;;
        *) fail "Granting bucket access failed: $(body_of "$response")" ;;
    esac
}

wait_for_garage
ensure_layout
ensure_bucket
ensure_key
grant_access
log "Garage is ready. Bucket='${GARAGE_BUCKET}' key='${GARAGE_ACCESS_KEY_ID}'."
