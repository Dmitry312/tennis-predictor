# Production inventory: read-only runbook

Статус: сценарий для будущего согласованного сеанса, НЕ выполнен на production.
Связанный документ: [архитектура и откат](production-releases.md).
Это намеренно пошаговый runbook, не исполняемый скрипт и не workflow.

## Границы и порядок

1. Получить отдельное разрешение владельца на сеанс, пользователя и область
   чтения. До соединения независимо проверить SSH host fingerprint по процедуре
   из архитектурного документа. Базовый whoami уже проверен 4 ноября, не повторять.
2. Получить от владельца подтверждённый путь checkout и имя БД-контейнера.
   `/root/Tennis_pelmen_model` — значение из Git, а не проверенный факт;
   `/opt/tennis-predictor` не предполагается. Не искать .env по всему диску.
3. Выполнять блоки по одному, без sudo, без перенаправления вывода в файлы сервера.
   Недоступная команда или permission denied → UNKNOWN и остановка этого блока,
   без установки ПО или расширения привилегий. Не использовать set -x.
4. Сначала метаданные, затем минимальные структурные сведения. Не читать .env,
   private keys, credentials, /proc/*/environ, shell history, сырые логи,
   содержимое Docker Config.Env или полные cron/unit-команды. Не публиковать
   конфиденциальные пути и идентификаторы без ревью владельца.
5. Вывод оценить на стороне оператора; в GitHub вносить только обезличенную
   сводку. SSH/аудит ОС могут сами писать системные журналы. Команды ниже не
   изменяют прикладные файлы, БД, контейнеры и конфигурацию; фоновые задачи
   продолжают жить, поэтому наблюдения не являются атомарным снимком.

## Ресурсы и время

```bash
date -Is
timedatectl show -p Timezone
free -h
df -h
df -i
```

Фиксировать доступное место/память и timezone для интерпретации cron.
Не запускать бенчмарки, проверки записи, очистку или создание каталогов.

## Checkout, окружение и locks: только метаданные

Значение вводится вручную после подтверждения владельца, это локальная shell
переменная текущего сеанса, не изменение конфигурации среды. Блок не запускать
с шаблонным значением. Не source .env и не запускать Python проекта.

```bash
inventory_root='/REPLACE_WITH_OWNER_CONFIRMED_EXISTING_CHECKOUT'
if [ -d "$inventory_root" ]; then
  stat -c '%A %U:%G %n' -- "$inventory_root"
  readlink -f -- "$inventory_root"
  stat -c '%A %U:%G %n' -- "$inventory_root/.env" "$inventory_root/.venv"
  readlink -f -- "$inventory_root/.venv/bin/python"
  git --no-optional-locks -c core.fsmonitor=false -c core.untrackedCache=false \
    -C "$inventory_root" rev-parse --verify HEAD
  git --no-optional-locks -c core.fsmonitor=false -c core.untrackedCache=false \
    -C "$inventory_root" diff --quiet --no-ext-diff --no-textconv HEAD --
  inventory_diff_status=$?
  printf 'tracked_diff_exit=%s (0=clean, 1=changed, other=unknown)\n' "$inventory_diff_status"
else
  printf 'UNKNOWN: confirmed checkout unavailable\n'
fi
```

Последняя проверка охватывает tracked изменения, но НЕ untracked/ignored файлы.
Владелец отдельно подтверждает наличие незакоммиченного кода и незаписанных
зависимостей; не читать такие файлы до согласования безопасного перечня.
Не выполнять git fetch/pull, pip/uv, приложение, импорты или health CLI.
Версии зависимостей устанавливаются по безопасной ведомости владельца;
содержимое direct_url.json и pip freeze может содержать credentials.
Метаданные существующих lock-файлов можно проверить stat по точным путям из
проверенного cron; не брать flock, не удалять locks и не создавать новые.

## Планировщики и процессы

Встроенные cron/unit-команды могут содержать секреты. Поэтому не выполнять
`crontab -l`, `cat /etc/crontab`, `systemctl cat` или дамп файлов в общий лог.
Вместо этого владелец предоставляет проверенную сводку: пользователь, timezone,
расписание, cwd, Python, модуль, безопасные аргументы, lock/режим ожидания,
пути логов. Значения окружения и произвольные аргументы исключить.
Сравнить её со всеми шестью записями infra/cron/tennis-predictor.crontab;
отдельно учесть user/system cron, timers и ручные фоновые запуски.
Без такой сводки полнота инвентаризации НЕ подтверждена.

Безопасные метаданные известных каталогов (без чтения содержимого):

```bash
stat -c '%A %U:%G %n' -- /etc/crontab /etc/cron.d
systemctl list-timers --all --no-pager --no-legend
ps -eo pid=,ppid=,user=,lstart=,etime=,comm=
```

Не использовать ps aux/args/cmdline и не читать /proc/*/environ.
Для PID, который владелец идентифицировал как задачу проекта:

```bash
inventory_pid='REPLACE_WITH_CONFIRMED_NUMERIC_PID'
case "$inventory_pid" in
  ''|*[!0-9]*) printf 'UNKNOWN: numeric PID required\n' ;;
  *) readlink -- "/proc/$inventory_pid/cwd" "/proc/$inventory_pid/exe" ;;
esac
```

PID может исчезнуть или переиспользоваться: повторно сверить время старта.
comm/cwd/exe не всегда отличают задачи и venv; не объявлять отсутствие задач
по одному снимку. Для переключения нужен отдельный план учёта запусков/дренирования.

## Docker и защита PostgreSQL

Только Docker CLI для чтения выбранных полей, без compose: compose может
раскрывать подставленные секреты. Никаких exec, pull, up, restart и prune.

```bash
docker ps --all --no-trunc --format '{{.ID}} {{.Image}} {{.Status}} {{.Names}}'
```

После подтверждения имени именно PostgreSQL-контейнера:

```bash
inventory_db_container='REPLACE_WITH_OWNER_CONFIRMED_CONTAINER'
docker inspect --type container --format \
  'image_id={{.Image}} state={{.State.Status}} restart={{.HostConfig.RestartPolicy.Name}}' \
  "$inventory_db_container"
docker inspect --type container --format \
  '{{range .Mounts}}type={{.Type}} name={{.Name}} destination={{.Destination}}{{println}}{{end}}' \
  "$inventory_db_container"
```

Не выводить полный inspect, environment, docker config/auth, container logs или
Compose config. Ошибка доступа к daemon означает UNKNOWN, не повод использовать
sudo. Проверить по сводке владельца volume/backup, RPO/RTO и дату последней
успешной проверки восстановления. На этом этапе не подключаться к БД даже для
SELECT; SQL-инвентаризация и восстановление копии требуют отдельного разрешения.
Не запускать существующий health check: он пишет job_runs/файлы и обращается к S3.

## Отчёт и условия остановки

Сводка: время наблюдения, confirmed/UNKNOWN, путь checkout, SHA и наличие tracked
изменений, происхождение untracked кода, версия Python/зависимостей, все
планировщики, active jobs/locks, timezone, ресурсы, Docker image ID и volume,
backup и ограничения доступа. Секреты и сырые журналы не прикладывать.

Остановить подготовку активации, если не подтверждены fingerprint, происхождение
работающего кода, полнота cron/процессов, совместимость двух версий, сохранность
БД, окружение, место для второго релиза или восстановимость прежнего запуска.
Никакое наблюдение не разрешает менять cron, копировать .env, создавать venv,
переключать symlink или запускать деплой. Решение о следующем этапе — владельцу.
