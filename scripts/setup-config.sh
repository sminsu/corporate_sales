#!/usr/bin/env bash
#
# 로컬 설정 파일(.local / .env.local)을 템플릿(.example)에서 생성한다.
#
# .local 류 파일은 비밀/환경별 값을 담아 git에 올리지 않으므로(.gitignore), clone 직후나
# 새 서버 배포 시 이 스크립트로 한 번 생성한 뒤 실제 값을 채워 넣는다.
#
# 이미 존재하는 파일은 절대 덮어쓰지 않는다 (실제 설정 보호). 덮어쓰려면 --force.
#
# 사용법:
#   ./scripts/setup-config.sh           # 없는 .local 파일만 생성
#   ./scripts/setup-config.sh --force   # 기존 파일을 .bak 으로 백업 후 새로 생성

set -euo pipefail

# 스크립트 위치 기준으로 프로젝트 루트를 잡는다 (어디서 실행하든 동작).
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

FORCE=0
if [[ "${1:-}" == "--force" ]]; then
  FORCE=1
fi

# "템플릿:대상" 쌍. 대상이 없을 때만 템플릿을 복사한다.
PAIRS=(
  ".env.example:.env.local"
  "config/agent.example.yaml:config/agent.local.yaml"
  "config/models.example.yaml:config/models.local.yaml"
)

created=0
skipped=0

for pair in "${PAIRS[@]}"; do
  src="${pair%%:*}"
  dst="${pair##*:}"

  if [[ ! -f "$src" ]]; then
    echo "  [skip] 템플릿 없음: $src"
    continue
  fi

  if [[ -f "$dst" ]]; then
    if [[ "$FORCE" -eq 1 ]]; then
      cp "$dst" "$dst.bak"
      echo "  [backup] $dst -> $dst.bak"
    else
      echo "  [keep] 이미 존재: $dst (덮어쓰려면 --force)"
      skipped=$((skipped + 1))
      continue
    fi
  fi

  cp "$src" "$dst"
  # agent 설정은 로컬 model registry를 가리키도록 참조를 바꿔준다.
  if [[ "$dst" == "config/agent.local.yaml" ]]; then
    # macOS/BSD sed 호환을 위해 백업 확장자 인자를 명시한다.
    sed -i.tmp 's#model_registry_path: models.example.yaml#model_registry_path: models.local.yaml#' "$dst"
    rm -f "$dst.tmp"
  fi
  echo "  [create] $dst  (from $src)"
  created=$((created + 1))
done

echo ""
echo "완료: 생성 ${created}개, 유지 ${skipped}개"
echo ""
echo "다음 값들을 환경에 맞게 채워 넣으세요:"
echo "  - .env.local"
echo "      * LLM_API_KEY              : model registry의 api_key_env가 참조하는 키"
echo "      * ATHENA_S3_STAGING_DIR, ATHENA_REGION, DB_SCHEMA 등 + AWS 자격증명"
echo "      * (세션 저장용 postgres) KBCARD_POSTGRES_DSN 또는 WEBAPP_SESSION_STORE=memory"
echo "  - config/models.local.yaml : LLM endpoint/model 확인"
echo ""
echo "임베딩 서버(로컬)는 다음으로 띄웁니다:"
echo "  uv run uvicorn embedding_server:app --app-dir .local --host 0.0.0.0 --port 8124"
