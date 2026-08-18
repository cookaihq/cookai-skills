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
# the chain. 401 does not consume credits.
#
# 网络抖动处理（ADR 0006）：
# - 每个 curl 都带 --connect-timeout / --max-time（create 60s、轮询 30s、下载 300s）。
# - 轮询与下载是幂等 GET：瞬时失败（网络错误 / 5xx / 429 / 408）重试 3 次、退避
#   1s+2s；轮询的瞬时失败只消耗一次轮询预算，不会杀死脚本。
# - 轮询除 MAX_ATTEMPTS 次数预算外还压一条墙钟预算（MAX_ATTEMPTS × POLL_INTERVAL，
#   默认 90 × 8s = 720s）：次数制不管每轮实际耗时，内层重试与慢响应会把总时长拖到
#   远超预期，墙钟预算到期即按终态收场并给出 task_id。
# - create 是计费写操作、无幂等键：429（服务端明确拒绝、未创建、不扣费）按
#   Retry-After 或退避安全重试；5xx 仅在响应体能确认「未创建」时重试；连接中断 /
#   超时属结果不明（ambiguous），不重试，直接报出并给查询指引。
# - 确定性 4xx（402/422/404 等）立即报错，不重试。

KEY_NAME="AIHUB_API_KEY"
LEGACY_KEY_NAME="X_API_KEY"
BASE_URL="${AIHUBMAX_BASE_URL:-https://api.aihubmax.com}"
CREATE_ENDPOINT="/v1/images/generations"
QUERY_ENDPOINT_PREFIX="/v1/tasks"

# skill 根目录（scripts/ 的上一层）。本脚本内所有 Python heredoc 都以
# `uv run --project "${SKILL_DIR}" python` 启动，把「用哪个解释器」钉死在
# <skill>/pyproject.toml + uv.lock 上，而不是 PATH 上碰到的那个 python3
# （ADR 0007 §1.4）。venv 缺失时 uv run 会按 uv.lock 自动创建。
SCRIPTS_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname -- "${SCRIPTS_DIR}")"

# --- 网络超时与重试参数（ADR 0006 §1/§3）---
# 超时按操作类型分档；重试总尝试 3 次（首次 + 2 次重试），退避 1s、2s。
CREATE_CONNECT_TIMEOUT=15
CREATE_MAX_TIME=60
POLL_CONNECT_TIMEOUT=10
POLL_MAX_TIME=30
DOWNLOAD_CONNECT_TIMEOUT=20
DOWNLOAD_MAX_TIME=300
NET_MAX_ATTEMPTS=3
RETRY_AFTER_CAP=60   # 服务端 Retry-After 的采信上限，避免被要求睡到超时预算之外

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

# --- Python 运行时钉死（ADR 0007 §1.4）---
# uv 是系统级依赖：缺失或版本过低只报错并给出可直接执行的命令，不擅自安装
# （ADR 0007 §4.2）。检查只做一次。
UV_CHECKED=0
require_uv() {
  if [[ ${UV_CHECKED} -eq 1 ]]; then
    return 0
  fi
  if ! command -v uv >/dev/null 2>&1; then
    echo "Error: 未安装 uv（本脚本的 Python 由 uv 钉死到 ${SKILL_DIR}/.venv）。" >&2
    echo "请执行：curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
  fi
  local ver major minor
  ver="$(uv --version 2>&1 | awk '{print $2}')"
  major="${ver%%.*}"
  minor="${ver#*.}"
  minor="${minor%%.*}"
  if [[ ! "${major}" =~ ^[0-9]+$ ]] || [[ ! "${minor}" =~ ^[0-9]+$ ]]; then
    echo "Error: 无法解析 uv 版本（uv --version 输出: ${ver}）。请执行：uv self update" >&2
    exit 1
  fi
  if [[ ${major} -eq 0 ]] && [[ ${minor} -lt 8 ]]; then
    echo "Error: uv 版本过低（需 >= 0.8，当前 ${ver}）。请执行：uv self update" >&2
    exit 1
  fi
  UV_CHECKED=1
}

# 本脚本内所有 Python heredoc 的唯一入口。venv 缺失时 uv run 会按 uv.lock 自动
# 创建（ADR 0007 §4.1），因此调用方不需要先手工建环境。
# --no-dev：uv run 的这次自动同步默认连 dev 依赖组（pytest 等）一起装，那些只服务
# 本仓测试，不该进终端用户的 <skill>/.venv。
py() {
  require_uv
  uv run --project "${SKILL_DIR}" --no-dev python "$@"
}

# --- 网络重试工具（ADR 0006 §2/§3）---
# 退避：第 1 次重试前等 1s，第 2 次等 2s。
backoff_seconds() {
  local retry_index="$1"   # 1 表示第 1 次重试
  local s=1 i=1
  while [[ ${i} -lt ${retry_index} ]]; do
    s=$((s * 2))
    i=$((i + 1))
  done
  printf '%s' "${s}"
}

# HTTP 状态码是否属瞬时（ADR 0006 §2：5xx / 429 / 408 可重试；确定性 4xx 不重试）
is_transient_http_code() {
  local code="$1"
  case "${code}" in
    5*|429|408) return 0 ;;
    *) return 1 ;;
  esac
}

# 从 curl 落盘的响应头里取 Retry-After（秒）。取不到或非法时打印空串。
# 只认整数秒形式；HTTP-date 形式与非法值回落到指数退避（打印空串）。
# 超过 RETRY_AFTER_CAP 时**钳到上限**，而不是丢弃后回落到 1s 起的指数退避——
# 上游明确要求等更久，回落成更短的等待只会立刻再撞一次限流。
retry_after_seconds() {
  local header_file="$1"
  local v
  [[ -f "${header_file}" ]] || return 0
  v="$(grep -i '^retry-after:' "${header_file}" 2>/dev/null | tail -n 1 \
       | sed -E 's/^[Rr]etry-[Aa]fter:[[:space:]]*//; s/[[:space:]]*$//' || true)"
  [[ "${v}" =~ ^[0-9]+$ ]] || return 0
  # 位数先兜一道：20 位数字直接进 [[ -gt ]] 会撞 bash 的整数溢出，判出错误结果。
  if [[ ${#v} -gt 9 ]] || [[ "${v}" -gt ${RETRY_AFTER_CAP} ]]; then
    printf '%s' "${RETRY_AFTER_CAP}"
  else
    printf '%s' "${v}"
  fi
}

# Sanitize a label for use in a filename. Keeps Unicode (including CJK), strips
# filesystem-unsafe characters, trims whitespace, takes the first 10 code
# points. Empty string is allowed.
sanitize_label() {
  local raw="$1"
  py - "$raw" <<'PY'
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

# 下载一张结果图。GET 幂等，重试安全（ADR 0006 §4）：瞬时失败（curl 网络错误 /
# 超时 / 5xx / 429 / 408）按 §3 重试至多 NET_MAX_ATTEMPTS 次并打重试日志；确定性
# 4xx（403 签名过期、404 已失效等）立即放弃，不做无谓重试。
download_with_retry() {
  local url="$1" target="$2"
  local attempt=1 http_code reason wait_s
  while [[ ${attempt} -le ${NET_MAX_ATTEMPTS} ]]; do
    reason=""
    if http_code="$(curl --silent --show-error --location \
        --connect-timeout "${DOWNLOAD_CONNECT_TIMEOUT}" \
        --max-time "${DOWNLOAD_MAX_TIME}" \
        --write-out '%{http_code}' --output "${target}" "${url}")"; then
      if [[ "${http_code}" == 2* ]]; then
        return 0
      fi
      if ! is_transient_http_code "${http_code}"; then
        echo "[save] Error: download failed for ${url} (HTTP ${http_code}，确定性错误，不重试)" >&2
        rm -f "${target}"
        return 1
      fi
      reason="HTTP ${http_code}"
    else
      reason="curl 网络错误或超时"
    fi
    rm -f "${target}"
    if [[ ${attempt} -ge ${NET_MAX_ATTEMPTS} ]]; then
      break
    fi
    wait_s="$(backoff_seconds "${attempt}")"
    echo "[retry] 下载第 ${attempt}/${NET_MAX_ATTEMPTS} 次尝试失败（${reason}），${wait_s}s 后重试: ${url}" >&2
    sleep "${wait_s}"
    attempt=$((attempt + 1))
  done
  echo "[save] Error: download failed for ${url}（已尝试 ${NET_MAX_ATTEMPTS} 次，最后原因: ${reason}）" >&2
  return 1
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
  pairs="$(py - "$query_response" <<'PY'
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
    if ! download_with_retry "$url" "$target"; then
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

IMAGE_URLS_JSON="$(py - <<'PY' ${IMAGE_URLS[@]+"${IMAGE_URLS[@]}"}
import json, sys
print(json.dumps(sys.argv[1:], ensure_ascii=False))
PY
)"

PAYLOAD="$(py - <<'PY' "$MODEL" "$PROMPT" "$ASPECT_RATIO" "$RESOLUTION" "$OUTPUT_FORMAT" "$GOOGLE_SEARCH" "$IMAGE_SEARCH" "$IMAGE_URLS_JSON"
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

# 单次 create 调用的结果（create 是计费写操作，无幂等键，故按 ADR 0006 §4 严格
# 区分「服务端明确拒绝、未创建」与「结果不明」两类失败）。
CREATE_HTTP_CODE=""
CREATE_BODY=""
CREATE_RETRY_AFTER=""
CREATE_CURL_FAILED=0

create_once() {
  local key="$1"
  local header_file raw
  CREATE_HTTP_CODE=""
  CREATE_BODY=""
  CREATE_RETRY_AFTER=""
  CREATE_CURL_FAILED=0
  # 显式给模板路径：`mktemp -t 前缀` 在 macOS 可用，GNU coreutils 会因模板缺少
  # XXXXXX 直接报错，这里两边都能跑。
  header_file="$(mktemp "${TMPDIR:-/tmp}/banana2-create-headers.XXXXXX")"
  if raw="$(curl --silent --show-error --location \
      --connect-timeout "${CREATE_CONNECT_TIMEOUT}" \
      --max-time "${CREATE_MAX_TIME}" \
      --dump-header "${header_file}" \
      --write-out $'\n%{http_code}' \
      "${BASE_URL}${CREATE_ENDPOINT}" \
      --header 'Content-Type: application/json' \
      --header "Authorization: Bearer ${key}" \
      --data "${PAYLOAD}")"; then
    CREATE_HTTP_CODE="${raw##*$'\n'}"
    CREATE_BODY="${raw%$'\n'$CREATE_HTTP_CODE}"
    CREATE_RETRY_AFTER="$(retry_after_seconds "${header_file}")"
  else
    CREATE_CURL_FAILED=1
  fi
  rm -f "${header_file}"
}

# 5xx 时判断能否确认「任务未创建」：响应体必须能解析成 JSON、带 error 且不带任务
# id，才算服务端明确拒绝，可安全重试；否则按结果不明处理，不盲重试。
create_body_confirms_rejected() {
  local body="$1" verdict
  verdict="$(py - "$body" <<'PY'
import json, sys
try:
    data = json.loads(sys.argv[1])
except Exception:
    print("0"); raise SystemExit(0)
if isinstance(data, dict) and data.get("error") and not data.get("id"):
    print("1")
else:
    print("0")
PY
)"
  [[ "${verdict}" == "1" ]]
}

# 结果不明（ambiguous）：请求已发出但没拿到可判定的响应，任务可能已创建并计费。
# 按 ADR 0006 §4 不盲重试，把状态明确报给调用方并给出查询指引。
fail_create_ambiguous() {
  local reason="$1"
  echo "Error: create 接口${reason}，本次调用结果不明（ambiguous）。" >&2
  echo "任务可能已在服务端创建并已计费，脚本不会自动重试以免重复扣费。" >&2
  echo "请先确认是否已产生任务，再决定是否重新提交：" >&2
  echo "  1) 打开 https://aihubmax.com 查看任务/用量记录，拿到 task_id；" >&2
  echo "  2) 用 task_id 查询终态：" >&2
  echo "     curl --location '${BASE_URL}${QUERY_ENDPOINT_PREFIX}/<task_id>?sync_upstream=true' --header 'Authorization: Bearer <YOUR_API_KEY>'" >&2
  exit 1
}

for idx in "${!KEY_VALUES[@]}"; do
  k="${KEY_VALUES[$idx]}"
  src="${KEY_SOURCES[$idx]}"
  masked="$(mask_key "$k")"

  echo "[auth] Trying key from: ${src}  (${masked})"

  attempt=1
  while :; do
    create_once "$k"

    if [[ ${CREATE_CURL_FAILED} -eq 1 ]]; then
      fail_create_ambiguous "连接中断或超时（未收到响应，key 来源 ${src}）"
    fi

    HTTP_CODE="${CREATE_HTTP_CODE}"
    CREATE_RESPONSE="${CREATE_BODY}"
    echo "[auth] HTTP ${HTTP_CODE}"

    # 429：服务端明确拒绝、任务未创建、不消耗积分 → 安全重试（有 Retry-After 时
    # 遵循它，否则指数退避）。ADR 0006 §2/§3。
    if [[ "${HTTP_CODE}" == "429" ]] && [[ ${attempt} -lt ${NET_MAX_ATTEMPTS} ]]; then
      if [[ -n "${CREATE_RETRY_AFTER}" ]]; then
        wait_s="${CREATE_RETRY_AFTER}"
        why="HTTP 429，遵循 Retry-After"
      else
        wait_s="$(backoff_seconds "${attempt}")"
        why="HTTP 429，指数退避"
      fi
      echo "[retry] create 第 ${attempt}/${NET_MAX_ATTEMPTS} 次尝试被限流（${why}），${wait_s}s 后重试（429 未创建任务、不扣费）" >&2
      sleep "${wait_s}"
      attempt=$((attempt + 1))
      continue
    fi

    # 5xx：只有响应体能确认「服务端拒绝、未创建」才重试；否则结果不明，停下。
    if [[ "${HTTP_CODE}" == 5* ]]; then
      if create_body_confirms_rejected "${CREATE_RESPONSE}"; then
        if [[ ${attempt} -lt ${NET_MAX_ATTEMPTS} ]]; then
          wait_s="$(backoff_seconds "${attempt}")"
          echo "[retry] create 第 ${attempt}/${NET_MAX_ATTEMPTS} 次尝试失败（HTTP ${HTTP_CODE}，响应体确认未创建任务），${wait_s}s 后重试" >&2
          sleep "${wait_s}"
          attempt=$((attempt + 1))
          continue
        fi
      else
        echo "Last response body:" >&2
        echo "${CREATE_RESPONSE}" >&2
        fail_create_ambiguous "返回 HTTP ${HTTP_CODE} 且响应体无法确认任务是否已创建"
      fi
    fi

    break
  done

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

TASK_ID="$(py - <<'PY' "$CREATE_RESPONSE"
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
# 单次轮询查询：GET 幂等，重试安全（ADR 0006 §4）。瞬时失败在这一层内重试至多
# NET_MAX_ATTEMPTS 次，全部失败也只让本轮拿不到状态，不会杀死脚本、不会击穿
# MAX_ATTEMPTS 总预算（ADR 0006 §5）。确定性 4xx 立即报错退出（ADR 0006 §2）。
# 返回：0 = 拿到 2xx 响应；1 = 本轮瞬时失败已耗尽重试；2 = 确定性失败。
# rc=0/2 时 stdout 为「HTTP 状态码 + 换行 + 响应体 + 换行 + 哨兵」（本函数在命令替换
# 的子 shell 里执行，赋值全局变量传不回来，所以结果一律走 stdout）。
#
# 尾哨兵是必需的，不是装饰：`$( )` 会剥掉命令替换结果末尾的所有换行。响应体为空
# 时（上游 502 只给状态码、连接被中途截断等），"200\n" 会被剥成 "200"，读取侧再按
# 换行拆就拆不出两段——状态码会被当成响应体喂给 json.loads，得到 int 200，随后
# data.get(...) 抛 AttributeError，整个脚本连同已创建的 task_id 一起崩掉。
# 末尾固定跟一个哨兵后，需要保留的那个换行不再位于字符串末尾，剥不掉；读取侧
# 去掉哨兵即可还原出准确的空响应体。
POLL_OUTPUT_SENTINEL="__BANANA2_POLL_EOF__"

poll_once() {
  local url="$1"
  local net_attempt=1 raw code body reason wait_s
  while [[ ${net_attempt} -le ${NET_MAX_ATTEMPTS} ]]; do
    reason=""
    if raw="$(curl --silent --show-error --location \
        --connect-timeout "${POLL_CONNECT_TIMEOUT}" \
        --max-time "${POLL_MAX_TIME}" \
        --write-out $'\n%{http_code}' \
        "${url}" \
        --header "Authorization: Bearer ${USED_KEY}")"; then
      code="${raw##*$'\n'}"
      body="${raw%$'\n'$code}"
      if [[ "${code}" == 2* ]]; then
        printf '%s\n%s\n%s' "${code}" "${body}" "${POLL_OUTPUT_SENTINEL}"
        return 0
      fi
      if ! is_transient_http_code "${code}"; then
        printf '%s\n%s\n%s' "${code}" "${body}" "${POLL_OUTPUT_SENTINEL}"
        return 2
      fi
      reason="HTTP ${code}"
    else
      reason="curl 网络错误或超时"
    fi
    if [[ ${net_attempt} -ge ${NET_MAX_ATTEMPTS} ]]; then
      break
    fi
    wait_s="$(backoff_seconds "${net_attempt}")"
    echo "[retry] 查询第 ${net_attempt}/${NET_MAX_ATTEMPTS} 次尝试失败（${reason}），${wait_s}s 后重试" >&2
    sleep "${wait_s}"
    net_attempt=$((net_attempt + 1))
  done
  echo "[retry] 本轮查询 ${NET_MAX_ATTEMPTS} 次尝试均失败（最后原因: ${reason}），按未拿到状态继续下一轮轮询" >&2
  return 1
}

# 轮询墙钟预算（ADR 0006 §5）。MAX_ATTEMPTS 只数轮次、不管每轮实际耗时：每轮内层
# 最多 3 次尝试、退避 1s+2s，加上单次查询 30s 上限，90 轮理论上能拖到远超预期的
# 时长。因此在次数制之外再压一条真墙钟上限 = MAX_ATTEMPTS × POLL_INTERVAL
# （默认 90 × 8s = 720s），随两个参数一起缩放；内层重试也不得突破它。
POLL_BUDGET_SECONDS=$((MAX_ATTEMPTS * POLL_INTERVAL))
POLL_START_SECONDS=${SECONDS}
POLL_BUDGET_EXCEEDED=0

attempt=1
while [[ $attempt -le $MAX_ATTEMPTS ]]; do
  if [[ $((SECONDS - POLL_START_SECONDS)) -ge ${POLL_BUDGET_SECONDS} ]]; then
    POLL_BUDGET_EXCEEDED=1
    break
  fi
  QUERY_URL="${BASE_URL}${QUERY_ENDPOINT_PREFIX}/${TASK_ID}?sync_upstream=true"
  POLL_RC=0
  POLL_OUT="$(poll_once "$QUERY_URL")" || POLL_RC=$?
  # 先摘掉尾哨兵，再拆「状态码 + 响应体」；响应体为空时这一步保住了那个分隔换行。
  POLL_OUT="${POLL_OUT%$'\n'"${POLL_OUTPUT_SENTINEL}"}"
  POLL_CODE="${POLL_OUT%%$'\n'*}"
  QUERY_RESPONSE="${POLL_OUT#*$'\n'}"
  if [[ "${QUERY_RESPONSE}" == "${POLL_CODE}" ]] && [[ "${POLL_OUT}" != *$'\n'* ]]; then
    # 没有换行可拆 = 只有状态码没有响应体，响应体按空串处理，绝不把状态码当 JSON。
    QUERY_RESPONSE=""
  fi

  if [[ ${POLL_RC} -eq 2 ]]; then
    echo "Error: 查询接口返回 HTTP ${POLL_CODE}（确定性错误，重试无意义）。" >&2
    echo "Last response body:" >&2
    echo "${QUERY_RESPONSE}" >&2
    echo "任务已创建，Task ID: ${TASK_ID}；修好原因（如 key 权限）后可手工查询：" >&2
    echo "  curl --location '${QUERY_URL}' --header 'Authorization: Bearer <YOUR_API_KEY>'" >&2
    exit 1
  fi

  if [[ ${POLL_RC} -ne 0 ]]; then
    # 本轮瞬时失败已耗尽重试：消耗一次轮询预算后继续，不放弃整个轮询。
    echo "[Attempt ${attempt}/${MAX_ATTEMPTS}] status=unreachable results=0"
    if [[ $attempt -lt $MAX_ATTEMPTS ]]; then
      sleep "$POLL_INTERVAL"
    fi
    attempt=$((attempt + 1))
    continue
  fi

  STATUS_INFO="$(py - <<'PY' "$QUERY_RESPONSE"
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

if [[ ${POLL_BUDGET_EXCEEDED} -eq 1 ]]; then
  echo "Polling stopped after the ${POLL_BUDGET_SECONDS}s wall-clock budget was spent (attempt ${attempt}/${MAX_ATTEMPTS})."
else
  echo "Polling timed out after ${MAX_ATTEMPTS} attempts."
fi
echo "Task may still be running. Query manually with task id: $TASK_ID"
echo "curl --location '${BASE_URL}${QUERY_ENDPOINT_PREFIX}/${TASK_ID}?sync_upstream=true' --header 'Authorization: Bearer <YOUR_API_KEY>'"
exit 3
