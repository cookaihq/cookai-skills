#!/usr/bin/env bash
set -euo pipefail

# Creates a Nano Banana 2 (gemini-3.1-flash-image-preview) task via aihubmax.com
# and polls until terminal status.
#
# Key resolution chain (high -> low). Every source accepts AIHUB_API_KEY first,
# then the deprecated X_API_KEY:
#   1. env AIHUB_API_KEY
#   2. $PWD/.env.local             (AIHUB_API_KEY=... line; auto-read, no flag needed)
#   3. $PWD/.env                   (AIHUB_API_KEY=... line; auto-read, no flag needed)
#   4. ~/.config/banana-2/.env     (only with --use-local-key)
#
# On HTTP 401 (authentication_error) the script falls back to the next key in
# the chain. 401 does not consume credits. Other errors (402/422/429/5xx,
# network errors) stop the chain immediately.

KEY_NAME="AIHUB_API_KEY"
LEGACY_KEY_NAME="X_API_KEY"
BASE_URL="${AIHUBMAX_BASE_URL:-https://api.aihubmax.com}"
CREATE_ENDPOINT="/v1/images/generations"
QUERY_ENDPOINT_PREFIX="/v1/tasks"

MODEL="gemini-3.1-flash-image-preview"
PROMPT=""
ASPECT_RATIO="1:1"
RESOLUTION="1K"
OUTPUT_FORMAT=""
GOOGLE_SEARCH=0
IMAGE_SEARCH=0
USE_LOCAL_KEY=0
IMAGE_URLS=()
POLL_INTERVAL=8
MAX_ATTEMPTS=90

# Output / download options
OUTPUT_DIR=""        # --output-dir; default: env BANANA_2_OUTPUT_DIR else $PWD
FILENAME=""          # --filename: override full stem (no extension)
LABEL=""             # --label: override 10-char label only
NO_SAVE=0            # --no-save: skip download, just print URLs
TIMESTAMP=""         # set later: YYYYMMDD-HHMMSS at script start

# gemini-3.1-flash-image-preview is the only supported model for this skill.
ALLOWED_MODELS=("gemini-3.1-flash-image-preview")
ALLOWED_ASPECT_RATIOS=("1:1" "1:4" "1:8" "2:3" "3:2" "3:4" "4:1" "4:3" "4:5" "5:4" "8:1" "9:16" "16:9" "21:9" "match_input_image")
ALLOWED_RESOLUTIONS=("512" "0.5K" "1K" "2K" "4K")
ALLOWED_OUTPUT_FORMAT=("jpg" "png" "webp")

usage() {
  cat <<'EOF'
Usage:
  create_task.sh --prompt "..." [options]

Required:
  --prompt          Prompt text (text-to-image: describe the image;
                    image editing: describe the changes to the reference image)

Common options:
  --model           gemini-3.1-flash-image-preview (default; only supported value)
  --aspect-ratio    Output aspect ratio (default 1:1). One of:
                    1:1 1:4 1:8 2:3 3:2 3:4 4:1 4:3 4:5 5:4 8:1 9:16 16:9 21:9 match_input_image
  --resolution      Output quality tier (default 1K). One of: 512 0.5K 1K 2K 4K
                    (512/0.5K = half-size; 1K ~1MP; 2K ~4MP; 4K ~16MP)
  --image-url       Reference image URL. Repeatable. Enables image editing / img2img

Advanced options (omit unless needed):
  --output-format   jpg | png | webp
  --google-search   Enable real-time web search to ground the image
  --image-search    Enable image search assistance (this model only)

Output options:
  --output-dir DIR  Save dir (default: $BANANA_2_OUTPUT_DIR or $PWD)
  --filename NAME   Full filename stem (no extension); overrides datetime-label scheme
  --label TEXT      Short label (<=10 chars) used in default filename
  --no-save         Skip download; only print URLs

Runtime options:
  --poll-interval   Seconds between polls (default 8)
  --max-attempts    Max poll attempts (default 90)
  --base-url        Override API base URL (default https://api.aihubmax.com)
  --use-local-key   Also try ~/.config/banana-2/.env after env / $PWD .env files
  -h, --help        Show help

Key resolution (high -> low; on HTTP 401 falls back to next). Each source
accepts AIHUB_API_KEY first, then the deprecated X_API_KEY:
  1. env AIHUB_API_KEY
  2. $PWD/.env.local         (auto)
  3. $PWD/.env               (auto)
  4. ~/.config/banana-2/.env  (only with --use-local-key)

Each key is sent as: Authorization: Bearer <key>

Examples:
  AIHUB_API_KEY=sk-xxx ./create_task.sh --prompt "A futuristic city skyline at dusk, cyberpunk style" --aspect-ratio 16:9 --resolution 1K
  cd my-project && ./create_task.sh --prompt "..." --aspect-ratio 1:1  # reads from ./.env
  ./create_task.sh --prompt "Replace the background with a tropical beach" \
    --image-url 'https://example.com/photo.jpg' --aspect-ratio match_input_image --resolution 2K --image-search
EOF
}

is_positive_int() { [[ "$1" =~ ^[1-9][0-9]*$ ]]; }

# Sanitize a label for use in a filename. Keeps Unicode (including CJK), strips
# filesystem-unsafe characters, trims whitespace, takes the first 10 code
# points. Empty string is allowed.
sanitize_label() {
  local raw="$1"
  python3 - "$raw" <<'PY'
import sys, re
s = sys.argv[1]
# Drop chars unsafe in filenames; keep CJK and most Unicode letters
s = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', s)
# Collapse whitespace and underscores
s = re.sub(r'\s+', '_', s).strip('_')
# Take first 10 Unicode code points
s = s[:10]
print(s)
PY
}

# Map a Content-Type to a file extension. Returns "" when unknown.
ext_for_content_type() {
  local ct="$1"
  case "$ct" in
    image/png) echo "png" ;;
    image/jpeg|image/jpg) echo "jpg" ;;
    image/webp) echo "webp" ;;
    *)
      echo ""
      ;;
  esac
}

# Compute the unique target path for one image. If the chosen path already
# exists, append "-2", "-3", ... before the extension until a free slot is
# found.
unique_target_path() {
  local dir="$1" stem="$2" ext="$3"
  local candidate="${dir}/${stem}.${ext}"
  local i=2
  while [[ -e "$candidate" ]]; do
    candidate="${dir}/${stem}-${i}.${ext}"
    i=$((i + 1))
  done
  printf '%s\n' "$candidate"
}

# Parse results[] from the completed query response and download each image
# into OUTPUT_DIR with a derived filename.
#
# Nano Banana 2 results[i] is typically just {url} (no content_type), so the
# extension is resolved as: --output-format > response content_type > URL tail > png.
download_results() {
  local query_response="$1"

  if [[ $NO_SAVE -eq 1 ]]; then
    echo "[save] --no-save set; skipping download. URLs printed in response above."
    return 0
  fi

  # Resolve label: --label > auto from prompt (first 10 sanitized chars)
  local label
  if [[ -n "$LABEL" ]]; then
    label="$(sanitize_label "$LABEL")"
  else
    label="$(sanitize_label "$PROMPT")"
  fi

  # Stem: --filename > "{TIMESTAMP}-{label}" (drop trailing -- if label empty)
  local stem
  if [[ -n "$FILENAME" ]]; then
    stem="$FILENAME"
  elif [[ -n "$label" ]]; then
    stem="${TIMESTAMP}-${label}"
  else
    stem="${TIMESTAMP}"
  fi

  # Ensure dir exists
  if ! mkdir -p "$OUTPUT_DIR" 2>/dev/null; then
    echo "[save] Error: cannot create output dir: $OUTPUT_DIR" >&2
    return 1
  fi

  # Extract results[] as TSV of "url<TAB>content_type"
  local pairs
  pairs="$(python3 - "$query_response" <<'PY'
import json, sys
try:
    data = json.loads(sys.argv[1])
except Exception:
    raise SystemExit(0)
for r in (data.get("results") or []):
    url = (r or {}).get("url") or ""
    ct  = (r or {}).get("content_type") or ""
    if url:
        print(f"{url}\t{ct}")
PY
)"

  if [[ -z "$pairs" ]]; then
    echo "[save] No results to download."
    return 0
  fi

  # Count results (for numbering decision)
  local count
  count="$(printf '%s\n' "$pairs" | grep -c '^' || true)"

  local idx=0
  local saved_paths=()
  while IFS=$'\t' read -r url ct; do
    [[ -z "$url" ]] && continue
    idx=$((idx + 1))

    # Resolve extension: --output-format > content_type > URL path tail > png
    local ext=""
    if [[ -n "$OUTPUT_FORMAT" ]]; then
      ext="$OUTPUT_FORMAT"
    fi
    if [[ -z "$ext" ]]; then
      ext="$(ext_for_content_type "$ct")"
    fi
    if [[ -z "$ext" ]]; then
      local path="${url%%\?*}"
      local tail="${path##*.}"
      case "$tail" in
        png|jpg|jpeg|webp) ext="$tail"; [[ "$ext" == "jpeg" ]] && ext="jpg" ;;
        *) ext="png" ;;
      esac
    fi

    # Per-file stem: add zero-padded index only when multiple results exist
    local file_stem="$stem"
    if (( count > 1 )); then
      file_stem="$(printf '%s-%02d' "$stem" "$idx")"
    fi

    local target
    target="$(unique_target_path "$OUTPUT_DIR" "$file_stem" "$ext")"

    echo "[save] Downloading result $idx/$count → $target"
    if ! curl --silent --show-error --location --output "$target" "$url"; then
      echo "[save] Error: download failed for $url" >&2
      continue
    fi
    saved_paths+=("$target")
  done <<< "$pairs"

  if [[ ${#saved_paths[@]} -gt 0 ]]; then
    echo "[save] Saved file(s):"
    printf '  %s\n' "${saved_paths[@]}"
  fi
}

in_array() {
  local val="$1"; shift
  local x
  for x in "$@"; do
    [[ "$x" == "$val" ]] && return 0
  done
  return 1
}

validate_aspect_ratio() {
  local ar="$1"
  in_array "$ar" "${ALLOWED_ASPECT_RATIOS[@]}" && return 0
  echo "Error: --aspect-ratio must be one of: ${ALLOWED_ASPECT_RATIOS[*]}" >&2
  return 1
}

validate_resolution() {
  local res="$1"
  in_array "$res" "${ALLOWED_RESOLUTIONS[@]}" && return 0
  echo "Error: --resolution must be one of: ${ALLOWED_RESOLUTIONS[*]}" >&2
  return 1
}

# Parse a key value for one variable name from a dotenv-style file.
# Supports: leading whitespace, spaces around `=`, "value" / 'value' / value,
#   `#` comment lines, blank lines; takes last occurrence if duplicated.
# NOT supported: shell expansion (${VAR} / $VAR), command substitution ($(...) /
#   backticks), line continuation (\). These are all treated as literal characters.
# Returns empty string if not found.
read_dotenv_var() {
  local file="$1" name="$2"
  [[ -f "$file" ]] || return 0
  grep -E "^[[:space:]]*${name}[[:space:]]*=" "$file" 2>/dev/null \
    | tail -n 1 \
    | sed -E "s/^[[:space:]]*${name}[[:space:]]*=[[:space:]]*//; s/^\"(.*)\"[[:space:]]*\$/\1/; s/^'(.*)'[[:space:]]*\$/\1/; s/[[:space:]]+\$//"
}

# Mask a key for safe display: head4****tail4 (or fewer chars if very short)
mask_key() {
  local k="$1"
  local n=${#k}
  if (( n <= 8 )); then
    printf '%s****' "${k:0:1}"
  else
    printf '%s****%s' "${k:0:4}" "${k: -4}"
  fi
}

# Parallel arrays for the key chain
KEY_VALUES=()
KEY_SOURCES=()
LEGACY_KEY_USED=0

add_key_candidate() {
  local v="$1" src="$2" is_legacy="${3:-0}"
  [[ -z "$v" ]] && return 0
  # Dedup by value to avoid retrying the same key
  local existing
  for existing in ${KEY_VALUES[@]+"${KEY_VALUES[@]}"}; do
    [[ "$existing" == "$v" ]] && return 0
  done
  if [[ "$is_legacy" == "1" ]]; then
    src="$src (${LEGACY_KEY_NAME}, deprecated)"
    [[ ${#KEY_VALUES[@]} -eq 0 ]] && LEGACY_KEY_USED=1
  fi
  KEY_VALUES+=("$v")
  KEY_SOURCES+=("$src")
}

# Add the first of AIHUB_API_KEY / X_API_KEY present in one dotenv file.
add_key_from_file() {
  local file="$1" src="$2" v
  v="$(read_dotenv_var "$file" "$KEY_NAME")"
  if [[ -n "$v" ]]; then
    add_key_candidate "$v" "$src" 0
    return 0
  fi
  v="$(read_dotenv_var "$file" "$LEGACY_KEY_NAME")"
  add_key_candidate "$v" "$src" 1
}

collect_keys() {
  if [[ -n "${AIHUB_API_KEY:-}" ]]; then
    add_key_candidate "$AIHUB_API_KEY" "env $KEY_NAME" 0
  elif [[ -n "${X_API_KEY:-}" ]]; then
    add_key_candidate "$X_API_KEY" "env" 1
  fi

  add_key_from_file "$PWD/.env.local" "$PWD/.env.local"
  add_key_from_file "$PWD/.env" "$PWD/.env"

  if [[ $USE_LOCAL_KEY -eq 1 ]]; then
    add_key_from_file "$HOME/.config/banana-2/.env" "~/.config/banana-2/.env"
  fi

  if [[ $LEGACY_KEY_USED -eq 1 ]]; then
    echo "⚠️ ${LEGACY_KEY_NAME} 已废弃，请改用 ${KEY_NAME}（本次仍按 ${LEGACY_KEY_NAME} 读取，来源：${KEY_SOURCES[0]}）" >&2
  fi
}

# --- arg parsing ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prompt)        PROMPT="${2:-}"; shift 2 ;;
    --model)         MODEL="${2:-}"; shift 2 ;;
    --aspect-ratio)  ASPECT_RATIO="${2:-}"; shift 2 ;;
    --resolution)    RESOLUTION="${2:-}"; shift 2 ;;
    --image-url)     IMAGE_URLS+=("${2:-}"); shift 2 ;;
    --output-format) OUTPUT_FORMAT="${2:-}"; shift 2 ;;
    --google-search) GOOGLE_SEARCH=1; shift ;;
    --image-search)  IMAGE_SEARCH=1; shift ;;
    --poll-interval) POLL_INTERVAL="${2:-}"; shift 2 ;;
    --max-attempts)  MAX_ATTEMPTS="${2:-}"; shift 2 ;;
    --base-url)      BASE_URL="${2:-}"; shift 2 ;;
    --use-local-key) USE_LOCAL_KEY=1; shift ;;
    --output-dir)    OUTPUT_DIR="${2:-}"; shift 2 ;;
    --filename)      FILENAME="${2:-}"; shift 2 ;;
    --label)         LABEL="${2:-}"; shift 2 ;;
    --no-save)       NO_SAVE=1; shift ;;
    -h|--help)       usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

# --- timestamp for default filenames (captured before API call so logs and saved files align) ---
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

# --- validation ---
[[ -z "$PROMPT" ]] && { echo "Error: --prompt is required" >&2; exit 1; }

# --- filename / save dir validation ---
if [[ -n "$FILENAME" ]]; then
  if [[ "$FILENAME" == */* ]]; then
    echo "Error: --filename must be a basename, not a path. Use --output-dir for directory." >&2
    exit 1
  fi
  # Strip a trailing extension if present (we re-append based on format/content-type)
  FILENAME="${FILENAME%.*}"
fi

# Resolve save dir priority: --output-dir > env > $PWD
if [[ -z "$OUTPUT_DIR" ]]; then
  OUTPUT_DIR="${BANANA_2_OUTPUT_DIR:-$PWD}"
fi

in_array "$MODEL" "${ALLOWED_MODELS[@]}" || {
  echo "Error: --model must be one of: ${ALLOWED_MODELS[*]}" >&2; exit 1; }

validate_aspect_ratio "$ASPECT_RATIO" || exit 1
validate_resolution "$RESOLUTION" || exit 1

if [[ -n "$OUTPUT_FORMAT" ]]; then
  in_array "$OUTPUT_FORMAT" "${ALLOWED_OUTPUT_FORMAT[@]}" || {
    echo "Error: --output-format must be one of: ${ALLOWED_OUTPUT_FORMAT[*]}" >&2; exit 1; }
fi

is_positive_int "$POLL_INTERVAL" || { echo "Error: --poll-interval must be a positive integer" >&2; exit 1; }
is_positive_int "$MAX_ATTEMPTS" || { echo "Error: --max-attempts must be a positive integer" >&2; exit 1; }

# --- key chain ---
collect_keys
if [[ ${#KEY_VALUES[@]} -eq 0 ]]; then
  echo "Error: no API key found in any of:" >&2
  echo "  - env $KEY_NAME" >&2
  echo "  - $PWD/.env.local" >&2
  echo "  - $PWD/.env" >&2
  if [[ $USE_LOCAL_KEY -eq 1 ]]; then
    echo "  - ~/.config/banana-2/.env" >&2
  else
    echo "  (~/.config/banana-2/.env skipped; pass --use-local-key to include it)" >&2
  fi
  exit 1
fi

# --- payload assembly ---
IMG_COUNT=${#IMAGE_URLS[@]}
MODE_LABEL="text2img"
(( IMG_COUNT > 0 )) && MODE_LABEL="img2img/editing (${IMG_COUNT} ref)"

echo "Request summary:"
echo "- create endpoint: ${BASE_URL}${CREATE_ENDPOINT}"
echo "- query endpoint: ${BASE_URL}${QUERY_ENDPOINT_PREFIX}/{id}"
echo "- model: $MODEL"
echo "- mode: $MODE_LABEL"
echo "- aspect_ratio: $ASPECT_RATIO"
echo "- resolution: $RESOLUTION"
[[ -n "$OUTPUT_FORMAT" ]] && echo "- output_format: $OUTPUT_FORMAT"
[[ $GOOGLE_SEARCH -eq 1 ]] && echo "- google_search: true"
[[ $IMAGE_SEARCH -eq 1 ]]  && echo "- image_search: true"
echo "- key chain (high → low):"
for idx in "${!KEY_SOURCES[@]}"; do
  echo "    $((idx+1)). ${KEY_SOURCES[$idx]}  ($(mask_key "${KEY_VALUES[$idx]}"))"
done
echo "- poll interval: ${POLL_INTERVAL}s"
echo "- max attempts: $MAX_ATTEMPTS"
if [[ $NO_SAVE -eq 1 ]]; then
  echo "- save: disabled (--no-save)"
else
  if [[ -n "$FILENAME" ]]; then
    echo "- save: ${OUTPUT_DIR}/${FILENAME}.<ext>  (--filename override)"
  else
    if [[ -n "$LABEL" ]]; then
      echo "- save: ${OUTPUT_DIR}/${TIMESTAMP}-$(sanitize_label "$LABEL").<ext>  (timestamp + --label)"
    else
      echo "- save: ${OUTPUT_DIR}/${TIMESTAMP}-$(sanitize_label "$PROMPT").<ext>  (timestamp + auto label from prompt)"
    fi
  fi
fi

IMAGE_URLS_JSON="$(python3 - <<'PY' ${IMAGE_URLS[@]+"${IMAGE_URLS[@]}"}
import json, sys
print(json.dumps(sys.argv[1:], ensure_ascii=False))
PY
)"

PAYLOAD="$(python3 - <<'PY' "$MODEL" "$PROMPT" "$ASPECT_RATIO" "$RESOLUTION" "$OUTPUT_FORMAT" "$GOOGLE_SEARCH" "$IMAGE_SEARCH" "$IMAGE_URLS_JSON"
import json, sys

model, prompt, aspect_ratio, resolution, output_format, google_search, image_search, image_urls_json = sys.argv[1:]
body = {
    "model": model,
    "prompt": prompt,
    "aspect_ratio": aspect_ratio,
    "resolution": resolution,
}

if output_format:
    body["output_format"] = output_format
if google_search == "1":
    body["google_search"] = True
if image_search == "1":
    body["image_search"] = True

image_urls = json.loads(image_urls_json)
if image_urls:
    body["image_urls"] = image_urls

print(json.dumps(body, ensure_ascii=False))
PY
)"

# --- create with 401 fallback ---
USED_KEY=""
USED_SOURCE=""
CREATE_RESPONSE=""
HTTP_CODE=""

for idx in "${!KEY_VALUES[@]}"; do
  k="${KEY_VALUES[$idx]}"
  src="${KEY_SOURCES[$idx]}"
  masked="$(mask_key "$k")"

  echo "[auth] Trying key from: ${src}  (${masked})"

  RAW="$(curl --silent --show-error --location \
      --write-out $'\n%{http_code}' \
      "${BASE_URL}${CREATE_ENDPOINT}" \
      --header 'Content-Type: application/json' \
      --header "Authorization: Bearer ${k}" \
      --data "$PAYLOAD")" || {
    echo "Error: network/curl failure while calling create endpoint with key from $src" >&2
    exit 1
  }

  HTTP_CODE="${RAW##*$'\n'}"
  CREATE_RESPONSE="${RAW%$'\n'$HTTP_CODE}"

  echo "[auth] HTTP ${HTTP_CODE}"

  if [[ "$HTTP_CODE" == "401" ]]; then
    echo "[auth] 401 from ${src}; 401 does not consume credits. Falling back to next key in chain."
    USED_KEY=""
    USED_SOURCE=""
    continue
  fi

  USED_KEY="$k"
  USED_SOURCE="$src"
  break
done

if [[ -z "$USED_KEY" ]]; then
  echo "Error: all configured keys returned HTTP 401 (authentication_error)." >&2
  echo "Last response body:" >&2
  echo "$CREATE_RESPONSE" >&2
  exit 1
fi

echo "[auth] Using key from: ${USED_SOURCE}"
echo "$CREATE_RESPONSE"

if [[ "$HTTP_CODE" != 2* ]]; then
  echo "Error: create endpoint returned HTTP ${HTTP_CODE}." >&2
  exit 1
fi

TASK_ID="$(python3 - <<'PY' "$CREATE_RESPONSE"
import json, sys
raw = sys.argv[1]
try:
    data = json.loads(raw)
except Exception:
    print(""); raise SystemExit(0)
task_id = data.get("id")
print(str(task_id) if task_id else "")
PY
)"

if [[ -z "$TASK_ID" ]]; then
  echo "Error: task id not found in create response; cannot continue polling." >&2
  exit 1
fi

echo "Task created successfully."
echo "Task ID: $TASK_ID"
echo "Start querying until terminal status..."

# --- polling with the successful key ---
attempt=1
while [[ $attempt -le $MAX_ATTEMPTS ]]; do
  QUERY_URL="${BASE_URL}${QUERY_ENDPOINT_PREFIX}/${TASK_ID}?sync_upstream=true"
  QUERY_RESPONSE="$(curl --silent --show-error --location "$QUERY_URL" \
    --header "Authorization: Bearer ${USED_KEY}")"

  STATUS_INFO="$(python3 - <<'PY' "$QUERY_RESPONSE"
import json, sys
raw = sys.argv[1]
try:
    data = json.loads(raw)
except Exception:
    print("unknown|0"); raise SystemExit(0)
status = str(data.get("status") or "unknown").lower()
results = data.get("results") or []
print(f"{status}|{len(results)}")
PY
)"
  STATUS="${STATUS_INFO%|*}"
  RESULT_COUNT="${STATUS_INFO#*|}"
  [[ "$RESULT_COUNT" =~ ^[0-9]+$ ]] || RESULT_COUNT=0

  echo "[Attempt ${attempt}/${MAX_ATTEMPTS}] status=${STATUS} results=${RESULT_COUNT}"

  if [[ "$STATUS" == "completed" ]]; then
    if (( RESULT_COUNT == 0 )); then
      # Upstream race: status flips to "completed" before results are attached.
      # Keep polling until results are populated (or we time out).
      echo "[poll] status=completed but results empty; treating as not-yet-final and continuing to poll"
    else
      echo "Task completed. Final response:"
      echo "$QUERY_RESPONSE"
      download_results "$QUERY_RESPONSE"
      exit 0
    fi
  fi

  if [[ "$STATUS" == "failed" ]]; then
    echo "Task failed. Final response:"
    echo "$QUERY_RESPONSE"
    exit 2
  fi

  if [[ $attempt -lt $MAX_ATTEMPTS ]]; then
    sleep "$POLL_INTERVAL"
  fi
  attempt=$((attempt + 1))
done

echo "Polling timed out after ${MAX_ATTEMPTS} attempts."
echo "Task may still be running. Query manually with task id: $TASK_ID"
echo "curl --location '${BASE_URL}${QUERY_ENDPOINT_PREFIX}/${TASK_ID}?sync_upstream=true' --header 'Authorization: Bearer <YOUR_API_KEY>'"
exit 3
