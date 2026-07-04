# dev-pipeline planner 추가 — 이어서 진행용 프롬프트

> 임시 핸드오프 문서. 구현 시작하면 삭제해도 됨.

repo: dev-pipeline / branch: `claude/dev-pipeline-llm-independence-9m9jhy` (main과 동일, v4.0.0) / 목표 버전 5.0.0(breaking).

## 목표
사용자 goal을 받아 **대화형으로** plan.md를 만드는 **planner**를 추가하고, **spec.md/spec_author를 제거**해 plan을 단일 계약으로 삼는다.

## 확정 설계
1. **planner = 호스트 세션 대화형**(헤드리스 러너 아님). `/dev-pipeline --request "<goal>" [--auto]` 모드에서 새 `dp-planner.md`를 따라: 목표 구체화·레포 탐색·**애매하면 사용자에게 질문/승인**·워크플로 유도·TDD/no-TDD 결정 → plan.md 작성 후 사용자 승인.
2. **plan.md = 상단 config 헤더(```dev-pipeline-config JSON) + body.** body = 기존 spec.md 내용(Mode+근거, Requirements, 검증가능 AC, Interface[TDD], Test Strategy[no-TDD], Examples[illustrative], Out of Scope, Constraints) + 필요시 Claude plan-mode 양식. **testable AC/Interface는 필수 유지.**
3. **spec.md/spec_author 완전 제거** → init이 헤더 뗀 body를 계약으로 downstream(test_implementor/implementor/reviewer)에 전달. body 필수 섹션(Requirements/AC, TDD면 Interface) 결정론 검증 → 없으면 거부(구 INSUFFICIENT 대체).
4. **`--tdd`/`--no-tdd` 플래그 삭제.** 모드 단일원본 = `config.driver.tdd_mode`(헤더가 세팅). `state.tdd_mode` freeze/echo는 유지.
5. **config 헤더 → init이 config.json에 병합** (화이트리스트: `driver.tdd_mode`, `review_block_severity`, `llm.tester/test_implementor/implementor/reviewer` 지침; **runners 제외**). ⚠ `llm.tester.*_instruction`은 테스터가 Bash로 실행하는 셸 명령 → 헤더는 실행영향 untrusted 입력이라 **"runners 제외라 안전"은 틀림**. planning 중 `apply-plan-config --dry-run`으로 config diff+검증을 **사용자에게 승인받는 게이트 필수**(`--auto`도 이 승인은 유지). **validate 통과 후에만 원자적 쓰기**(temp+os.replace+.bak), 변경 키만.
6. **`--request` 부트스트랩**: config 없으면 bootstrap하되 멈추지 말고 planning 진입(planner가 placeholder를 헤더로 채움). project_root는 cwd/git toplevel로 탐색.

## 손댈 파일
- `driver.py`: 플래그 제거, `ROLE_META`에서 `spec_author` 제거, `parse_plan_config`/`apply_plan_config` + `cmd_apply_plan_config(--dry-run/--accept)`, `cmd_init`(헤더 병합+body 파생+구조검증), `__version__=5.0.0`. 헤더 파싱 엄격(첫 콘텐츠, 블록 2개=에러).
- 신규 `agents/skills/dev-pipeline/agents/dp-planner.md`, 삭제 `dp-spec-author.md`.
- `SKILL.md`: "planning(대화형) vs execution(헤드리스)" 재프레이밍, Global Rule 3·10 개정, Step 0에 `--request/--auto/--out`·`--tdd/--no-tdd` 전수 제거. 신규 `states/planning.md`(state 아님, Step0에서만 진입), `init.md` spec 저작 스텝 제거.
- `config.schema.json`/`config.example.json`: `runners.spec_author` 제거. `install.sh`: dp-planner.md 추가·dp-spec-author.md 제거·플래그/Cline heredoc 정리.
- 문서 `AGENTS.md`/`README.md`/`CHANGELOG.md`(5.0.0). `test_driver.py`: 플래그·헤더병합/화이트리스트/원자성/idempotency·body 검증·spec_author 테스트 삭제.

## 꼭 지킬 것 (Fable 적대 리뷰)
- 화이트리스트 보안 주장 정정(사용자 승인이 유일 방어).
- config 쓰기: validate 후만·변경키만·원자적+백업, 실패 시 불변. 헤더 셸명령이 downstream에 안 새게 body 전달.
- 플래그 제거는 전수 grep 체크리스트(SKILL/states/driver HELP/에러문구/README/AGENTS/install.sh/Cline heredoc 등 ~25곳).

## 검증
단위테스트 green + 헤더없는 흐름 비파괴 + Sonnet 실전(헤더 plan으로 init 병합→TDD/no-TDD 완주; 이전 세션의 스크래치 `orch.sh` 하네스 재사용). 커밋 author `noreply@anthropic.com`. main 반영/태그는 사용자 지시 시(태그 push는 프록시 403이라 사용자 직접).
