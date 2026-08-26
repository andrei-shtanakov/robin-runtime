# Epic-axis shadow — план имплементации (срез 1)

Спека: `docs/superpowers/specs/2026-08-26-epic-shadow-digest-design.md`.
Все задачи — в этой ветке, один PR. Порядок сохраняет рабочее состояние:
каждая задача завершается зелёным `uv run pytest -q` + `ruff format
--check` + `ruff check src tests`; интеграция в `run()` — последней.

## Задача 1 — зонтик в списки зеркал (спека §3.2)

- `src/robin/config.py`: `ai-orchestrators-workspace` в `_ECOSYSTEM_REPOS`
  с комментарием (реестр эпиков читается из этого зеркала; урок
  maestro/libretto — регистрируем в день появления потребности).
- `deploy/setup.sh`: та же запись в `REPOS`.
- Синхронность двух списков уже закреплена
  `tests/test_config.py:57` (`test_mirror_list_matches_the_deploy_script`)
  — задача проходит его без правок теста.

Работоспособно после задачи: да — зеркало просто появляется в обзоре
(на машинах без каталога `load_config()` уносит его в `missing_mirrors`,
как любое отсутствующее).

## Задача 2 — окно во владении `run()` (спека §3.1)

- `src/robin/digest.py`:
  - `run(kind, *, now=None)`: `now` разыменовывается ровно один раз на
    входе (`datetime.now(zone)`), дальше только передаётся;
  - `period = window(config, kind, now=now)` вычисляется в `run()`, с
    пришпиленным верхом: `Period(since=..., until=now, label=...)`;
  - `compose(config, kind, *, now=None, period=None)`: при переданном
    `period` не зовёт `window()`; сам `window()` не меняется;
  - `persist(config, kind, text, now=now)` — как сейчас.
- Тесты: существующие тесты `compose`/`run` адаптируются (сигнатура
  обратно совместима — оба параметра опциональны); новый тест: коммит
  с датой позже `until` в окно не попадает.

Работоспособно после задачи: да — поведение дайджеста не меняется,
кроме пришпиленного `until` (раньше окно было полуоткрытым до «сейчас
каждого git log»; теперь верх один на прогон).

## Задача 3 — `src/robin/epic_shadow.py`: сбор и классификация (§3–§5)

Один модуль, чистые функции + один writer. Состав:

- `@dataclass(frozen=True) ShadowSnapshot`: `window`, `generated_at`
  (ISO `period.until`), `per_epic`, `buckets`, `provenance`.
- `collect(config, period) -> ShadowSnapshot`:
  - корпус `[config.vault_path, *config.repo_paths]`;
  - per-repo `git log` (`subprocess.run`, таймаут `_GIT_TIMEOUT_S`-стиль,
    read-only) с framing
    `--format=%x1e%H%x1f%(trailers:key=Epic,valueonly=true)%x1f%(trailers:key=Defect,valueonly=true)`,
    `--since`/`--until` из `period`, БЕЗ `--no-merges`, без лимита;
  - git отсутствует/timeout/non-zero → `skipped: <причина>`; запись не
    из 3 полей → пропуск + `partial: <n> unparsed`;
  - `config.missing_mirrors` → `skipped: not a directory`;
  - реестр: `epics.toml` зонтика, `[epics]` → `{key: title|None}`;
    catch-all любой ошибки → `registry: unavailable, <причина>`;
  - классификация по таблице спеки §4 (множество непустых уникальных
    значений `Epic:`; `Defect:`-счётчик = коммиты с ≥1 непустым).
- `render_json(snapshot) -> str` (`json.dumps(..., sort_keys=True,
  ensure_ascii=False, indent=2)`) и `render_md(snapshot) -> str`
  (канонический порядок §3; bucket-строки всегда; экранирование
  примеров `json.dumps`; блок провенанса).
- `persist(config, snapshot) -> tuple[Path, Path]`: JSON → MD, оба с
  read-back (паттерн `digest.persist`), пути из даты `generated_at`.

Тесты задачи 3 (tmp-git-фикстуры, паттерн тестов `changes`; фикстура
реестра — tmp-каталог с `epics.toml`), явный список:

- трейлер в финальном блоке → классифицирован; отделённый пустой
  строкой → `unclassified` (фикстура строит оба коммита);
- два разных `Epic:` → `conflict` (и рендер примера `"a" + "b"`); два
  одинаковых → классифицирован; пустое значение → отброшено; ключ вне
  реестра → `unregistered`;
- merge-коммит без трейлера → `unclassified` (регрессионный к отличию
  от `git_log` с `--no-merges`);
- реестр: параметризованный тест всех классов сбоя §3.2 (нет файла,
  `OSError`/`PermissionError`, ошибка декодирования, невалидный TOML,
  нет `[epics]`, `[epics]` не таблица) → `unavailable` + причина в
  провенансе, bucket `unverified`, строки `unregistered` нет;
- зеркало с падающим git → `skipped`; запись не из 3 полей →
  `partial: 1 unparsed`; `missing_mirrors` → `skipped: not a directory`;
- ноль коммитов → оба файла пишутся, нулевые счётчики, все
  bucket-строки, провенанс полный;
- `generated_at`: метки в JSON и MD совпадают и равны ISO
  `period.until`; сценарий смешанной пары (JSON перезаписан, MD
  старый) обнаружим по расхождению меток;
- детерминизм: два прогона по одной фикстуре с одним `now` → побайтно
  одинаковые md и json;
- md и json одного прогона согласованы по счётчикам.

## Задача 4 — интеграция в `run()` (§3.1)

- `digest.run`: для `kind == "weekly"` последним шагом (после
  success-record) —
  `try: epic_shadow.run_shadow(config, period)` / `except Exception:`
  лог + best-effort failure-record (`surface="epic-shadow"`, свой
  try/except вокруг записи).
- Тесты задачи 4: сбой shadow (моканный `collect` бросает) не роняет
  `run` и пишет failure-record; сбой записи failure-record подавлен;
  daily прогон shadow не запускает; интеграционный тест единого окна —
  monkeypatch перехватывает `Period`, полученный `compose()` и shadow,
  и утверждает, что это **один и тот же объект** (спека §3.1, §7).

## Задача 5 — TODO.md и README

- `TODO.md`: пункт среза 1 `@id:epic-shadow-slice1 @epic:eco.epics`
  (закрывается второй фазой после мержа); пункт среза 2
  `@id:epic-shadow-pr-attribution @epic:eco.epics
  @blocked_by:dispatcher#199` (переходная форма — пункта-носителя в
  TODO dispatcher ещё нет) — атрибуция по `merged`-окну snapshot/v2 +
  вендоринг контракта.
- `README.md`: короткая секция про shadow-артефакты (`var/epic-shadow/`,
  `var/digests/*-epic-shadow.md`): что это, почему не постится, ссылка
  на спеку.

## Задача 6 — черта под PR

- Прогнать полный набор: `uv run pytest -q`, `uv run ruff format
  --check .`, `uv run ruff check src tests`.
- Draft PR; номер PR вписать в оба новых пункта TODO тем же PR
  (правило spec-authoring); снятие драфта после чтения пары владельцем.
- Коммиты — `Epic: eco.epics` финальным трейлерным блоком.

## Соответствие спеке (трассируемость)

| Секция спеки | Задача |
|---|---|
| §3.1 окно, запуск, изоляция сбоя | 2, 4 |
| §3.2 источник-трейлеры, framing, per-repo ошибки | 3 |
| §3.2 реестр, зонтик в зеркалах | 1, 3 |
| §4 классификация | 3 |
| §5 строгий разбор трейлеров | 3 |
| §6 выход, метки пары, порядок | 3 |
| §7 тесты | 3, 4 |
| §8 срез 2 | 5 (пункт TODO) |
| §9 приёмка | 6 + прогоны на VPS после мержа |

Вне плана (не-цели спеки): cutover, Telegram, snapshot/v2, GitHub API,
pyrefly.
