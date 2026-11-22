# vanilla-agent —— 仓库级任务入口（docs/plan.md §11 / T0.4）。
#
# 约定：
# - 一切 Python 命令走 uv，且必须 cd 进 service/（pyproject / uv.lock /
#   pytest 的 pythonpath=["."] 都在那里；在仓库根直接跑会静默收不到测试）。
# - 实现属于后续里程碑的目标带守卫，指名所属 TODO 而不是假装通过：
#     config-check    由 T1.6 完整实现；在 service/app/__main__.py 存在前
#                     打印 SKIP 并 exit 0 —— lint 串了它，落地当天自动
#                     变成硬门槛，无需再改 Makefile。
#     gen-ts          由 T5.3 提供（scripts/gen_typescript.py），此前报错退出。
#     sandbox-build   由 T6.3 提供（sandbox/image/Dockerfile），此前报错退出。
#     dev             API 进程在 app/main.py（T5.2）出现后自动启用，
#                     前端进程在 web/package.json（T5.4）出现后自动启用。

UV ?= uv

# 与 service/app/core/env.py 的 DEFAULT_SANDBOX_IMAGE 保持一致；T6.3 落地时
# 由 settings.sandbox.image_tag 重新校验（docs/plan.md §11）。
SANDBOX_IMAGE ?= vanilla-agent/sandbox:latest

.PHONY: help lint format test dev config-check sandbox-build gen-ts

help:
	@echo "lint           ruff check + ruff format --check + pyright + config-check"
	@echo "test           pytest -q（容忍'尚无测试'的退出码 5；tests/ 非空后以真实结果判定）"
	@echo "dev            运行已就绪的进程：uvicorn :8000 与 next dev :3000（守卫见上）"
	@echo "config-check   配置校验 + 密钥扫描（T1.6；未落地时 SKIP）"
	@echo "sandbox-build  构建沙箱镜像（T6.3；未落地时如实报错）"
	@echo "gen-ts         事件 DTO -> TypeScript 类型（T5.3；未落地时如实报错）"

lint:
	cd service && $(UV) run ruff check .
	cd service && $(UV) run ruff format --check .
	cd service && $(UV) run pyright
	$(MAKE) --no-print-directory config-check

format:
	cd service && $(UV) run ruff format .
	cd service && $(UV) run ruff check --fix .

test:
	cd service && $(UV) run pytest -q || [ $$? -eq 5 ]

config-check:
	@if (cd service && $(UV) run python -c "import app.__main__") >/dev/null 2>&1; then \
		cd service && $(UV) run python -m app.config check; \
	else \
		echo "SKIP config-check: service/app/config CLI 尚未实现（由 T1.6 提供；存在后本目标自动启用）"; \
	fi

gen-ts:
	@test -f scripts/gen_typescript.py || { echo "gen-ts: scripts/gen_typescript.py 尚不存在（由 T5.3 提供）" >&2; exit 1; }
	cd service && $(UV) run python ../scripts/gen_typescript.py

sandbox-build:
	@test -f sandbox/image/Dockerfile || { echo "sandbox-build: sandbox/image/Dockerfile 尚不存在（由 T6.3 提供）" >&2; exit 1; }
	docker build -t $(SANDBOX_IMAGE) sandbox/image

dev:
	@pids=""; \
	if (cd service && $(UV) run python -c "import app.main") >/dev/null 2>&1; then \
		echo "dev: api -> uvicorn app.main:app --reload :8000"; \
		(cd service && $(UV) run uvicorn app.main:app --reload --port 8000) & pids="$$pids $$!"; \
	else \
		echo "dev: API 入口 app/main.py 尚不存在（由 T5.2 提供），跳过该进程"; \
	fi; \
	if [ -f web/package.json ]; then \
		echo "dev: web -> next dev :3000"; \
		(cd web && npm run dev) & pids="$$pids $$!"; \
	else \
		echo "dev: web/ 脚手架尚不存在（由 T5.4 提供），跳过该进程"; \
	fi; \
	if [ -n "$$pids" ]; then \
		trap 'kill $$pids 2>/dev/null || true' INT TERM EXIT; \
		wait; \
	fi
