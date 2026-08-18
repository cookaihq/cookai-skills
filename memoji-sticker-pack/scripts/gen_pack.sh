#!/usr/bin/env bash
# memoji-sticker-pack — 从一张人物照片生成一套 Apple Memoji 风格表情贴纸包。
#
# 本脚本循环调用已安装的 image-2 (gpt-image-2) 的 create_task.sh（生成，复用它的
# key 链、轮询、下载、401 兜底），并调用同目录内置的 upload.py（把本地图/URL/base64
# 上传到 aihubmax 文件接口换成 72h 公网 URL，同一把 AIHUB_API_KEY）。
#
# 参考图统一走「上传取 URL」，不再内联 base64 data URI：所有 image_urls 都是
# aihubmax CDN 链接。这样上游收到的是真实 URL，也不受命令行 ARG_MAX 限制。
#
# 流程：
#   1. 预处理输入照片（缩到 ≤768px）→ 上传得 URL。
#   2. 生成 1 张「基准 Memoji」(base.png)，锁定人物长相与风格。
#   3. base.png（缩到 ≤640px）上传得 URL，作为所有表情的参考（上传 1 次、全套复用）。
#   4. 以基准图 URL 为参考，逐个生成各表情（透明底 PNG）。每次失败生成默认重试一次。
#   5. 写 manifest.json + index.html 画廊。
#
# 成功判定：create_task.sh 退出码 0 且产物文件确实存在（双重判断）。
set -uo pipefail

VERSION="1.3.0"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
UPLOAD_PY="$SCRIPT_DIR/upload.py"

# 本 skill 的 Python 解释器由 uv 按 <skill>/pyproject.toml + uv.lock 钉死，venv
# 落 <skill>/.venv（ADR 0007 §1.4 示例侧）。所有 python 调用一律经这个数组，
# 禁止改回裸 python3——那会按 PATH 解析到系统解释器，cutout.py 的 numpy/Pillow
# 就不在那里。各 .py 顶部还有 bootstrap 兜底，绕过本脚本直接跑也会被拉回同一 venv。
# --no-dev：uv run 会先把项目环境同步到位，默认连 dev 依赖组（pytest 等）一起装。
# 那些只服务本仓测试，不该进终端用户的 <skill>/.venv。
PY=(uv run --project "$SKILL_DIR" --no-dev python)

# ---------------- 默认参数 ----------------
IMAGE=""
NAME="memoji"
OUTDIR=""
MODE="pack"            # pack | single
COUNT=0                # 0 = 全部
RESOLUTION="1024x1024"
EXPRESSIONS_OVERRIDE=""
USE_LOCAL_KEY=0
PLAN_ONLY=0
RETRY=1                # 每次失败生成的重试次数（含基准；用户已授权；0 可关闭）
BASE_URL_REUSE=""      # --base-url：复用已有基准图 URL（跳过基准生成、省一次积分）
STAGGER=0.6            # 并行提交每张之间的间隔秒，避免 429 限流
PER_CALL_POLL=6        # 传给 create_task.sh 的轮询间隔
PER_CALL_MAXATT=75     # 传给 create_task.sh 的最大轮询次数（6s×75≈450s 单张上限）

# aihubmax 网关 base URL：上传接口与生成接口共用同一 host（可用 AIHUBMAX_BASE_URL
# 覆盖，与 create_task.sh 同一约定；已废弃的 FOXAPI_BASE_URL 仍作兜底）。
AIHUBMAX_BASE="${AIHUBMAX_BASE_URL:-${FOXAPI_BASE_URL:-https://api.aihubmax.com}}"
if [[ -z "${AIHUBMAX_BASE_URL:-}" && -n "${FOXAPI_BASE_URL:-}" ]]; then
  echo "⚠️ FOXAPI_BASE_URL 已废弃，请改用 AIHUBMAX_BASE_URL（本次仍按 FOXAPI_BASE_URL 读取）" >&2
fi

# 输入预处理尺寸
PHOTO_MAXPX=768        # 原始照片缩放上限
REF_MAXPX=640          # 基准图作为参考时的缩放上限

# ---------------- 默认 16 表情 ----------------
# 格式： slug|中文标签|英文表情/动作描述
DEFAULT_EXPRESSIONS=(
  "smile|微笑|a warm gentle closed-mouth smile, friendly eyes"
  "grin|大笑|a big happy open smile showing teeth, cheerful"
  "laugh|狂笑|laughing hard, eyes squeezed shut, head tilted back, one hand near the face"
  "cry|大哭|crying loudly, mouth wide open, streams of cartoon tears"
  "tear|流泪|sad and teary-eyed, pouting lips, a single tear rolling down"
  "surprised|惊讶|shocked and surprised, very wide eyes, open mouth, eyebrows raised high"
  "love|比心|making a finger-heart gesture with the hands, affectionate warm smile"
  "thumbsup|点赞|giving a clear thumbs-up with one hand, confident happy smile"
  "heart-eyes|爱心眼|big heart-shaped eyes, in love, hands near the cheeks, blushing"
  "angry|生气|angry, furrowed eyebrows, frowning mouth, tense and grumpy"
  "glare|瞪眼|annoyed unimpressed flat stare, side-eye, deadpan"
  "think|思考|thinking, one hand resting on the chin, looking up pensively"
  "wink|眨眼|playful wink with one eye, tongue slightly out, cheeky smile"
  "eyeroll|翻白眼|rolling the eyes upward, exasperated and done"
  "facepalm|捂脸|facepalming, one hand covering the forehead, embarrassed"
  "ok|OK手势|making an OK hand sign near the face, cheerful relaxed smile"
)

# ---------------- 风格 prompt 片段 ----------------
# 注意：本渠道无法输出真透明，且对"transparent background"会画出假棋盘格纹理。
# 因此统一要求纯绿幕底，再由 cutout.py 抠成透明 PNG。绝不能要 transparent/checkerboard。
STYLE_FRAGMENT="Rendered as an Apple Memoji-style 3D avatar: a smooth rounded cartoon character, glossy soft 3D shading, large expressive eyes, soft studio lighting, head-and-shoulders portrait, centered, facing the camera. Place the character on a SOLID FLAT UNIFORM CHROMA-KEY GREEN background (pure bright green screen, RGB约 0,200,0) — absolutely NO checkerboard, NO transparency pattern, NO gradient, NO scenery, NO cast shadow on the background. No text, no watermark, no border, no extra characters."

usage() {
  cat <<EOF
gen_pack.sh v${VERSION} — Memoji 表情包生成器（编排 image-2）

用法：
  gen_pack.sh --image <路径|URL|dataURI> [选项]
  gen_pack.sh --base-url <已有基准图URL> [选项]

必填其一：
  --image PATH        人物照片：本地路径 / 公网 URL / data URI
  --base-url URL      复用已有基准图，跳过基准生成

选项：
  --name NAME         人物名/包名（影响输出目录与标题），默认 memoji
  --outdir DIR        输出目录，默认 ./memoji-<name>/
  --mode pack|single  pack=整套表情(默认)；single=只出 1 张基准头像
  --count N           只取前 N 个表情（默认全部）
  --resolution WxH    贴纸分辨率，默认 1024x1024
  --expressions STR   覆盖默认表情，格式 "slug:描述;slug:描述;..."（英文描述）
  --no-retry          关闭失败重试（默认每次失败生成重试 1 次）
  --use-local-key     允许 image-2 / memoji-sticker-pack 读取各自 ~/.config/<skill>/.env
  --plan              只打印将要做什么 + 预计调用次数，不真正生成（不消耗积分）
  -h, --help          显示帮助

成本（不复用基准图）：pack 无重试 = 1+N；默认最大 = 2+2N（每次生成最多重试一次）。
EOF
}

# ---------------- 参数解析 ----------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --image)        IMAGE="${2:-}"; shift 2 ;;
    --name)         NAME="${2:-}"; shift 2 ;;
    --outdir)       OUTDIR="${2:-}"; shift 2 ;;
    --mode)         MODE="${2:-}"; shift 2 ;;
    --count)        COUNT="${2:-}"; shift 2 ;;
    --resolution)   RESOLUTION="${2:-}"; shift 2 ;;
    --expressions)  EXPRESSIONS_OVERRIDE="${2:-}"; shift 2 ;;
    --no-retry)     RETRY=0; shift ;;
    --base-url)     BASE_URL_REUSE="${2:-}"; shift 2 ;;
    --use-local-key) USE_LOCAL_KEY=1; shift ;;
    --plan)         PLAN_ONLY=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 1 ;;
  esac
done

# ---------------- 校验 ----------------
[[ -z "$IMAGE" && -z "$BASE_URL_REUSE" ]] && {
  echo "错误：--image 与 --base-url 至少提供一个" >&2
  usage
  exit 1
}
case "$MODE" in pack|single) ;; *) echo "错误：--mode 只能是 pack 或 single" >&2; exit 1 ;; esac
[[ "$COUNT" =~ ^[0-9]+$ ]] || { echo "错误：--count 必须是非负整数" >&2; exit 1; }

# 安全化 name 用于路径
NAME_SLUG="$(printf '%s' "$NAME" | tr -d '/\\:*?"<>|' | tr ' ' '_')"
[[ -z "$NAME_SLUG" ]] && NAME_SLUG="memoji"
[[ -z "$OUTDIR" ]] && OUTDIR="./memoji-${NAME_SLUG}"

# ---------------- 组装表情列表 ----------------
EXPR_LIST=()  # 每项： slug|label|desc
if [[ -n "$EXPRESSIONS_OVERRIDE" ]]; then
  # 解析 "slug:desc;slug:desc"
  IFS=';' read -ra _parts <<< "$EXPRESSIONS_OVERRIDE"
  for p in "${_parts[@]}"; do
    [[ -z "${p// }" ]] && continue
    slug="${p%%:*}"; desc="${p#*:}"
    slug="$(printf '%s' "$slug" | tr -d ' ')"
    [[ -z "$slug" ]] && continue
    EXPR_LIST+=("${slug}|${slug}|${desc}")
  done
else
  EXPR_LIST=("${DEFAULT_EXPRESSIONS[@]}")
fi
# 应用 count
if [[ "$COUNT" -gt 0 && "$COUNT" -lt "${#EXPR_LIST[@]}" ]]; then
  EXPR_LIST=("${EXPR_LIST[@]:0:$COUNT}")
fi

EXPR_N=${#EXPR_LIST[@]}

# ---------------- 调用账本 ----------------
# 基准图复用时不生成基准；pack 仍需生成 N 个表情并上传基准图一次。
BASE_CALLS=1
[[ -n "$BASE_URL_REUSE" ]] && BASE_CALLS=0
EXPR_CALLS=0
[[ "$MODE" == "pack" ]] && EXPR_CALLS=$EXPR_N
TOTAL_CALLS=$((BASE_CALLS + EXPR_CALLS))
MAX_CALLS=$TOTAL_CALLS
[[ $RETRY -gt 0 ]] && MAX_CALLS=$((2 * TOTAL_CALLS))

UPLOAD_CALLS=$BASE_CALLS
[[ "$MODE" == "pack" ]] && UPLOAD_CALLS=$((UPLOAD_CALLS + 1))
NEEDS_IMAGE2=0
NEEDS_UPLOADER=0
[[ $TOTAL_CALLS -gt 0 ]] && NEEDS_IMAGE2=1
[[ $UPLOAD_CALLS -gt 0 ]] && NEEDS_UPLOADER=1

# ---------------- 定位 image-2 的 create_task.sh ----------------
find_image2() {
  local c
  for c in "$HOME"/.claude/skills/image-2*/scripts/create_task.sh; do
    [[ -f "$c" ]] && { printf '%s' "$c"; return 0; }
  done
  return 1
}

# ---------------- 计划模式（不消耗积分） ----------------
if [[ $PLAN_ONLY -eq 1 ]]; then
  echo "PLAN（不消耗积分）："
  echo "- 模式: $MODE"
  echo "- 人物/包名: $NAME"
  echo "- 输出目录: $OUTDIR"
  echo "- 分辨率: $RESOLUTION"
  echo "- 基准 Memoji 生成调用: $BASE_CALLS 次"
  [[ -n "$BASE_URL_REUSE" ]] && echo "- 基准图来源: 复用 $BASE_URL_REUSE"
  if [[ "$MODE" == "pack" ]]; then
    echo "- 表情数: $EXPR_N"
    printf '    '; for e in "${EXPR_LIST[@]}"; do lbl="${e#*|}"; printf '%s ' "${lbl%%|*}"; done; echo
  fi
  echo "- image-2 生成调用总数（无重试）: $TOTAL_CALLS 次"
  [[ $RETRY -gt 0 ]] && echo "- 最大生成调用数（每次失败生成最多重试 1 次）: $MAX_CALLS 次"
  echo "- 文件上传调用: $UPLOAD_CALLS 次（转存到 aihubmax 文件接口；非生成调用）"
  if [[ $NEEDS_IMAGE2 -eq 0 ]]; then
    echo "- image-2: 本次不需要"
  elif CREATE_SH="$(find_image2)"; then
    echo "- 复用生成脚本: $CREATE_SH"
  else
    echo "- ⚠️ 未找到 image-2 的 create_task.sh（请确认 image-2 技能已安装）"
  fi
  if [[ $NEEDS_UPLOADER -eq 0 ]]; then
    echo "- 内置上传: 本次不需要"
  else
    echo "- 内置上传脚本: $UPLOAD_PY"
  fi
  echo "- 上传目标 host: $AIHUBMAX_BASE"
  echo "- ⚠️ 实际运行会消耗 aihubmax 积分。"
  exit 0
fi

# 真正运行：只在本次确实需要生成时确认 image-2 存在
CREATE_SH=""
if [[ $NEEDS_IMAGE2 -eq 1 ]] && ! CREATE_SH="$(find_image2)"; then
  echo "错误：未找到 image-2 的 create_task.sh。" >&2
  echo "本技能依赖 image-2 (gpt-image-2) 技能，请先安装它。" >&2
  exit 1
fi

mkdir -p "$OUTDIR"

# ---------------- 工具函数 ----------------

# 把本地图缩放到临时文件，echo 临时文件路径。 $1=文件 $2=最大边 $3=jpg|png
# 缩放失败则 echo 原图路径兜底。调用方用完后删返回路径（若等于原图路径则别删）。
downscale_to_tmp() {
  local src="$1" maxpx="$2" fmt="$3"
  local tmp out
  tmp="$(mktemp -t memoji.XXXXXX)"
  out="${tmp}.${fmt}"
  if command -v sips >/dev/null 2>&1; then
    if [[ "$fmt" == "png" ]]; then
      sips -s format png -Z "$maxpx" "$src" --out "$out" >/dev/null 2>&1
    else
      sips -s format jpeg -s formatOptions 85 -Z "$maxpx" "$src" --out "$out" >/dev/null 2>&1
    fi
  elif command -v ffmpeg >/dev/null 2>&1; then
    ffmpeg -y -i "$src" -vf "scale='min(${maxpx},iw)':-2" "$out" >/dev/null 2>&1
  else
    cp "$src" "$out"
  fi
  rm -f "$tmp" 2>/dev/null || true
  if [[ -f "$out" ]]; then printf '%s' "$out"; else printf '%s' "$src"; fi
}

# 把 --image（本地路径 / 公网 URL / data URI）统一上传到 aihubmax 文件接口换 URL，
# echo 得到的公网 URL（stdout 只输出 URL，供命令替换捕获；诊断进 .log-upload.txt）。
# 一律转存：连公网 URL 也重新托管，使所有 image_urls 都是 aihubmax CDN 链接。
# 上传失败返回非 0，绝不回退内联 base64。 $1=输入 $2=最大边（本地文件缩图用）$3=fmt
upload_image() {
  local in="$1" maxpx="$2" fmt="$3"
  local ulog="$OUTDIR/.log-upload.txt"
  local url rc tmp="" src_flag src_val
  case "$in" in
    http://*|https://*) src_flag="--url";    src_val="$in" ;;
    data:*)             src_flag="--base64"; src_val="$in" ;;
    *)
      [[ -f "$in" ]] || { echo "错误：找不到图片文件: $in" >&2; return 1; }
      tmp="$(downscale_to_tmp "$in" "$maxpx" "$fmt")"
      src_flag="--file"; src_val="$tmp"
      ;;
  esac
  local args=("$src_flag" "$src_val" --base-url "$AIHUBMAX_BASE")
  [[ $USE_LOCAL_KEY -eq 1 ]] && args+=(--use-local-key)
  url="$("${PY[@]}" "$UPLOAD_PY" "${args[@]}" 2>>"$ulog")"
  rc=$?
  [[ -n "$tmp" && "$tmp" != "$in" ]] && rm -f "$tmp" 2>/dev/null || true
  if [[ $rc -ne 0 || -z "$url" ]]; then
    echo "错误：上传图片失败（日志见 $ulog）：$in" >&2
    return 1
  fi
  printf '%s' "$url"
}

# 查找某个 stem 实际生成的产物文件
produced_file() {
  local stem="$1" f
  for f in "$OUTDIR/$stem".png "$OUTDIR/$stem".webp "$OUTDIR/$stem".jpg "$OUTDIR/$stem".jpeg; do
    [[ -f "$f" ]] && { printf '%s' "$f"; return 0; }
  done
  return 1
}

# 调一次 create_task.sh。 $1=image_url $2=prompt $3=stem $4=logfile
run_one() {
  local img="$1" prompt="$2" stem="$3" log="$4"
  # 注：本账号的 gpt-image-2 渠道不支持 background 参数（返回 422
  # unsupported_advanced_options），故不传 --background；透明/纯净底靠 prompt 表达。
  local args=(
    --prompt "$prompt"
    --image-url "$img"
    --resolution "$RESOLUTION"
    --output-dir "$OUTDIR"
    --filename "$stem"
    --poll-interval "$PER_CALL_POLL"
    --max-attempts "$PER_CALL_MAXATT"
  )
  [[ $USE_LOCAL_KEY -eq 1 ]] && args+=(--use-local-key)
  bash "$CREATE_SH" "${args[@]}" >"$log" 2>&1
}

# 从 create_task.sh 的日志里取结构化错误码。非 --json 模式下它把终态打成
#   Error [<code>]: <message>
# 这一行，是本层能拿到的全部结构化信息（不改用 --json，以免动 image-2 的调用契约
# 和产物落盘方式）。取不到就返回空串，由调用方按「无错误码」处理。
gen_error_code() {
  local log="$1"
  [[ -f "$log" ]] || { printf ''; return 0; }
  sed -n 's/^Error \[\([a-z_]*\)\].*/\1/p' "$log" | tail -n 1
}

# 判断「整任务重跑」是否安全（ADR 0006 规则 2/4）。重跑 = 再发一次计费的生成
# 请求，所以只在**明确没有留下已计费任务、且重跑有可能变好**时才做。
#   可重跑：
#     upstream_failed          上游明确说这次生成失败了；重跑是唯一出路（用户已授权 RETRY）
#     ""（无错误码）           create_task.sh 没打结构化终态：进程崩溃、脚本没找到、
#                              或 partial_success（图生成了但本地保存失败）。这些
#                              都不是「已创建任务在跑」的歧义态，重跑安全。
#   不可重跑：
#     create_transport_error   结果不明（ambiguous）：任务可能已创建并计费
#     poll_timeout             任务可能仍在运行且已计费；重跑等于再买一张
#     query_*                  任务已创建并计费，只是本地没查到终态
#     create_http_error        401/402/422 等确定性失败；429/5xx 已在 image-2 内部
#                              按 ADR 0006 重试过 3 次，本层再跑一次仍是同样结果
#     invalid_arguments / configuration_error / invalid_task_id / internal_error
#                              确定性或本地问题，重跑无意义
# 边界：本层只能看到退出码 + 上面那一行错误码，做不到比这更细的分类。
gen_failure_is_retryable() {
  local code="$1"
  case "$code" in
    upstream_failed|"") return 0 ;;
    *) return 1 ;;
  esac
}

# 生成一张：成功把路径写入全局 PRODUCED 并返回 0。 $1=img $2=prompt $3=stem
PRODUCED=""
generate() {
  local img="$1" prompt="$2" stem="$3"
  local log="$OUTDIR/.log-${stem}.txt"
  local attempt=0 max=$((1 + RETRY)) code=""
  PRODUCED=""
  # 删除可能存在的旧产物，避免误判成功
  rm -f "$OUTDIR/$stem".png "$OUTDIR/$stem".webp "$OUTDIR/$stem".jpg "$OUTDIR/$stem".jpeg 2>/dev/null || true
  while (( attempt < max )); do
    attempt=$((attempt + 1))
    if run_one "$img" "$prompt" "$stem" "$log" && produced_file "$stem" >/dev/null 2>&1; then
      PRODUCED="$(produced_file "$stem")"
      return 0
    fi
    code="$(gen_error_code "$log")"
    echo "    ! ${stem} 第 ${attempt} 次失败（错误码：${code:-无}，日志见 ${log}）" >&2
    if ! gen_failure_is_retryable "${code}"; then
      echo "    ! ${stem} 该失败不重跑：${code} 属确定性失败或结果不明（重跑会再次扣费），交由用户决定" >&2
      return 1
    fi
  done
  return 1
}

# 对刚生成的原图（绿幕底）抠图成透明 PNG。 $1=stem，使用全局 PRODUCED。
# 成功后把最终透明 PNG 路径写入全局 FINAL_PNG。
FINAL_PNG=""
cutout_file() {
  local stem="$1"
  local out="$OUTDIR/${stem}.png"
  local log="$OUTDIR/.log-${stem}.txt"
  if "${PY[@]}" "$SCRIPT_DIR/cutout.py" --in "$PRODUCED" --out "$out" >>"$log" 2>&1; then
    [[ "$PRODUCED" != "$out" ]] && rm -f "$PRODUCED" 2>/dev/null || true
    FINAL_PNG="$out"
  else
    echo "    ! ${stem} 抠图失败，保留原图（带绿底）" >&2
    FINAL_PNG="$PRODUCED"
  fi
}

# ---------------- 第 1 步：基准 Memoji ----------------
echo "=== Memoji 表情包：${NAME} ==="
if [[ -n "$BASE_URL_REUSE" ]]; then
  echo "[基准] 下载并复用已有基准图…"
else
  echo "[1/$([[ "$MODE" == single ]] && echo 1 || echo $((1+EXPR_N)))] 上传照片 + 生成基准 Memoji…"
fi

BASE_PROMPT="Turn the person in the reference photo into a friendly Memoji-style avatar. Preserve their key identifying traits: hairstyle, hair color, skin tone, facial hair, glasses or accessories, and overall vibe. Neutral friendly expression. ${STYLE_FRAGMENT}"

if [[ -n "$BASE_URL_REUSE" ]]; then
  echo "    复用已有基准图 URL（不消耗积分）"
  # 幂等 GET（ADR 0006）：必设超时（连接 15s、整体 120s）；瞬时错误（超时、5xx、
  # 429）交给 curl 自带的 --retry 重试 2 次，退避从 1s 起翻倍。
  # 不能加 --retry-delay：显式给非 0 值会把等待固定成该值并**关闭**指数退避，
  # 与上面这行注释的意图相反。省略该选项（等价于 0）才是 curl 的默认退避。
  if ! curl -sS -L --connect-timeout 15 --max-time 120 --retry 2 \
       -o "$OUTDIR/base_raw" "$BASE_URL_REUSE" || [[ ! -s "$OUTDIR/base_raw" ]]; then
    echo "错误：下载复用基准图失败：$BASE_URL_REUSE" >&2; exit 2
  fi
  PRODUCED="$OUTDIR/base_raw"
else
  echo "    上传输入照片换 URL…"
  INPUT_REF="$(upload_image "$IMAGE" "$PHOTO_MAXPX" jpg)" || {
    echo "错误：输入照片上传失败，无法生成基准 Memoji。日志：$OUTDIR/.log-upload.txt" >&2
    exit 2
  }
  if ! generate "$INPUT_REF" "$BASE_PROMPT" "base"; then
    echo "错误：基准 Memoji 生成失败，无法继续。日志：$OUTDIR/.log-base.txt" >&2
    exit 2
  fi
fi
cutout_file "base"
BASE_FILE="$FINAL_PNG"
echo "    ✓ 基准头像：$BASE_FILE"

# single 模式到此结束
if [[ "$MODE" == "single" ]]; then
  "${PY[@]}" "$SCRIPT_DIR/build_gallery.py" \
    --outdir "$OUTDIR" --name "$NAME" \
    --base "$(basename "$BASE_FILE")" || true
  echo "=== 完成（single 模式）：$OUTDIR ==="
  exit 0
fi

# ---------------- 第 2 步：逐个表情（并行提交） ----------------
# 用基准图（缩小版）作为所有表情的参考，保证同一张脸。
# 单张 gpt-image-2 图生图很慢（分钟级），故并发提交、墙钟≈单张耗时。
# 基准图上传一次得 URL，全套表情复用同一 URL（不再每张内联 base64）。
echo "[上传] 基准图 → aihubmax URL（供所有表情复用）…"
BASE_REF="$(upload_image "$BASE_FILE" "$REF_MAXPX" png)" || {
  echo "错误：基准图上传失败，无法生成表情。日志：$OUTDIR/.log-upload.txt" >&2
  exit 2
}

# 组装每个表情的 stem / prompt / label / slug（按索引对齐）
STEMS=(); PROMPTS=(); LABELS=(); SLUGS=()
idx=1
for entry in "${EXPR_LIST[@]}"; do
  IFS='|' read -r e_slug e_label e_desc <<< "$entry"
  STEMS+=("$(printf '%02d-%s' "$idx" "$e_slug")")
  SLUGS+=("$e_slug"); LABELS+=("$e_label")
  PROMPTS+=("Keep the EXACT same character as in the reference image — identical face shape, hairstyle, hair color, skin tone, facial hair, glasses/accessories and art style. Change ONLY the facial expression and pose to: ${e_desc}. ${STYLE_FRAGMENT}")
  idx=$((idx + 1))
done

# 后台并发提交一批索引，wait 等全部结束
launch_round() {
  local i stem prompt log
  for i in "$@"; do
    stem="${STEMS[$i]}"; prompt="${PROMPTS[$i]}"; log="$OUTDIR/.log-${stem}.txt"
    rm -f "$OUTDIR/$stem".png "$OUTDIR/$stem".webp "$OUTDIR/$stem".jpg "$OUTDIR/$stem".jpeg 2>/dev/null || true
    ( run_one "$BASE_REF" "$prompt" "$stem" "$log" ) &
    sleep "$STAGGER"
  done
  wait
}

collect_failures() {
  FAIL_IDX=()
  local i
  for i in "${!STEMS[@]}"; do
    produced_file "${STEMS[$i]}" >/dev/null 2>&1 || FAIL_IDX+=("$i")
  done
}

# 只挑「重跑安全」的失败项（判据与 gen_failure_is_retryable 同一套）。确定性失败
# 与结果不明的项不再整任务重跑，避免无意义的重复扣费。
collect_retryable_failures() {
  RETRY_IDX=()
  local i stem code
  for i in "${FAIL_IDX[@]}"; do
    stem="${STEMS[$i]}"
    code="$(gen_error_code "$OUTDIR/.log-${stem}.txt")"
    if gen_failure_is_retryable "${code}"; then
      RETRY_IDX+=("$i")
    else
      echo "    ! ${stem} 不重跑：${code}（确定性失败或结果不明，重跑会再次扣费）" >&2
    fi
  done
}

# 第 1 轮：全部
ALL_IDX=()
for i in "${!STEMS[@]}"; do ALL_IDX+=("$i"); done
echo "[并行] 提交 ${#STEMS[@]} 个表情（每张间隔 ${STAGGER}s，单张最长约 $((PER_CALL_POLL*PER_CALL_MAXATT))s）…"
launch_round "${ALL_IDX[@]}"

# 重试一轮失败项（用户已授权）
collect_failures
if [[ $RETRY -gt 0 && ${#FAIL_IDX[@]} -gt 0 ]]; then
  RETRY_IDX=()
  collect_retryable_failures
  if [[ ${#RETRY_IDX[@]} -gt 0 ]]; then
    echo "[并行] 重试 ${#RETRY_IDX[@]} 个失败表情（已排除确定性失败与结果不明项）…"
    launch_round "${RETRY_IDX[@]}"
    collect_failures
  fi
fi

# 抠图 + 汇总
OK_ROWS=()      # slug|label|filename
FAIL_ROWS=()    # slug|label
for i in "${!STEMS[@]}"; do
  if produced_file "${STEMS[$i]}" >/dev/null 2>&1; then
    PRODUCED="$(produced_file "${STEMS[$i]}")"
    cutout_file "${STEMS[$i]}"
    OK_ROWS+=("${SLUGS[$i]}|${LABELS[$i]}|$(basename "$FINAL_PNG")")
    echo "    ✓ ${LABELS[$i]} → $(basename "$FINAL_PNG")"
  else
    FAIL_ROWS+=("${SLUGS[$i]}|${LABELS[$i]}")
    echo "    ✗ ${LABELS[$i]} 跳过"
  fi
done

# ---------------- 第 3 步：画廊 + manifest ----------------
# 把成功项写入临时 TSV 交给 python 生成
ITEMS_TSV="$(mktemp -t memoji-items.XXXXXX)"
if [[ ${#OK_ROWS[@]} -gt 0 ]]; then
  for row in "${OK_ROWS[@]}"; do
    IFS='|' read -r r_slug r_label r_file <<< "$row"
    printf '%s\t%s\t%s\n' "$r_slug" "$r_label" "$r_file" >> "$ITEMS_TSV"
  done
fi

"${PY[@]}" "$SCRIPT_DIR/build_gallery.py" \
  --outdir "$OUTDIR" --name "$NAME" \
  --base "$(basename "$BASE_FILE")" \
  --items "$ITEMS_TSV" || true
rm -f "$ITEMS_TSV" 2>/dev/null || true

# ---------------- 汇总 ----------------
echo ""
echo "=== 完成：$OUTDIR ==="
echo "成功 ${#OK_ROWS[@]}/${EXPR_N} 个表情 + 1 张基准头像"
if [[ ${#FAIL_ROWS[@]} -gt 0 ]]; then
  echo "失败（已跳过）："
  for row in "${FAIL_ROWS[@]}"; do
    IFS='|' read -r slug label <<< "$row"
    echo "  - ${label} (${slug})"
  done
  echo "可针对单个表情单独重跑，例如："
  if [[ -n "$BASE_URL_REUSE" ]]; then
    echo "  bash $0 --base-url '$BASE_URL_REUSE' --name '$NAME' --outdir '$OUTDIR' --expressions '<slug>:<描述>'"
  else
    echo "  bash $0 --image '$IMAGE' --name '$NAME' --outdir '$OUTDIR' --expressions '<slug>:<描述>'"
  fi
fi
echo "画廊：$OUTDIR/index.html"
exit 0
