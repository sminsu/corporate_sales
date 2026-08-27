"""Shared test environment.

semantic_layer.yaml과 골든셋 fixture는 physical table을 card_system. 으로
qualified 해서 적는다. 운영 기본값(DB_SCHEMA="")은 pyathena connection의
schema_name이 card_system이라 prefix를 벗겨 실행하지만, 오프라인 테스트는
fixture 문자열과 그대로 비교하므로 database-qualified 형태로 고정한다.
config 모듈이 import 시점에 env를 읽으므로 어떤 테스트 모듈보다 먼저 설정한다.
"""

import os

os.environ.setdefault("DB_SCHEMA", "card_system")
