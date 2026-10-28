"""기술 별칭 사전 — 표기가 달라도 같은 기술이면 같은 이름으로 (TASKS T9, AGENT_DESIGN 4.6)

'비슷한 기술'은 묶지 않는다. LangChain과 LangGraph는 다르고, React와 React Native도 다르다 (DESIGN_DECISIONS D4).
"""

from __future__ import annotations

import re

ALIASES: dict[str, tuple[str, ...]] = {
    "python": ("python", "파이썬"),
    "javascript": ("javascript", "js", "자바스크립트"),
    "typescript": ("typescript", "ts", "타입스크립트"),
    "react": ("react", "react.js", "reactjs", "리액트"),
    "react native": ("react native", "react-native", "리액트 네이티브"),
    "next.js": ("next.js", "nextjs", "넥스트"),
    "node.js": ("node.js", "nodejs"),
    "vue.js": ("vue.js", "vuejs", "vue", "뷰"),
    "django": ("django", "장고"),
    "fastapi": ("fastapi",),
    "spring boot": ("spring boot", "springboot", "스프링 부트"),
    "java": ("java", "자바"),
    "kotlin": ("kotlin", "코틀린"),
    "aws": ("aws", "amazon web services"),
    "docker": ("docker", "도커"),
    "kubernetes": ("kubernetes", "k8s", "쿠버네티스"),
    "sql": ("sql",),
    "mysql": ("mysql",),
    "postgresql": ("postgresql", "postgres"),
    "langgraph": ("langgraph", "랭그래프"),
    "langchain": ("langchain", "랭체인"),
    "rag": ("rag",),
    "mcp": ("mcp",),
    "llm": ("llm",),
    "n8n": ("n8n",),
    "playwright": ("playwright",),
    "git": ("git",),
    "linux": ("linux", "리눅스"),
    "tableau": ("tableau", "태블로"),
    "figma": ("figma", "피그마"),
}

_VARIANT_TO_CANON = {v: canon for canon, variants in ALIASES.items() for v in variants}
# 긴 표기부터 찾는다 ('react native'를 'react'보다 먼저, '자바스크립트'를 '자바'보다 먼저)
_VARIANTS_LONGEST_FIRST = sorted(_VARIANT_TO_CANON, key=len, reverse=True)


def canonical(skill: str) -> str:
    t = re.sub(r"\s+", " ", skill.strip().lower())
    return _VARIANT_TO_CANON.get(t, t)


def _pattern(variant: str) -> re.Pattern:
    if variant.isascii():
        return re.compile(rf"(?<![a-z0-9]){re.escape(variant)}(?![a-z0-9])")
    return re.compile(re.escape(variant))


_PATTERNS = [(_pattern(v), _VARIANT_TO_CANON[v]) for v in _VARIANTS_LONGEST_FIRST]


def skills_in(text: str) -> set[str]:
    """문장에 들어 있는 알려진 기술들 (정규화된 이름)"""
    rest = text.lower()
    found: set[str] = set()
    for pat, canon in _PATTERNS:
        if pat.search(rest):
            found.add(canon)
            rest = pat.sub(" ", rest)
    return found
