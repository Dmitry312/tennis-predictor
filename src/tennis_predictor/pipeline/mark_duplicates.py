"""Разметка дублей в Match: один реальный матч под двумя nbbet_match_id.

Подтверждено (docs/decisions.md): у дублей идентичный start_ts, и когда у
обеих строк есть pbp — общий pbp_s3_path. Дубль уже в исходной sqlite,
не наша ошибка. Строки не удаляются — следующий sweep пересоздал бы их
по nbbet_match_id (ON CONFLICT). Вместо этого у "лишней" строки
duplicate_of указывает на оставляемую.

Ключ группы: те же два игрока (без учёта порядка/регистра) и
|Δstart_ts| <= 2 часа; если у кого-то start_ts нет — тот же match_date
±1 день. Внутри группы различаем:
  - настоящий дубль: все start_ts совпадают с точностью до нескольких
    минут (TIGHT_MINUTES) — это и есть сигнатура "то же событие под
    двумя ID", которую подтвердили на выборке;
  - перенос/другой матч (как пара Davis Cup — разное время И разный
    статус): max Δstart_ts в группе больше TIGHT_MINUTES — такие
    группы НЕ размечаются, выводятся отдельным списком на ручной разбор.

Правило выбора "оставляемой" строки: has_pbp, затем details_fetched_at
IS NOT NULL, затем status == 3, затем меньший nbbet_match_id.

--dry-run включён по умолчанию — ничего не пишет, только печатает отчёт.
Применение (--dry-run=False, он же --apply) делается один раз вручную
после подтверждения.
"""

import argparse
import logging
from dataclasses import dataclass

import pandas as pd
from sqlalchemy import text

from tennis_predictor.storage.db import engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TIGHT_MINUTES = 10  # то же событие, отличие — не более пары минут на обеих сторонах
LOOSE_HOURS = 2  # окно кандидатов в группу (до разбора tight/loose)
DATE_FALLBACK_DAYS = 1  # если у кого-то start_ts нет вовсе


def load_candidates() -> pd.DataFrame:
    with engine.begin() as conn:
        df = pd.read_sql_query(
            text("""
                SELECT match_id, nbbet_match_id, tournament_name, player1, player2,
                       match_date, start_ts, status, has_pbp, details_fetched_at
                FROM matches WHERE level IN ('tour', 'challenger')
            """),
            conn,
        )
    df["players_key"] = df.apply(
        lambda r: tuple(sorted((str(r.player1).lower(), str(r.player2).lower()))), axis=1
    )
    return df


def _same_candidate(a: pd.Series, b: pd.Series) -> tuple[bool, str]:
    """(совпадает ли ключ группы, по какой ветке)."""
    if pd.notna(a.start_ts) and pd.notna(b.start_ts):
        delta_h = abs((a.start_ts - b.start_ts).total_seconds()) / 3600
        if delta_h <= LOOSE_HOURS:
            return True, "start_ts<=2h"
        return False, ""
    delta_d = abs((a.match_date - b.match_date).days)
    if delta_d <= DATE_FALLBACK_DAYS:
        return True, "date±1d (start_ts отсутствует)"
    return False, ""


@dataclass
class Group:
    indices: list[int]
    branch: str
    max_delta_minutes: float | None


class _UnionFind:
    def __init__(self, items):
        self.parent = {i: i for i in items}

    def find(self, x):
        while self.parent[x] != x:
            x = self.parent[x]
        return x

    def union(self, x, y):
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self.parent[rx] = ry


def find_groups(df: pd.DataFrame) -> list[Group]:
    groups: list[Group] = []
    for _players, sub in df.groupby("players_key"):
        if len(sub) < 2:
            continue
        idx = list(sub.index)
        n = len(idx)
        uf = _UnionFind(idx)

        branch_of_pair: dict[tuple[int, int], str] = {}
        for i in range(n):
            for j in range(i + 1, n):
                a, b = sub.loc[idx[i]], sub.loc[idx[j]]
                ok, branch = _same_candidate(a, b)
                if ok:
                    uf.union(idx[i], idx[j])
                    branch_of_pair[(idx[i], idx[j])] = branch

        clusters: dict[int, list[int]] = {}
        for i in idx:
            clusters.setdefault(uf.find(i), []).append(i)

        for members in clusters.values():
            if len(members) < 2:
                continue
            deltas = []
            branches = set()
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    a, b = df.loc[members[i]], df.loc[members[j]]
                    if pd.notna(a.start_ts) and pd.notna(b.start_ts):
                        deltas.append(abs((a.start_ts - b.start_ts).total_seconds()) / 60)
                    key = (min(members[i], members[j]), max(members[i], members[j]))
                    if key in branch_of_pair:
                        branches.add(branch_of_pair[key])
            max_delta = max(deltas) if deltas else None
            branch = "/".join(sorted(branches)) if branches else "date±1d"
            groups.append(Group(indices=members, branch=branch, max_delta_minutes=max_delta))
    return groups


def choose_keeper(df: pd.DataFrame, indices: list[int]) -> int:
    def sort_key(i):
        r = df.loc[i]
        return (
            0 if r.has_pbp else 1,
            0 if pd.notna(r.details_fetched_at) else 1,
            0 if r.status == 3 else 1,
            r.nbbet_match_id,
        )

    return min(indices, key=sort_key)


def reason_for(df: pd.DataFrame, keeper: int, other: int) -> str:
    rk, ro = df.loc[keeper], df.loc[other]
    if rk.has_pbp != ro.has_pbp:
        return f"has_pbp {rk.has_pbp} vs {ro.has_pbp}"
    if pd.notna(rk.details_fetched_at) != pd.notna(ro.details_fetched_at):
        return "details_fetched_at"
    if (rk.status == 3) != (ro.status == 3):
        return f"status {rk.status} vs {ro.status}"
    return f"nbbet_match_id {rk.nbbet_match_id} < {ro.nbbet_match_id}"


def run(dry_run: bool) -> None:
    df = load_candidates()
    groups = find_groups(df)

    tight_all = [g for g in groups if g.max_delta_minutes is None or g.max_delta_minutes <= TIGHT_MINUTES]
    loose = [g for g in groups if g.max_delta_minutes is not None and g.max_delta_minutes > TIGHT_MINUTES]

    def _all_terminal(g: Group) -> bool:
        return all(df.loc[i].status in (3, 9, 10) for i in g.indices)

    tight = [g for g in tight_all if _all_terminal(g)]
    tight_excluded = [g for g in tight_all if not _all_terminal(g)]
    if tight_excluded:
        logger.info(
            "групп НЕ размечаем (есть строка со статусом не 3/9/10): %d из %d",
            len(tight_excluded), len(tight_all),
        )
        for g in tight_excluded:
            print(f"  {[df.loc[i].nbbet_match_id for i in g.indices]} "
                  f"статусы={[df.loc[i].status for i in g.indices]}")

    sizes = pd.Series([len(g.indices) for g in tight]).value_counts().sort_index()
    logger.info("групп-дублей (tight, status IN (3,9,10) у всех строк, будут размечены): %d", len(tight))
    logger.info("размеры групп: %s", sizes.to_dict())
    branch_counts = pd.Series([g.branch for g in tight]).value_counts()
    logger.info("по ветке ключа: %s", branch_counts.to_dict())

    logger.info("\n10 примеров:")
    for g in tight[:10]:
        keeper = choose_keeper(df, g.indices)
        row_k = df.loc[keeper]
        print(f"  группа {[df.loc[i].nbbet_match_id for i in g.indices]} "
              f"({row_k.tournament_name}, {row_k.match_date}, ветка={g.branch})")
        for i in g.indices:
            mark = "ОСТАВЛЯЕМ" if i == keeper else "дубль"
            reason = "" if i == keeper else f" ({reason_for(df, keeper, i)})"
            r = df.loc[i]
            print(f"    [{mark}] nbbet_id={r.nbbet_match_id} status={r.status} "
                  f"has_pbp={r.has_pbp} details={pd.notna(r.details_fetched_at)}{reason}")

    logger.info("\nгруппы с |Δstart_ts| > %d мин (перенос/другой матч, НЕ размечаются):", TIGHT_MINUTES)
    for g in loose:
        ids = [df.loc[i].nbbet_match_id for i in g.indices]
        statuses = [df.loc[i].status for i in g.indices]
        print(f"  {ids}, статусы={statuses}, max Δ={g.max_delta_minutes:.0f} мин, "
              f"{df.loc[g.indices[0]].tournament_name}, {df.loc[g.indices[0]].match_date}")

    if dry_run:
        logger.info("\n[dry-run] ничего не записано. Запустите с --apply после подтверждения.")
        return

    updates = []
    for g in tight:
        keeper = choose_keeper(df, g.indices)
        for i in g.indices:
            if i != keeper:
                updates.append({"match_id": str(df.loc[i].match_id), "dup_of": str(df.loc[keeper].match_id)})

    with engine.begin() as conn:
        for u in updates:
            conn.execute(
                text("UPDATE matches SET duplicate_of = :dup_of WHERE match_id = :match_id"),
                u,
            )
    logger.info("размечено дублей: %d", len(updates))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="записать duplicate_of (по умолчанию — dry-run)")
    a = p.parse_args()
    run(dry_run=not a.apply)


if __name__ == "__main__":
    main()
