from html import escape
from textwrap import wrap

# Headroom for delivery ID, history caption and Telegram entities.
LIMIT = 3700


def units(text):
    return len(text.encode("utf-16-le")) // 2


def author_blocks(handle, summary, ids):
    title = f"<b>@{escape(handle)}</b>"
    links = [f'<a href="https://x.com/{handle}/status/{xid}">{i}</a>' for i, xid in enumerate(ids, 1)]
    body = f"{title}\n{escape(summary)}\n🔗 Источники ({len(ids)}): " + " · ".join(links)
    if units(body) <= LIMIT:
        return [body]
    # Escape after splitting plain text so neither tags nor HTML entities are cut.
    parts = [title]
    for i, fragment in enumerate(wrap(summary, width=480)):
        separator = "\n" if i == 0 else " "
        line = escape(fragment)
        if units(parts[-1] + separator + line) > LIMIT:
            parts.append(title + " · продолжение\n" + line)
        else:
            parts[-1] += separator + line
    sources = f"🔗 Источники ({len(ids)}):"
    if units(parts[-1] + "\n" + sources + " " + (links[0] if links else "")) > LIMIT:
        parts.append(title + "\n" + sources)
    else:
        parts[-1] += "\n" + sources
    for link in links:
        if units(parts[-1] + " " + link) > LIMIT:
            parts.append(title + " · источники\n" + link)
        else:
            parts[-1] += " " + link
    return parts


def render(groups, start, end, hours, next_due):
    def pretty(t):
        return t.strftime("%d.%m.%Y %H:%M")
    header = (f"<b>📰 Дайджест X — {end:%d.%m.%Y}</b>\n"
              f"Публикации с {pretty(start)} по {pretty(end)} UTC.")
    blocks = [header]
    for handle, summary, ids in groups:
        blocks.extend(author_blocks(handle, summary, ids))
    total = sum(len(ids) for _, _, ids in groups)
    blocks.append(f"<b>📊 Итого</b> · Новых твитов: {total}. Авторов: {len(groups)}.\n"
                  f"Следующая плановая проверка — {pretty(next_due)} UTC. Интервал — {hours} ч.")
    parts = []
    for block in blocks:
        if parts and units(parts[-1] + "\n\n" + block) <= LIMIT:
            parts[-1] += "\n\n" + block
        else:
            parts.append(block)
    return parts
