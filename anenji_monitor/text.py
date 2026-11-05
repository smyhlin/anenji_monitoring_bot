"""User-facing copy. Ukrainian, HTML markup, emoji — the chat audience language."""

from __future__ import annotations

import time

SOC_MARK = {50: "🟡", 25: "🟠"}


def format_duration(seconds: float) -> str:
    """Compact Ukrainian span: 45с → менше хвилини, 1500 → 25хв, 69240 → 19год 14хв."""
    total = max(0, int(seconds))
    if total < 1:
        return "0хв"
    if total < 60:
        return "менше хвилини"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}год")
    if minutes and not days:
        parts.append(f"{minutes}хв")
    return " ".join(parts)


def clock(timestamp: float) -> str:
    return time.strftime("%H:%M", time.localtime(timestamp))


def day_label(timestamp: float) -> str:
    return time.strftime("%d.%m.%Y", time.localtime(timestamp))


def moment(timestamp: float) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(timestamp))


def battery_bar(soc: float, cells: int = 10) -> str:
    filled = max(0, min(cells, round(soc / 100 * cells)))
    return "█" * filled + "░" * (cells - filled)


def soc_mark(soc: float) -> str:
    return "🪫" if soc < 25 else "🔋"


def percent(part: float, whole: float) -> str:
    return f"{0 if whole <= 0 else part / whole * 100:.0f}%"


def transition_message(powered: bool, at: float, previous_duration: float) -> str:
    if powered:
        return (
            f"🟢 <b>{clock(at)} Світло з'явилося</b>\n"
            f"🕐 Його не було {format_duration(previous_duration)}"
        )
    return (
        f"🔴 <b>{clock(at)} Світло зникло</b>\n"
        f"🕐 Воно було {format_duration(previous_duration)}"
    )


def battery_message(
    level: int, soc: float, powered: bool | None, register_soc: float | None = None
) -> str:
    mark = SOC_MARK.get(level, "🔋")
    grid = "🟢 світло є" if powered else "🔴 світла немає"
    detail = f"заряд {soc:.0f}%"
    if register_soc is not None and abs(soc - register_soc) >= 1:
        detail += f" · інв. {register_soc:.0f}%"
    return f"{mark} <b>Батарея: поріг {level}%</b>\n{grid} · {detail}"


def recovery_message(
    *,
    title: str,
    gap: float,
    powered_now: bool | None,
    changed: bool,
    soc: float | None,
) -> str:
    lines = [f"<b>{title}</b>"]
    if gap > 0:
        lines.append(f"🕐 Без моніторингу {format_duration(gap)}")
    if powered_now is not None:
        lines.append("🟢 Світло є" if powered_now else "🔴 Світла немає")
        if changed:
            lines.append(
                "📌 Поки бот спав, світло " + ("з'явилося" if powered_now else "зникло")
            )
    if soc is not None:
        lines.append(f"🔋 Батарея {soc:.0f}%")
    return "\n".join(lines)
