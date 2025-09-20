# core/yaml_edit.py
from __future__ import annotations

import re

import io
import os
import time
import difflib
from pathlib import Path
from typing import Tuple, Any

# --- YAML backend: ruamel (с сохранением форматирования) -> fallback на PyYAML ---
_HAS_RUAMEL = True
try:
    from ruamel.yaml import YAML  # type: ignore
    _yaml = YAML()
    _yaml.preserve_quotes = True
    _yaml.width = 4096
    _yaml.indent(mapping=2, sequence=2, offset=2)
except Exception:
    import yaml as _pyyaml  # type: ignore
    _HAS_RUAMEL = False

# --- ПУБЛИЧНЫЕ API ---

def load_yaml_preserve(path: str | os.PathLike) -> Tuple[Any, str]:
    """
    Читает YAML, возвращает (data, original_text).
    При наличии ruamel.yaml — сохраняет комменты/кавычки.
    """
    p = Path(path)
    text = ""
    if p.exists():
        text = p.read_text(encoding="utf-8", errors="replace")

    if _HAS_RUAMEL:
        data = _yaml.load(text or "")  # CommentedMap/CommentedSeq
        if data is None:
            data = {}
    else:
        data = _pyyaml.safe_load(text) or {}

    return data, text


def dump_yaml_preserve(data: Any) -> str:
    """
    Преобразует объект YAML обратно в текст.
    С ruamel сохраняет форматирование; иначе — аккуратный safe_dump.
    """
    if _HAS_RUAMEL:
        buf = io.StringIO()
        _yaml.dump(data, buf)
        return buf.getvalue()
    else:
        return _pyyaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def make_unified_diff(old_text: str, new_text: str, filename: str) -> str:
    """
    Возвращает unified diff (как текст). Если изменений нет — возвращает "".
    """
    if old_text == new_text:
        return ""
    a = old_text.splitlines(keepends=True)
    b = new_text.splitlines(keepends=True)
    diff = difflib.unified_diff(a, b, fromfile=f"{filename} (old)", tofile=f"{filename} (new)")
    return "".join(diff)


def atomic_write_with_backup(path: str | os.PathLike, new_text: str) -> str | None:
    """
    Безопасная запись:
      - если файл существовал — создаёт .bak с timestamp
      - пишет во временный файл и атомарно заменяет
    Возвращает путь к .bak (или None, если исходника не было).
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    backup_path = None
    if p.exists():
        ts = time.strftime("%Y%m%d_%H%M%S")
        backup_path = str(p.with_suffix(p.suffix + f".bak.{ts}"))
        try:
            Path(backup_path).write_bytes(p.read_bytes())
        except Exception:
            backup_path = None

    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, p)  # атомарная замена (где возможно)
    return backup_path


# --- Утилита "показать diff и (по желанию) сохранить" для CLI ---

def preview_and_write_yaml(path: str, new_data: Any, *, auto_yes: bool = False) -> tuple[bool, str | None]:
    """
    Удобный хелпер для CLI:
      - читает текущий YAML
      - строит diff
      - показывает diff (если есть) и спрашивает подтверждение (если auto_yes=False)
      - пишет файл атомарно + создаёт .bak
    Возвращает (saved, backup_path_or_None)
    """
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.prompt import Confirm
    except Exception:
        Console = None
        Confirm = None
        Panel = None

    data_old, old_text = load_yaml_preserve(path)
    new_text = dump_yaml_preserve(new_data)
    diff = make_unified_diff(old_text, new_text, Path(path).name)

    # Если нет изменений — молча выходим
    if not diff:
        if Console:
            console = Console()
            console.print(Panel.fit(f"[dim]{path} — изменений нет[/dim]", border_style="grey50"))
        return False, None

    if Console:
        console = Console()
        console.print(Panel.fit(diff if len(diff) < 50_000 else (diff[:50_000] + "\n... [diff trimmed]"),
                                title=f"DIFF • {path}", border_style="cyan"))

    proceed = True if auto_yes else (Confirm.ask("Сохранить изменения?", default=True) if Confirm else True)
    if not proceed:
        if Console:
            console.print(Panel.fit("❌ Отменено. Файл не изменён.", border_style="red"))
        return False, None

    backup = atomic_write_with_backup(path, new_text)

    if Console:
        msg = f"✅ Сохранено: {path}" + (f"\n[dim]backup: {backup}[/dim]" if backup else "")
        console.print(Panel.fit(msg, border_style="green"))

    return True, backup
# === NL → ops для CI (Шаг 4) ===
def build_ops_from_nl(kind: str, features_text: str) -> list[dict]:
    """
    Строит список операций для apply_ops по естественному описанию.
    Поддерживаем шаблоны: python, node, go, java, rust, dotnet, docker.
    """
    ops: list[dict] = []
    text = (" " + (features_text or "") + " ").lower()

    def has(*words: str) -> bool:
        tokens = re.split(r"[\s\+\.,;]+", (features_text or "").lower().strip())
        return any(word.lower() in tokens for word in words)

    k = (kind or "").strip().lower()
    if k == "докер":
        k = "docker"

        # --- Root env: "env CI=true VERSION=123"
      # --- Root env: "env CI=true VERSION=123"
    if " env " in (" " + (features_text or "") + " ").lower():
        try:
            tail = features_text.split("env", 1)[1].strip()
        except Exception:
            tail = ""
        kv = {}
        for tok in tail.split():
            if "=" in tok:
                k_, v_ = tok.split("=", 1)
                k_ = k_.strip()
                v_ = v_.strip().strip('"').strip("'")
                if k_:
                    kv[k_] = v_
        if kv:
            ops.append({"op": "set_root_env", "value": kv})


       # -------- Python ----------
    if kind == "python":
        wants_black = has("black")
        wants_cov = has("coverage", "покрытие", "коверидж")

        # ----- TEST -----
        ops.append({
            "op": "set_run",
            "step": {"name": "test"},
            "value": "pytest -q || true"
        })

        # ----- LINT -----
        if wants_black:
            # если шаг lint уже есть → правим, иначе создаём перед test
            ops.append({
                "op": "set_run",
                "step": {"name": "lint"},
                "value": "black --check ."
            })
            ops.append({
                "op": "insert_before",
                "step": {"name": "test"},
                "name": "lint",
                "value": {"run": "black --check .", "target": "auto"}
            })

        # ----- COVERAGE -----
        if wants_cov:
            ops.append({
                "op": "set_run",
                "step": {"name": "coverage"},
                "value": "coverage run -m pytest && coverage report"
            })
            ops.append({
                "op": "insert_after",
                "step": {"name": "test"},
                "name": "coverage",
                "value": {"run": "coverage run -m pytest && coverage report", "target": "auto"}
            })

    # -------- Node ----------
    # -------- Node.js ----------
    elif kind == "node":
    # Флаги по запросу пользователя
        wants_eslint = has("eslint", "линт", "lint")
        wants_cov = has("coverage", "покрытие", "коверидж", "cover")
        wants_docker = has("docker", "докер", "docker hub", "push", "деплой", "deploy")

        # --- LINT ---
        if wants_eslint:
            ops.append({
                "op": "insert_before",
                "step": {"name": "test"},   # если нет test — фоллбек вставит в начало
                "name": "lint",
                "value": {
                    "run": "npm install --save-dev eslint && npx eslint .",
                    "target": "auto"
                }
            })

        # --- TEST ---
        ops.append({
            "op": "set_run",
            "step": {"name": "test"},
            "value": "npm ci && npm test --silent"
        })

        # --- COVERAGE ---
        if wants_cov:
            ops.append({
                "op": "insert_after",
                "step": {"name": "test"},
                "name": "coverage",
                "value": {
                    "run": "npm run coverage --if-present || echo 'no coverage script'",
                    "target": "auto",
                    "needs": ["test"]
                }
            })

        # --- DOCKER ---
        if wants_docker:
            ops.append({
                "op": "insert_after",
                "step": {"name": "test"},
                "name": "build_image",
                "value": {
                    "run": "docker build -t myapp:latest .",
                    "target": "host",
                    "needs": ["test"]
                }
            })
            ops.append({
                "op": "insert_after",
                "step": {"name": "build_image"},
                "name": "push_image",
                "value": {
                    "run": "docker push myapp:latest",
                    "target": "host",
                    "needs": ["build_image"]
                }
            })

        # -------- Go ----------
    elif kind == "go":
        # Делаем coverage ПОСЛЕДНИМ insert_after → он окажется СРАЗУ после 'test'
        coverage_op = None

        # Линтеры:
        # - golangci-lint (если явно попросили)
        # - иначе строгая проверка форматирования (gofmt) как "линтер чек"
                # Линтеры:
        # Go-шаблон не содержит шага 'lint' → создаём его ПЕРЕД 'build'
        if has("golangci", "golangci-lint"):
            ops.append({
                "op": "insert_before",
                "step": {"name": "build"},   # если build нет — apply_ops вставит в начало (мягкий фоллбек)
                "name": "lint",
                "value": {
                    "run": "golangci-lint run",
                    "target": "auto"
                }
            })
        elif has("gofmt", "fmt", "format"):
            ops.append({
                "op": "insert_before",
                "step": {"name": "build"},
                "name": "lint",
                "value": {
                    "run": 'test -z "$(gofmt -l .)" || (gofmt -l .; exit 1)',
                    "target": "auto"
                }
            })


        # Тесты:
        # По умолчанию — go test ./...
        # Если попросили -race → добавим флаг
        # (quiet не нужен: у go по умолчанию тихий режим; -v — уже «шумный»)
        base_test = "go test ./..."
        if has("race", "-race"):
            base_test = "go test -race ./..."
        if has("bench", "benchmark"):
            # если вдруг пользователь попросил бенчи — добавим как основной тест-ран
            base_test = "go test -bench=. ./..."
        ops.append({
            "op": "set_run",
            "step": {"name": "test"},
            "value": base_test
        })

        # Docker — build/push после test
        if has("docker", "докер", "docker hub", "push", "пуш", "деплой", "deploy"):
            ops.append({
                "op": "insert_after",
                "step": {"name": "test"},
                "name": "build_image",
                "value": {
                    "run": "docker build -t myapp:latest .",
                    "target": "host",
                    "needs": ["test"]
                }
            })
            ops.append({
                "op": "insert_after",
                "step": {"name": "build_image"},
                "name": "push_image",
                "value": {
                    "run": "docker push myapp:latest",
                    "target": "host",
                    "needs": ["build_image"]
                }
            })

        # Coverage — в самом конце, чтобы оказаться ближе всех к 'test'
        if has("coverage", "покрытие", "коверидж", "cover"):
            coverage_op = {
                "op": "insert_after",
                "step": {"name": "test"},
                "name": "coverage",
                "value": {
                    "run": "go test -coverprofile=coverage.out ./... && go tool cover -func=coverage.out && go tool cover -html=coverage.out -o coverage.html",
                    "target": "auto",
                    "needs": ["test"]
                }
            }
        if coverage_op:
            ops.append(coverage_op)

    # -------- Java (Maven) ----------
    elif kind == "java":
    # 1) детектируем gradle/gradlew
        uses_gradle = has("gradle", "gradlew", "градл")

        # 2) маленький хелпер для выбора команды
        def j(cmd_gradle: str, cmd_mvn: str) -> str:
            return cmd_gradle if uses_gradle else cmd_mvn

        # 3) НОРМАЛИЗАЦИЯ базовых шагов build/test под выбранный тулчейн
        ops.append({"op": "set_run", "step": {"name": "build"}, "value": j("./gradlew -q assemble", "mvn -B -ntp -DskipTests package")})
        ops.append({"op": "set_run", "step": {"name": "test"},  "value": j("./gradlew -q test",      "mvn -B -ntp test")})
        ops.append({
            "op": "set_run",
            "step": {"name": "setup"},
            "value": j(
                # Gradle-ветка:
                "./gradlew -q --no-daemon tasks || true",
                # Maven-ветка (как у тебя):
                "mvn -B -ntp -DskipTests dependency:resolve || true"
            )
        })


        # ----- ENV (root) -----
        # (универсально для всех языков — но оставляем на всякий случай и в java-блоке)
        # уже делается выше: set_root_env при "env KEY=VAL ..." — если у тебя общий блок есть, можно убрать отсюда

        # ----- LINT -----
        # Собираем несколько линтеров в один шаг 'lint' через && (если в фразе одновременно checkstyle + spotbugs + pmd)
        lint_cmds = []
        if has("checkstyle", "чекстайл"):
            lint_cmds.append(j(
                "./gradlew checkstyleMain checkstyleTest",
                "mvn -B -ntp checkstyle:check"
            ))
        if has("spotbugs", "findbugs", "спотбагс"):
            # Maven: официальный goal плагина
            lint_cmds.append(j(
                "./gradlew spotbugsMain spotbugsTest",
                "mvn -B -ntp com.github.spotbugs:spotbugs-maven-plugin:check"
            ))
        if has("pmd", "пмд"):
            lint_cmds.append(j(
                "./gradlew pmdMain pmdTest",
                "mvn -B -ntp pmd:check"
            ))

        if lint_cmds:
            ops.append({
                "op": "insert_before",
                "step": {"name": "test"},   # если build отсутствует, apply_ops вставит в начало
                "name": "lint",
                "value": {
                    "run": " && ".join(lint_cmds),
                    "target": "auto"
                }
            })

        # ----- TEST -----
        # Если явно попросили тесты/юнит — переопределим команду теста
        if has("test", "tests", "unit", "junit", "тест"):
            ops.append({
                "op": "set_run",
                "step": {"name": "test"},
                "value": j("./gradlew test", "mvn -B -ntp test")
            })

        # ----- COVERAGE (JaCoCo) -----
        # Для Gradle: нужен плагин 'jacoco' в build.gradle; для Maven — плагин jacoco в pom.xml.
        # Мы генерим стандартные команды; если плагина нет, шаг упадёт (это ок — видно во время прогона).
        if has("coverage", "jacoco", "коверидж", "джакоко"):
            ops.append({
                "op": "insert_after",
                "step": {"name": "test"},
                "name": "coverage",
                "value": {
                    "run": j(
                        "./gradlew test jacocoTestReport",
                        "mvn -B -ntp -DskipTests=false test jacoco:report"
                    ),
                    "target": "auto",
                    "needs": ["test"]
                }
            })

        # ----- DOCKER (build → push) -----
        if has("docker", "докер", "docker hub", "push", "деплой", "deploy"):
            ops.append({
                "op": "insert_after",
                "step": {"name": "test"},
                "name": "build_image",
                "value": {
                    "run": "docker build -t myapp:latest .",
                    "target": "host",
                    "needs": ["test"]
                }
            })
            ops.append({
                "op": "insert_after",
                "step": {"name": "build_image"},
                "name": "push_image",
                "value": {
                    "run": "docker push myapp:latest",
                    "target": "host",
                    "needs": ["build_image"]
                }
            })

    # -------- Rust ----------
    
    elif kind == "rust":
        # Детект хотелок из фразы
        wants_fmt = has("fmt", "rustfmt", "format", "формат")
        wants_clippy = has("clippy", "lint", "линт")
        wants_cov = has("coverage", "коверидж", "llvm-cov", "grcov", "tarpaulin")
        wants_tarpaulin = has("tarpaulin")
        wants_docker = has("docker", "докер", "docker hub", "push", "пуш", "деплой", "deploy")
        quiet_tests = has(" -q", "quiet", "тихо")

        # ----- setup/build/test (нормализации) -----
        # setup — подтягиваем зависимости; при покрытии ставим нужные инструменты
        setup_cmd = "cargo fetch"
        if wants_cov and not wants_tarpaulin:
            # llvm-cov по умолчанию
            setup_cmd += " && rustup component add llvm-tools-preview || true"
            setup_cmd += " && cargo install cargo-llvm-cov --locked || true"
        elif wants_cov and wants_tarpaulin:
            setup_cmd += " && cargo install cargo-tarpaulin --locked || true"
        ops.append({"op": "set_run", "step": {"name": "setup"}, "value": setup_cmd})

        # build — релизная сборка
        ops.append({"op": "set_run", "step": {"name": "build"}, "value": "cargo build --release"})

        # test — поддержка -q
        test_cmd = "cargo test -q" if quiet_tests else "cargo test"
        ops.append({"op": "set_run", "step": {"name": "test"}, "value": test_cmd})

        # ----- LINT (создаём новый шаг 'lint' перед build) -----
        lint_cmds = []
        if wants_fmt:
            lint_cmds.append("cargo fmt --all -- --check")
        if wants_clippy or (has("lint", "линт") and not wants_fmt):
            lint_cmds.append("cargo clippy --workspace --all-features -- -D warnings")
        if lint_cmds:
            ops.append({
                "op": "insert_before",
                "step": {"name": "build"},
                "name": "lint",
                "value": {
                    "run": " && ".join(lint_cmds),
                    "target": "auto"
                }
            })

        # ----- COVERAGE (после test) -----
        if wants_cov:
            if wants_tarpaulin:
                cov_run = "cargo tarpaulin --workspace --out Xml"
            else:
                # llvm-cov по умолчанию + lcov-отчёт (под CI/Codecov)
                cov_run = (
                    "cargo llvm-cov --all-features --workspace --summary-only "
                    "&& cargo llvm-cov --all-features --workspace --lcov --output-path lcov.info"
                )
            ops.append({
                "op": "insert_after",
                "step": {"name": "test"},
                "name": "coverage",
                "value": {
                    "run": cov_run,
                    "target": "auto",
                    "needs": ["test"]
                }
            })

        # ----- DOCKER (build → push) -----
        if wants_docker:
            ops.append({
                "op": "insert_after",
                "step": {"name": "test"},
                "name": "build_image",
                "value": {
                    "run": "docker build -t myapp:latest .",
                    "target": "host",
                    "needs": ["test"]
                }
            })
            ops.append({
                "op": "insert_after",
                "step": {"name": "build_image"},
                "name": "push_image",
                "value": {
                    "run": "docker push myapp:latest",
                    "target": "host",
                    "needs": ["build_image"]
                }
            })


    # -------- .NET ----------
    elif kind == "dotnet":
        wants_format = has("format", "fmt", "dotnet format", "линт", "lint", "style", "аналайзер")
        wants_cov = has("coverage", "coverlet", "коверидж")
        wants_docker = has("docker", "докер", "docker hub", "push", "пуш", "деплой", "deploy")
        quiet_tests = has(" -q", "quiet", "тихо")

        # БАЗОВЫЕ шаги (нормализация): ВАЖНО — здесь 'restore', а не 'setup'
        ops.append({"op": "set_run", "step": {"name": "restore"}, "value": "dotnet restore --nologo"})
        ops.append({"op": "set_run", "step": {"name": "build"}, "value": "dotnet build --configuration Release --nologo"})
        test_cmd = "dotnet test --no-build --nologo -v quiet" if quiet_tests else "dotnet test --no-build --nologo -v minimal"
        ops.append({"op": "set_run", "step": {"name": "test"}, "value": test_cmd})

        # LINT/FORMAT — отдельный шаг перед build
        if wants_format:
            ops.append({
                "op": "insert_before",
                "step": {"name": "build"},
                "name": "lint",
                "value": {"run": "dotnet format --verify-no-changes --verbosity minimal", "target": "auto"}
            })

        # COVERAGE — после test (Coverlet через свойства)
        if wants_cov:
            ops.append({
                "op": "insert_after",
                "step": {"name": "test"},
                "name": "coverage",
                "value": {
                    "run": (
                        "dotnet test --no-build "
                        "/p:CollectCoverage=true "
                        "/p:CoverletOutput=coverage/lcov.info "
                        "/p:CoverletOutputFormat=lcov"
                    ),
                    "target": "auto",
                    "needs": ["test"]
                }
            })

        # DOCKER — build → push
        if wants_docker:
            ops.append({
                "op": "insert_after",
                "step": {"name": "test"},
                "name": "build_image",
                "value": {"run": "docker build -t myapp:latest .", "target": "host", "needs": ["test"]}
            })
            ops.append({
                "op": "insert_after",
                "step": {"name": "build_image"},
                "name": "push_image",
                "value": {"run": "docker push myapp:latest", "target": "host", "needs": ["build_image"]}
            })


    # -------- Docker ----------
   
    elif kind == "docker":
        # --- разбор параметров из фразы ---
        kv = {}
        tokens = re.split(r"[\s\+\.,;]+", features_text.lower()) if features_text else []
        for tok in tokens:
            if "=" in tok:
                k, v = tok.split("=", 1)
                kv[k.strip().lower()] = v.strip()

        image = kv.get("image", "myapp")
        tag = kv.get("tag", "${VERSION:-latest}")
        dockerfile = kv.get("dockerfile", "Dockerfile")
        context = kv.get("context", ".")
        platform = kv.get("platform", "")
        registry = kv.get("registry", "")
        use_buildx = ("buildx" in tokens or "platform" in tokens or bool(platform))

        img_ref = f"{image}:{tag}"

        # --- BUILD ---
        if use_buildx:
            plat = f"--platform {platform} " if platform else ""
            build_cmd = f"docker buildx build {plat}-t {img_ref} -f {dockerfile} {context} --load"
        else:
            build_cmd = f"docker build -t {img_ref} -f {dockerfile} {context}"

        ops.append({
            "op": "insert_after",
            "step": {"name": "Checkout"},   # якорь = после checkout
            "name": "build",
            "value": {"run": build_cmd, "target": "host"}
        })

        # --- LOGIN (по запросу) ---
        if "login" in tokens or "логин" in tokens:
            login_cmd = (
                f'echo "$DOCKER_PASSWORD" | docker login {registry} -u "$DOCKER_USERNAME" --password-stdin'
                if registry else
                'echo "$DOCKER_PASSWORD" | docker login -u "$DOCKER_USERNAME" --password-stdin'
            )
            ops.append({
                "op": "insert_before",
                "step": {"name": "build"},
                "name": "login",
                "value": {"run": login_cmd, "target": "host"}
            })

        # --- RUN (по запросу) ---
        if "run" in tokens or "запусти" in tokens:
            ops.append({
                "op": "insert_after",
                "step": {"name": "build"},
                "name": "run",
                "value": {"run": f"docker run --rm {img_ref}", "target": "host"}
            })

        # --- PUSH ---
        if "push" in tokens or "пуш" in tokens or "deploy" in tokens or "деплой" in tokens:
            ops.append({
                "op": "insert_after",
                "step": {"name": "build"},
                "name": "push",
                "value": {"run": f"docker push {img_ref}", "target": "host"}
            })