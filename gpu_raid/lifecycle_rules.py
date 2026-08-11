"""Чистые правила автостопа облачных воркеров (без импортов ComfyUI).

decide() отвечает на один вопрос: надо ли СЕЙЧАС останавливать данного
воркера. Вся телеметрия приходит снаружи (view) — правила тестируются
матрицей без поднятого сервера.

view:
  kind             "cloud" | "lan" | "local"
  pinned           bool — 📌 в панели: не трогать никогда
  state            статус воркера ("online" — иначе не трогаем)
  busy             bool — в системе есть НЕзавершённые задания (глобально:
                   между сегментами chain воркер на секунды свободен, гасить
                   его в этот момент нельзя)
  has_worked       bool — воркер выполнил хотя бы один юнит с момента
                   старта мастера (защита instant от убийства только что
                   поднятого воркера, ждущего первого задания)
  idle_s           секунд с последней активности (или с выхода в online,
                   если заданий не было) | None, если неизвестно
  session_age_min  минут с запуска воркер-процесса (бюджет-guard)
  keep_alive_until epoch-время, до которого действует «не гасить после
                   этого задания» (0 = не задано)
"""

STOP = "stop"
NONE = "none"

POLICIES = ("keep", "eco", "instant", "local_only")


def effective_policy(cfg, platform=None, record_override=None):
    """Итоговая политика воркера: глобальная -> платформа -> запись воркера.

    cfg — settings["lifecycle"] целиком (в нём же platform_overrides:
    {"colab": {"policy": "keep", "idle_stop_min": 5}}); record_override —
    поле lifecycle записи воркера. policy="inherit"/пусто на любом уровне
    означает «не переопределяю». budget_min всегда глобальный.
    """
    out = dict(cfg or {})
    overrides = (cfg or {}).get("platform_overrides") or {}
    for ovr in (overrides.get(platform or ""), record_override):
        if not isinstance(ovr, dict):
            continue
        policy = str(ovr.get("policy") or "").strip()
        if policy and policy != "inherit":
            out["policy"] = policy
        try:
            idle = int(ovr.get("idle_stop_min") or 0)
        except (TypeError, ValueError):
            idle = 0
        if idle > 0:
            out["idle_stop_min"] = idle
    return out

# сколько секунд простоя нужно instant-политике: защита от гонки, когда
# пользователь ставит задания в очередь одно за другим
INSTANT_GRACE_S = 30


def is_cloud_allowed(policy):
    return policy != "local_only"


def decide(policy_cfg, view, now):
    """-> (STOP|NONE, причина-строка)."""
    if view.get("kind") != "cloud":
        return NONE, ""
    if view.get("pinned"):
        return NONE, ""
    if view.get("state") != "online":
        return NONE, ""
    if view.get("busy"):
        return NONE, ""

    policy = str(policy_cfg.get("policy") or "eco")

    # бюджет — защита квоты: срабатывает при любой политике (кроме pinned)
    budget = float(policy_cfg.get("budget_min") or 0)
    age = float(view.get("session_age_min") or 0)
    if budget > 0 and age >= budget:
        return STOP, f"бюджет сессии исчерпан ({int(age)} мин ≥ {int(budget)} мин)"

    kau = float(view.get("keep_alive_until") or 0)
    if kau and now < kau:
        return NONE, ""

    if policy == "keep":
        return NONE, ""
    if policy == "local_only":
        return STOP, "режим «Только локально»"

    idle_s = view.get("idle_s")
    if idle_s is None:
        return NONE, ""

    if policy == "instant":
        if view.get("has_worked") and idle_s >= INSTANT_GRACE_S:
            return STOP, "остановка сразу после задания"
        return NONE, ""

    # eco (и любая незнакомая политика ведёт себя как eco)
    idle_min = float(policy_cfg.get("idle_stop_min") or 10)
    if idle_s >= idle_min * 60:
        return STOP, f"простой {int(idle_s // 60)} мин ≥ {int(idle_min)} мин"
    return NONE, ""
