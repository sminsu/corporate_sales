"""로컬 개발용 PII 마스커 스텁.

실제 구현은 보안망에 있는 같은 이름의 파일이며, 배포 시 이 파일을 대체한다.
계약은 ``mask_pii(text) -> str`` 하나뿐이므로 시그니처를 바꾸면 안 된다.
여기 정규식은 실제 탐지 범위와 다르니 마스킹 품질을 이 파일로 판단하지 말 것.
"""

from __future__ import annotations

import re

_PATTERNS = (
    (re.compile(r"(?<!\d)\d{6}-[1-4]\d{6}(?!\d)"), "[주민등록번호]"),
    (re.compile(r"(?<!\d)01[016-9]-?\d{3,4}-?\d{4}(?!\d)"), "[전화번호]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[이메일]"),
)


def mask_pii(text: str) -> str:
    masked = str(text or "")
    for pattern, token in _PATTERNS:
        masked = pattern.sub(token, masked)
    return masked
