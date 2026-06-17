.DEFAULT_GOAL := prepare

.PHONY: help
help: ## Show available make targets.
	@echo "Available make targets:"
	@awk 'BEGIN { FS = ":.*## " } /^[A-Za-z0-9_.-]+:.*## / { printf "  %-20s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

.PHONY: install-prek
install-prek: ## Install prek and repo git hooks.
	@echo "==> Installing prek"
	@uv tool install prek
	@echo "==> Installing git hooks with prek"
	@uv tool run prek install

.PHONY: prepare
prepare: download-deps install-prek ## Sync dependencies for all workspace packages and install prek hooks.
	@echo "==> Syncing dependencies for all workspace packages"
	@uv sync --frozen --all-extras --all-packages

.PHONY: prepare-build
prepare-build: download-deps ## Sync dependencies for releases without workspace sources.
	@echo "==> Syncing dependencies for release builds (no sources)"
	@uv sync --all-extras --all-packages --no-sources

# for pythinker web development
.PHONY: web-back web-front
web-back: ## Start web backend with uvicorn (reload enabled).
	@LOG_LEVEL=DEBUG uv run uvicorn pythinker_code.web.app:create_app --factory --reload --port 5494
web-front: ## Start web frontend (vite dev server).
	@npm --prefix web run dev

# for pythinker dashboard development
.PHONY: dashboard-back dashboard-front
dashboard-back: ## Start dashboard backend with uvicorn (reload enabled).
	@LOG_LEVEL=DEBUG uv run uvicorn pythinker_code.dashboard.app:create_app --factory --reload --port 5495
dashboard-front: ## Start dashboard frontend (vite dev server).
	@npm --prefix dashboard run dev

.PHONY: format format-pythinker-code format-pythinker-core format-pythinker-host format-pythinker-review format-pythinker-sdk format-web
format: format-pythinker-code format-pythinker-core format-pythinker-host format-pythinker-review format-pythinker-sdk format-web ## Auto-format all workspace packages.
format-pythinker-code: ## Auto-format Pythinker Code sources with ruff.
	@echo "==> Formatting Pythinker Code sources"
	@uv run ruff check --fix
	@uv run ruff format
format-pythinker-core: ## Auto-format Pythinker core sources with ruff.
	@echo "==> Formatting Pythinker core sources"
	@uv run --directory packages/pythinker-core ruff check --fix
	@uv run --directory packages/pythinker-core ruff format
format-pythinker-host: ## Auto-format Pythinker Host sources with ruff.
	@echo "==> Formatting Pythinker Host sources"
	@uv run --directory packages/pythinker-host ruff check --fix
	@uv run --directory packages/pythinker-host ruff format
format-pythinker-review: ## Auto-format Pythinker Review sources with ruff.
	@echo "==> Formatting Pythinker Review sources"
	@uv run --directory packages/pythinker-review ruff check --fix
	@uv run --directory packages/pythinker-review ruff format
format-pythinker-sdk: ## Auto-format pythinker-sdk sources with ruff.
	@echo "==> Formatting pythinker-sdk sources"
	@uv run --directory sdks/pythinker-sdk ruff check --fix
	@uv run --directory sdks/pythinker-sdk ruff format
format-web: ## Auto-format web sources with npm run format.
	@echo "==> Formatting web sources"
	@if command -v npm >/dev/null 2>&1; then \
		npm --prefix web run format; \
	else \
		echo "npm not found. Install Node.js (npm) to run web formatting."; \
		exit 1; \
	fi
.PHONY: check check-pythinker-code check-pythinker-core check-pythinker-host check-pythinker-review check-pythinker-sdk check-web
check: check-pythinker-code check-pythinker-core check-pythinker-host check-pythinker-review check-pythinker-sdk check-web ## Run linting and type checks for all packages.
check-pythinker-code: ## Run linting and type checks for Pythinker Code.
	@echo "==> Checking Pythinker Code (ruff + pyright + ty)"
	@uv run ruff check
	@uv run ruff format --check
	@uv run pyright
	@uv run ty check
check-pythinker-core: ## Run linting and type checks for Pythinker core.
	@echo "==> Checking Pythinker core (ruff + pyright + ty; ty is non-blocking)"
	@uv run --directory packages/pythinker-core ruff check
	@uv run --directory packages/pythinker-core ruff format --check
	@uv run --directory packages/pythinker-core pyright
	@uv run --directory packages/pythinker-core ty check || true
check-pythinker-host: ## Run linting and type checks for Pythinker Host.
	@echo "==> Checking Pythinker Host (ruff + pyright + ty; ty is non-blocking)"
	@uv run --directory packages/pythinker-host ruff check
	@uv run --directory packages/pythinker-host ruff format --check
	@uv run --directory packages/pythinker-host pyright
	@uv run --directory packages/pythinker-host ty check || true
check-pythinker-review: ## Run linting and type checks for pythinker-review.
	@echo "==> Checking pythinker-review (ruff + pyright + ty; ty is non-blocking)"
	@uv run --directory packages/pythinker-review ruff check
	@uv run --directory packages/pythinker-review ruff format --check
	@uv run --directory packages/pythinker-review pyright
	@uv run --directory packages/pythinker-review ty check || true
check-pythinker-sdk: ## Run linting and type checks for pythinker-sdk.
	@echo "==> Checking pythinker-sdk (ruff + pyright + ty; ty is non-blocking)"
	@uv run --directory sdks/pythinker-sdk ruff check
	@uv run --directory sdks/pythinker-sdk ruff format --check
	@uv run --directory sdks/pythinker-sdk pyright
	@uv run --directory sdks/pythinker-sdk ty check || true
check-web: ## Run linting and type checks for web.
	@echo "==> Checking web (biome + tsc)"
	@if command -v npm >/dev/null 2>&1; then \
		npm --prefix web run lint && npm --prefix web run typecheck; \
	else \
		echo "npm not found. Install Node.js (npm) to run web checks."; \
		exit 1; \
	fi
.PHONY: test test-pythinker-code test-pythinker-core test-pythinker-host test-pythinker-review test-pythinker-sdk
test: test-pythinker-code test-pythinker-core test-pythinker-host test-pythinker-review test-pythinker-sdk ## Run all test suites.
test-pythinker-code: ## Run Pythinker Code tests.
	@echo "==> Running Pythinker Code tests"
	@uv run pytest tests -vv
	@uv run pytest tests_e2e -vv
test-pythinker-core: ## Run Pythinker core tests (including doctests).
	@echo "==> Running Pythinker core tests"
	@uv run --directory packages/pythinker-core pytest --doctest-modules -vv
test-pythinker-host: ## Run Pythinker Host tests.
	@echo "==> Running Pythinker Host tests"
	@uv run --directory packages/pythinker-host pytest tests -vv
test-pythinker-review: ## Run pythinker-review tests.
	@echo "==> Running pythinker-review tests"
	@uv run --directory packages/pythinker-review pytest tests -vv
test-pythinker-sdk: ## Run pythinker-sdk tests.
	@echo "==> Running pythinker-sdk tests"
	@uv run --directory sdks/pythinker-sdk pytest tests -vv

.PHONY: cov cov-pythinker-code cov-pythinker-core cov-pythinker-host cov-pythinker-sdk
cov: cov-pythinker-code cov-pythinker-core cov-pythinker-host cov-pythinker-sdk ## Run all test suites with coverage.
cov-pythinker-code: ## Run Pythinker Code tests with coverage.
	@echo "==> Running Pythinker Code tests with coverage"
	@uv run pytest tests tests_e2e \
		--cov --cov-report=xml:coverage.xml --cov-report=term-missing -vv
cov-pythinker-core: ## Run Pythinker Core tests with coverage.
	@echo "==> Running Pythinker Core tests with coverage"
	@uv run --directory packages/pythinker-core pytest --doctest-modules \
		--cov --cov-report=xml:coverage.xml -vv
cov-pythinker-host: ## Run Pythinker Host tests with coverage.
	@echo "==> Running Pythinker Host tests with coverage"
	@uv run --directory packages/pythinker-host pytest tests \
		--cov --cov-report=xml:coverage.xml -vv
cov-pythinker-sdk: ## Run Pythinker SDK tests with coverage.
	@echo "==> Running Pythinker SDK tests with coverage"
	@uv run --directory sdks/pythinker-sdk pytest tests \
		--cov --cov-report=xml:coverage.xml -vv

.PHONY: build build-pythinker-code build-pythinker-core build-pythinker-host build-pythinker-review build-pythinker-sdk build-bin build-bin-onedir
build: build-web build-dashboard build-pythinker-code build-pythinker-core build-pythinker-host build-pythinker-review build-pythinker-sdk ## Build Python packages for release.
build-pythinker-code: build-web build-dashboard ## Build the pythinker-code sdist and wheel.
	@echo "==> Building pythinker-code distributions"
	@uv build --package pythinker-code --no-sources --out-dir dist
build-pythinker-core: ## Build the pythinker-core sdist and wheel.
	@echo "==> Building pythinker-core distributions"
	@uv build --package pythinker-core --no-sources --out-dir dist/pythinker-core
build-pythinker-host: ## Build the pythinker-host sdist and wheel.
	@echo "==> Building pythinker-host distributions"
	@uv build --package pythinker-host --no-sources --out-dir dist/pythinker-host
build-pythinker-review: ## Build the pythinker-review sdist and wheel.
	@echo "==> Building pythinker-review distributions"
	@uv build --package pythinker-review --no-sources --out-dir dist/pythinker-review
build-pythinker-sdk: ## Build the pythinker-sdk sdist and wheel.
	@echo "==> Building pythinker-sdk distributions"
	@uv build --package pythinker-sdk --no-sources --out-dir dist/pythinker-sdk
build-web: ## Build web UI and sync into pythinker-code package.
	@echo "==> Building web UI"
	@uv run scripts/build_web.py
build-dashboard: ## Build dashboard UI and sync into pythinker-code package.
	@echo "==> Building dashboard UI"
	@uv run scripts/build_dashboard.py
build-bin: build-web build-dashboard ## Build the standalone executable with PyInstaller (one-file mode).
	@echo "==> Building PyInstaller binary (one-file)"
	@uv run pyinstaller pythinker.spec
	@mkdir -p dist/onefile
	@if [ -f dist/pythinker.exe ]; then mv dist/pythinker.exe dist/onefile/; elif [ -f dist/pythinker ]; then mv dist/pythinker dist/onefile/; fi
build-bin-onedir: build-web build-dashboard ## Build the standalone executable with PyInstaller (one-dir mode).
	@echo "==> Building PyInstaller binary (one-dir)"
	@rm -rf dist/onedir dist/pythinker
	@PYINSTALLER_ONEDIR=1 uv run pyinstaller pythinker.spec
	@if [ -f dist/pythinker/pythinker-exe.exe ]; then mv dist/pythinker/pythinker-exe.exe dist/pythinker/pythinker.exe; elif [ -f dist/pythinker/pythinker-exe ]; then mv dist/pythinker/pythinker-exe dist/pythinker/pythinker; fi
	@mkdir -p dist/onedir && mv dist/pythinker dist/onedir/
.PHONY: ai-test
ai-test: ## Run the test suite with Pythinker Code.
	@echo "==> Running AI test suite"
	@uv run tests_ai/scripts/run.py tests_ai

.PHONY: gen-changelog gen-docs
gen-changelog: ## Generate changelog with Pythinker Code.
	@echo "==> Generating changelog"
	@uv run pythinker --yolo --prompt /skill:gen-changelog
gen-docs: ## Generate user docs with Pythinker Code.
	@echo "==> Generating user docs"
	@uv run pythinker --yolo --prompt /skill:gen-docs

include src/pythinker_code/deps/Makefile
