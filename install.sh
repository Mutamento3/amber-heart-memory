#!/usr/bin/env bash
# Amber Heart 一键安装与升级。
#
# 首次运行：分别配置嵌入、可选的第一级精排和对话，生成 .env 后构建并启动。
# 升级：保留已有 .env 的模型、地址和维度。
#
# 非交互安装使用以下环境变量。留空的非密钥项用推荐值：
#   AMBER_EMBEDDING_BASE_URL、AMBER_EMBEDDING_API_KEY、AMBER_EMBEDDING_MODEL、
#   AMBER_EMBEDDING_DIMENSIONS（Gemini Embedding 2 默认 1536）
#   AMBER_RERANK_ENABLED（yes/no）、AMBER_RERANK_BASE_URL、AMBER_RERANK_API_KEY、
#   AMBER_RERANK_MODEL、AMBER_RERANK_FORMAT（voyage/cohere）
#   AMBER_CHAT_BASE_URL、AMBER_CHAT_API_KEY、AMBER_LIGHT_MODEL、AMBER_HEAVY_MODEL
#   AMBER_DOMAIN           你的域名，例如 memory.example.com
#   AMBER_HTTPS            caddy（内置自动 HTTPS，默认）或 own（自备 nginx）
#   AMBER_TIMEZONE         默认 Asia/Shanghai
#   AMBER_ADMIN_PASSWORD   网页登录密码（15–256 个字符）
#   PIP_INDEX_URL          可选的 Python 软件源，会写进 .env
# 非交互时，未提供任何精排配置则跳过精排。同一服务可省略后续岗位的 key；换服务时显式填写。
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE=.env
PASSWORD_FILE=/var/lib/memory-admin/password.hash

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\n安装停止：%s\n' "$*" >&2; exit 1; }
interactive() { [ -t 0 ]; }

# 提前检查管理密码长度，免得构建完才发现不合格。
if [ -n "${AMBER_ADMIN_PASSWORD:-}" ] && { [ ${#AMBER_ADMIN_PASSWORD} -lt 15 ] || [ ${#AMBER_ADMIN_PASSWORD} -gt 256 ]; }; then
  die "AMBER_ADMIN_PASSWORD 须为 15–256 个字符"
fi

# ask 变量名 "提示" [默认值] [secret]：已有同名环境变量时直接用，否则交互询问。
ask() {
  local name=$1 prompt=$2 default=${3:-} secret=${4:-} value=${!1:-}
  if [ -z "$value" ]; then
    interactive || { [ -n "$default" ] && value=$default; } || die "缺少 $name（非交互运行时请用环境变量提供）"
    if [ -z "$value" ]; then
      local shown=$prompt
      [ -n "$default" ] && [ -z "$secret" ] && shown="$prompt [$default]"
      if [ -n "$secret" ]; then read -r -s -p "$shown: " value; echo; else read -r -p "$shown: " value; fi
      value=${value:-$default}
    fi
  fi
  [ -n "$value" ] || die "$prompt 不能为空"
  printf -v "$name" '%s' "$value"
}

random_secret() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40; }
# Fernet 主密钥：32 字节随机数的 URL 安全 base64。
fernet_key() { head -c 32 /dev/urandom | base64 | tr -d '\n' | tr '+/' '-_'; }

# set_env 键 值：替换 .env 中已有（或被注释掉的）同名行，没有就追加。值通过环境变量传给 awk，避免转义问题。
set_env() {
  local key=$1
  KEY=$key VALUE=$2 awk '
    BEGIN { done = 0 }
    $0 ~ "^#? *" ENVIRON["KEY"] "=" { if (!done) { print ENVIRON["KEY"] "=" ENVIRON["VALUE"]; done = 1 }; next }
    { print }
    END { if (!done) print ENVIRON["KEY"] "=" ENVIRON["VALUE"] }
  ' "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}
get_env() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1; }

compose() { docker compose "$@"; }

check_docker() {
  command -v docker >/dev/null || die "没有找到 docker，请先安装 Docker（https://docs.docker.com/engine/install/）"
  docker compose version >/dev/null 2>&1 || die "没有找到 docker compose 插件，请安装 Docker Compose v2"
  docker info >/dev/null 2>&1 || die "无法连接 Docker：请用 root 运行，或把当前用户加入 docker 组"
}

# 按服务器内存给出默认上限（实测：1 GB 档；2、4 GB 档为余量建议）。
memory_limits() {
  local total_mb
  total_mb=$(awk '/MemTotal/ { printf "%d", $2 / 1024 }' /proc/meminfo 2>/dev/null || echo 1024)
  # 系统会占掉一部分内存，标称 4 GB 的服务器通常只显示 3.6～3.8 GB。
  if [ "$total_mb" -ge 3400 ]; then echo "1024m 512m"
  elif [ "$total_mb" -ge 1700 ]; then echo "768m 256m"
  else echo "512m 256m"; fi
}

endpoint_host() {
  local authority=${1#*://}
  authority=${authority%%/*}
  printf '%s' "${authority%%:*}"
}

ask_role_key() {
  local destination=$1 prompt=$2 endpoint=$3 previous_endpoint=$4 previous_key=$5
  # A non-interactive omission must not send a key to a different service.
  local current_authority=${endpoint#*://} previous_authority=${previous_endpoint#*://}
  if ! interactive && { [ "${endpoint%%://*}" != "${previous_endpoint%%://*}" ] ||
    [ "${current_authority%%/*}" != "${previous_authority%%/*}" ]; }; then
    previous_key=
  fi
  ask "$destination" "$prompt" "$previous_key" secret
}

configure_models() {
  local embedding_model=gemini-embedding-2 embedding_dimensions=1536
  local rerank_enabled=yes rerank_model=rerank-3 rerank_format=voyage
  local light=deepseek-v4-flash heavy=deepseek-v4-pro previous_key previous_endpoint role

  say "1/3 嵌入"
  ask AMBER_EMBEDDING_BASE_URL "嵌入接口基址（不含 /embeddings）" "https://openrouter.ai/api/v1"
  case "$(endpoint_host "$AMBER_EMBEDDING_BASE_URL")" in
    openrouter.ai) embedding_model=google/gemini-embedding-2 ;;
    api.siliconflow.cn|api.siliconflow.com) embedding_model=BAAI/bge-m3 ;;
    api.openai.com) embedding_model=text-embedding-3-small ;;
  esac
  ask AMBER_EMBEDDING_API_KEY "嵌入 API key" "" secret
  ask AMBER_EMBEDDING_MODEL "嵌入模型名" "$embedding_model"
  case "${AMBER_EMBEDDING_MODEL,,}" in
    *bge-m3*) embedding_dimensions=1024 ;;
  esac
  ask AMBER_EMBEDDING_DIMENSIONS "嵌入维度" "$embedding_dimensions"
  [[ "$AMBER_EMBEDDING_DIMENSIONS" =~ ^[1-9][0-9]{0,3}$ ]] &&
    (( AMBER_EMBEDDING_DIMENSIONS <= 2000 )) || die "嵌入维度须为 1–2000 的整数"
  set_env MEMORY_EMBEDDING_BASE_URL "$AMBER_EMBEDDING_BASE_URL"
  set_env MEMORY_EMBEDDING_API_KEY "$AMBER_EMBEDDING_API_KEY"
  set_env MEMORY_EMBEDDING_MODEL "$AMBER_EMBEDDING_MODEL"
  set_env MEMORY_EMBEDDING_DIMENSIONS "$AMBER_EMBEDDING_DIMENSIONS"

  previous_key=$AMBER_EMBEDDING_API_KEY
  previous_endpoint=$AMBER_EMBEDDING_BASE_URL
  say "2/3 第一级精排（可选）"
  if ! interactive && [ -z "${AMBER_RERANK_BASE_URL:-}${AMBER_RERANK_API_KEY:-}${AMBER_RERANK_MODEL:-}${AMBER_RERANK_FORMAT:-}" ]; then
    rerank_enabled=no
  fi
  ask AMBER_RERANK_ENABLED "启用第一级精排（yes/no）" "$rerank_enabled"
  case "${AMBER_RERANK_ENABLED,,}" in
    yes|y)
      ask AMBER_RERANK_BASE_URL "精排接口基址（不含 /rerank）" "https://api.voyageai.com/v1"
      case "$(endpoint_host "$AMBER_RERANK_BASE_URL")" in
        openrouter.ai) rerank_model=mongodb/rerank-3; rerank_format=cohere ;;
        api.siliconflow.cn|api.siliconflow.com) rerank_model=BAAI/bge-reranker-v2-m3; rerank_format=cohere ;;
        api.cohere.com) rerank_model=rerank-v4.0-pro; rerank_format=cohere ;;
        api.jina.ai) rerank_model=jina-reranker-v2-base-multilingual; rerank_format=cohere ;;
      esac
      ask_role_key AMBER_RERANK_API_KEY "精排 API key（跟嵌入一样就回车，换服务请填该服务的 key）" \
        "$AMBER_RERANK_BASE_URL" "$previous_endpoint" "$previous_key"
      ask AMBER_RERANK_MODEL "精排模型名" "$rerank_model"
      ask AMBER_RERANK_FORMAT "精排格式（voyage/cohere）" "$rerank_format"
      case "$AMBER_RERANK_FORMAT" in voyage|cohere) ;; *) die "精排格式须为 voyage 或 cohere" ;; esac
      set_env MEMORY_DOCUMENT_RERANK_BASE_URL "$AMBER_RERANK_BASE_URL"
      set_env MEMORY_DOCUMENT_RERANK_API_KEY "$AMBER_RERANK_API_KEY"
      set_env MEMORY_DOCUMENT_RERANK_MODEL "$AMBER_RERANK_MODEL"
      set_env MEMORY_DOCUMENT_RERANK_FORMAT "$AMBER_RERANK_FORMAT"
      previous_key=$AMBER_RERANK_API_KEY
      previous_endpoint=$AMBER_RERANK_BASE_URL ;;
    no|n) ;;
    *) die "是否启用第一级精排须填 yes 或 no" ;;
  esac

  say "3/3 对话模型"
  ask AMBER_CHAT_BASE_URL "对话接口基址（不含 /chat/completions）" "https://openrouter.ai/api/v1"
  case "$(endpoint_host "$AMBER_CHAT_BASE_URL")" in
    openrouter.ai) light=deepseek/deepseek-v4-flash; heavy=deepseek/deepseek-v4-pro ;;
    api.deepseek.com) light=deepseek-flash ;;
    api.siliconflow.cn|api.siliconflow.com) light=deepseek-ai/DeepSeek-V4-Flash; heavy=deepseek-ai/DeepSeek-V4-Pro
      say "提示：硅基流动上的写作模型（抽卡、日记、摘要）实测明显偏慢，导入几百条可能要很久；建议改用 DeepSeek 官方或 OpenRouter。" ;;
  esac
  ask_role_key AMBER_CHAT_API_KEY "对话 API key（跟上一个启用的岗位一样就回车，换服务请填该服务的 key）" \
    "$AMBER_CHAT_BASE_URL" "$previous_endpoint" "$previous_key"
  ask AMBER_LIGHT_MODEL "轻任务模型（关键词、第二级精排、回执补判）" "$light"
  ask AMBER_HEAVY_MODEL "写作模型（摘要、日记、抽卡等）" "$heavy"
  set_env MEMORY_UPSTREAM_BASE_URL "$AMBER_CHAT_BASE_URL"
  set_env MEMORY_UPSTREAM_API_KEY "$AMBER_CHAT_API_KEY"
  set_env MEMORY_KEYWORD_MODEL "$AMBER_LIGHT_MODEL"
  set_env MEMORY_FINAL_RERANK_MODEL "$AMBER_LIGHT_MODEL"
  set_env MEMORY_RECEIPT_JUDGE_MODEL "$AMBER_LIGHT_MODEL"
  for role in SUMMARY DIARY EXTRACTION TOPIC_SUMMARY RECENT_NOTES_TIDY; do
    set_env "MEMORY_${role}_MODEL" "$AMBER_HEAVY_MODEL"
  done
  set_env MEMORY_SUMMARY_BACKUP_MODEL "$AMBER_LIGHT_MODEL"
  set_env MEMORY_DIARY_BACKUP_MODEL "$AMBER_LIGHT_MODEL"
}

first_time_config() (
  umask 077
  say "首次安装：生成配置"
  [ -f .env.example ] || die "缺少 .env.example，请在仓库根目录运行"
  ask AMBER_DOMAIN "你的域名（已解析到本服务器，例如 memory.example.com）"
  AMBER_DOMAIN=${AMBER_DOMAIN#https://}; AMBER_DOMAIN=${AMBER_DOMAIN%%/*}
  ask AMBER_HTTPS "HTTPS 方式：caddy=内置自动证书，own=自备 nginx 等反代" "caddy"
  case "$AMBER_HTTPS" in caddy|own) ;; *) die "HTTPS 方式只能是 caddy 或 own" ;; esac
  ask AMBER_TIMEZONE "时区（日记等每日任务按它的凌晨 3 点划分一天）" "Asia/Shanghai"

  ENV_FILE=$(mktemp .env.install.XXXXXX)
  trap 'rm -f -- "$ENV_FILE" "$ENV_FILE.tmp"' EXIT
  cp .env.example "$ENV_FILE"
  set_env MEMORY_DB_ADMIN_PASSWORD "$(random_secret)"
  set_env MEMORY_DB_OWNER_PASSWORD "$(random_secret)"
  set_env MEMORY_DB_APP_PASSWORD "$(random_secret)"
  set_env MEMORY_ACCESS_KEY_MASTER_KEY "$(fernet_key)"
  set_env MEMORY_DOMAIN "$AMBER_DOMAIN"
  set_env MEMORY_WEB_ORIGIN "https://$AMBER_DOMAIN"
  if [ "$AMBER_HTTPS" = caddy ]; then set_env COMPOSE_PROFILES caddy; else set_env COMPOSE_PROFILES ""; fi
  set_env MEMORY_TIMEZONE "$AMBER_TIMEZONE"
  read -r app_limit db_limit <<<"$(memory_limits)"
  set_env MEMORY_APP_MEM_LIMIT "$app_limit"
  set_env MEMORY_DB_MEM_LIMIT "$db_limit"
  if [ -n "${PIP_INDEX_URL:-}" ]; then set_env PIP_INDEX_URL "$PIP_INDEX_URL"; fi
  configure_models
  if grep -q '^[A-Z_]*=<' "$ENV_FILE"; then
    die ".env 里还有未填写的占位符：$(grep -o '^[A-Z_]*=<' "$ENV_FILE" | tr -d '=<' | tr '\n' ' ')"
  fi
  ln -- "$ENV_FILE" .env || die "已有 .env，请核对配置后重新运行"
  rm -- "$ENV_FILE"
  trap - EXIT
  say "已生成 .env（内存上限 程序 $app_limit / 数据库 $db_limit）。请妥善备份它：丢失后访问钥匙需要重新生成。"
)

wait_for_db() {
  local i
  for i in $(seq 1 60); do
    compose exec -T db pg_isready -U postgres -d amber_heart >/dev/null 2>&1 && return 0
    sleep 2
  done
  die "数据库 2 分钟内没有就绪，请运行 docker compose logs db 查看原因"
}

set_admin_password() {
  if compose run --rm --no-deps -T app test -f "$PASSWORD_FILE" >/dev/null 2>&1; then
    return 0
  fi
  say "设置管理网页的登录密码（15–256 个字符，输入时不显示）"
  if [ -n "${AMBER_ADMIN_PASSWORD:-}" ]; then
    printf '%s\n%s\n' "$AMBER_ADMIN_PASSWORD" "$AMBER_ADMIN_PASSWORD" \
      | compose run --rm --no-deps -T app python -m memory_web.password --file "$PASSWORD_FILE"
  elif interactive; then
    local i
    for i in 1 2 3; do
      compose run --rm --no-deps app python -m memory_web.password --file "$PASSWORD_FILE" && return 0
    done
    die "管理密码没有设置成功；重新运行 ./install.sh 即可从这一步继续"
  else
    die "缺少 AMBER_ADMIN_PASSWORD（非交互运行时请用环境变量提供管理密码）"
  fi
}

wait_for_app() {
  local i state
  for i in $(seq 1 45); do
    state=$(docker inspect --format '{{.State.Health.Status}}' "$(compose ps -q app)" 2>/dev/null || true)
    [ "$state" = healthy ] && return 0
    sleep 2
  done
  die "记忆库没有正常启动，请运行 docker compose logs app 查看原因（常见：key 错误、嵌入模型名或维度不对）"
}

main() {
  check_docker
  if [ -f "$ENV_FILE" ]; then
    say "检测到已有 .env，按升级处理（配置保持不变）"
  else
    first_time_config
  fi
  local dimensions timezone day_hour
  dimensions=$(get_env MEMORY_EMBEDDING_DIMENSIONS); timezone=$(get_env MEMORY_TIMEZONE); day_hour=$(get_env MEMORY_DAY_START_HOUR)

  say "构建程序镜像（首次需要几分钟）"
  compose build app
  say "验证模型接口"
  compose run --rm --no-deps -T app python -m memory_web.deploy.runtime verify-models \
    --dimensions "${dimensions:-1024}" || die "请修正 .env 中提示的岗位配置，然后重新运行 ./install.sh"
  say "启动数据库"
  compose up -d db
  wait_for_db
  say "安装或升级数据库结构"
  compose run --rm --no-deps -T app python -m memory_web.deploy.runtime install \
    --dimensions "${dimensions:-1024}" --timezone "${timezone:-Asia/Shanghai}" --day-start-hour "${day_hour:-3}"
  set_admin_password
  say "启动记忆库"
  compose up -d
  wait_for_app

  local origin
  origin=$(get_env MEMORY_WEB_ORIGIN)
  say "完成"
  cat <<EOF
管理网页：$origin
  1. 用刚设置的管理密码登录，填写你和 AI 的名字、选择主题模板，创建记忆空间。
  2. 在"设置 → 访问钥匙"生成钥匙。
  3. 在聊天 App 里新增 OpenAI 兼容服务：地址填 $origin/v1，API Key 填这把访问钥匙（详见 README「接入聊天 App」）。
EOF
  if [ "$(get_env COMPOSE_PROFILES)" = caddy ]; then
    echo "首次访问时 Caddy 会自动申请证书，可能需要等几十秒；如果失败，确认域名已解析到本服务器且 80、443 端口已放行。"
  else
    echo "你选择了自备反代：请把 $origin 反代到 127.0.0.1:$(get_env MEMORY_LOCAL_PORT)（示例见 memory_web/deploy/nginx-subdomain.conf.example）。"
  fi
}

main "$@"
