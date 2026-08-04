#!/usr/bin/env bash
set -euo pipefail

: "${ADMIN_IMAGE:?ADMIN_IMAGE is required}"
: "${EMS_IMAGE:?EMS_IMAGE is required}"
: "${ADMIN_DIGEST:?ADMIN_DIGEST is required}"
: "${EMS_DIGEST:?EMS_DIGEST is required}"
: "${EXPECTED_TAG:?EXPECTED_TAG is required}"
: "${EXPECTED_REVISION:?EXPECTED_REVISION is required}"
: "${EXPECTED_BUILD_ID:?EXPECTED_BUILD_ID is required}"

PLATFORM="${PLATFORM:-linux/amd64}"
ADMIN_REF="${ADMIN_IMAGE}@${ADMIN_DIGEST}"
EMS_REF="${EMS_IMAGE}@${EMS_DIGEST}"
EXPECTED_ARCH="${PLATFORM#linux/}"

fail() {
  echo "REMOTE SYSTEM BUILD MISMATCH: $1" >&2
  exit 1
}

expect() {
  [[ "$1" == "$2" ]] || fail "$3=$1 != $2"
}

label() {
  docker image inspect --format "{{ index .Config.Labels \"$1\" }}" "$2"
}

verify_image() {
  local role="$1" ref="$2" digest="$3"
  expect "$(label org.opencontainers.image.version "$ref")" "$EXPECTED_TAG" "$role version"
  expect "$(label org.opencontainers.image.revision "$ref")" "$EXPECTED_REVISION" "$role revision"
  expect "$(label de.basecubedev.ems.build_id "$ref")" "$EXPECTED_BUILD_ID" "$role build ID"
  expect "$(label de.basecubedev.ems.channel "$ref")" "development" "$role channel"
  expect "$(label de.basecubedev.ems.release_tag "$ref")" "$EXPECTED_TAG" "$role release tag"
  expect "$(docker image inspect --format '{{.Architecture}}' "$ref")" "$EXPECTED_ARCH" "$role architecture"
  docker image inspect --format '{{json .RepoDigests}}' "$ref" \
    | EXPECTED_DIGEST="$digest" python3 -c \
      'import json,os,sys; values=json.load(sys.stdin) or []; expected=os.environ["EXPECTED_DIGEST"]; raise SystemExit(0 if any(item.endswith("@" + expected) for item in values) else 1)' \
    || fail "$role local inspection does not contain $digest"
}

docker pull --platform "$PLATFORM" "$ADMIN_REF"
docker pull --platform "$PLATFORM" "$EMS_REF"
verify_image "Admin" "$ADMIN_REF" "$ADMIN_DIGEST"
verify_image "EMS" "$EMS_REF" "$EMS_DIGEST"

docker run --rm --entrypoint python "$ADMIN_REF" -c \
  'import hashlib,json,pathlib; root=pathlib.Path("/app/release-resources"); manifest=json.loads((root/"resource-manifest.json").read_text()); missing=[]; mismatched=[]; [(missing.append(name) if not (root/name).is_file() else mismatched.append(name) if "sha256:"+hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest else None) for name,digest in manifest["files"].items()]; assert not missing and not mismatched, {"missing":missing,"mismatched":mismatched}'

SYSTEM_BUILD_PATH="/app/release-resources/system-build.json"
RESOURCE_MANIFEST_PATH="/app/release-resources/resource-manifest.json"
for descriptor in "$SYSTEM_BUILD_PATH" "$RESOURCE_MANIFEST_PATH"; do
  docker run --rm "$ADMIN_REF" cat "$descriptor" \
    | EXPECTED_TAG="$EXPECTED_TAG" EXPECTED_REVISION="$EXPECTED_REVISION" \
      EXPECTED_BUILD_ID="$EXPECTED_BUILD_ID" ADMIN_IMAGE="$ADMIN_IMAGE" \
      EMS_IMAGE="$EMS_IMAGE" python3 -c \
      'import json,os,sys; value=json.load(sys.stdin); expected={"system_tag":os.environ["EXPECTED_TAG"],"channel":"development","revision":os.environ["EXPECTED_REVISION"],"build_id":os.environ["EXPECTED_BUILD_ID"],"release_tag":os.environ["EXPECTED_TAG"],"admin_image":os.environ["ADMIN_IMAGE"]+":"+os.environ["EXPECTED_TAG"],"ems_image":os.environ["EMS_IMAGE"]+":"+os.environ["EXPECTED_TAG"]}; bad={key:(value.get(key),wanted) for key,wanted in expected.items() if value.get(key)!=wanted}; assert not bad,bad'
done

ADMIN_CONTAINER="remote-admin-smoke-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"
cleanup() {
  docker rm -f "$ADMIN_CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM
cleanup
docker run --detach --name "$ADMIN_CONTAINER" --publish 127.0.0.1::8090 "$ADMIN_REF" >/dev/null
ADMIN_PORT="$(docker port "$ADMIN_CONTAINER" 8090/tcp | awk -F: 'NR == 1 {print $NF}')"
[[ "$ADMIN_PORT" =~ ^[0-9]+$ ]] || fail "Admin startup did not publish a port"
curl --fail --silent --show-error --retry 30 --retry-all-errors --retry-delay 1 \
  "http://127.0.0.1:${ADMIN_PORT}/api/admin/auth/status" >/dev/null
cleanup

docker run --rm --entrypoint python3 "$EMS_REF" emsctl.py --help >/dev/null
docker run --rm --entrypoint test "$EMS_REF" -f /app/config.template.json
docker run --rm --entrypoint python3 "$EMS_REF" -c \
  'import ems.build_info,ems.config,ems.zendure_mqtt.runtime; print(ems.build_info.collect_build_info())' >/dev/null
expect "$(docker run --rm --entrypoint printenv "$EMS_REF" EMS_GIT_COMMIT)" "$EXPECTED_REVISION" "EMS runtime revision"
expect "$(docker run --rm --entrypoint printenv "$EMS_REF" EMS_BUILD_ID)" "$EXPECTED_BUILD_ID" "EMS runtime build ID"
expect "$(docker run --rm --entrypoint printenv "$EMS_REF" EMS_CHANNEL)" "development" "EMS runtime channel"
expect "$(docker run --rm --entrypoint printenv "$EMS_REF" EMS_RELEASE_TAG)" "$EXPECTED_TAG" "EMS runtime release tag"

echo "Remote Admin ${ADMIN_DIGEST} and EMS ${EMS_DIGEST} passed pull-back inspection on ${PLATFORM}."
