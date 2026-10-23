from __future__ import annotations

from io import BytesIO
import math

from PIL import Image, ImageDraw, ImageFont


RATING_RANK_CUTOFFS = [
    (0, "Iron"),
    (1300, "Bronze"),
    (1400, "Silver"),
    (1550, "Gold"),
    (1700, "Platinum"),
    (1800, "Diamond"),
    (1900, "Champion"),
]


def _font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "arialbd.ttf" if bold else "arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def build_rating_history_png(
    history: list[dict], username: str, season_name: str
) -> bytes | None:
    """Render post-match rating values against season-local match numbers."""
    points = []
    for item in history:
        try:
            match_id = int(item["match_id"])
            rating = int(item["rating"])
        except (KeyError, TypeError, ValueError):
            continue
        points.append((len(points) + 1, rating, match_id))
    if not points:
        return None

    width, height = 1000, 520
    left, right, top, bottom = 150, 36, 64, 82
    plot_w, plot_h = width - left - right, height - top - bottom
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font = _font(23, True)
    label_font = _font(14)
    tick_font = _font(12)
    draw.text((left, 18), f"{username} — {season_name}", fill="#20242b", font=title_font)

    game_numbers = [point[0] for point in points]
    ratings = [point[1] for point in points]
    min_x, max_x = min(game_numbers), max(game_numbers)
    min_rating, max_rating = min(ratings), max(ratings)
    lowest_rank_index = max(
        index
        for index, (cutoff, _) in enumerate(RATING_RANK_CUTOFFS)
        if min_rating >= cutoff
    )
    highest_rank_index = max(
        index
        for index, (cutoff, _) in enumerate(RATING_RANK_CUTOFFS)
        if max_rating >= cutoff
    )
    low_y = max(1000, RATING_RANK_CUTOFFS[lowest_rank_index][0])
    if min_rating < low_y:
        low_y = min_rating - 50
    if highest_rank_index + 1 < len(RATING_RANK_CUTOFFS):
        high_y = RATING_RANK_CUTOFFS[highest_rank_index + 1][0]
    else:
        high_y = math.ceil((max_rating + 100) / 100) * 100
    if high_y == low_y:
        high_y += 1

    def xy(match_id: int, rating: int) -> tuple[float, float]:
        x = left + (match_id - min_x) / max(1, max_x - min_x) * plot_w
        y = top + (high_y - rating) / (high_y - low_y) * plot_h
        return x, y

    # Mark ProAvalon's rank cutoffs so rating changes have rank context.
    rating_guides = [
        (cutoff, f"{rank} · {cutoff:,}")
        for cutoff, rank in RATING_RANK_CUTOFFS
        if low_y <= cutoff <= high_y
    ]
    if low_y == 1000:
        rating_guides.insert(0, (1000, "1,000"))
    if high_y > RATING_RANK_CUTOFFS[-1][0]:
        rating_guides.append((high_y, f"{high_y:,.0f}"))
    for value, label in rating_guides:
        if value < low_y or value > high_y:
            continue
        y = top + (high_y - value) / (high_y - low_y) * plot_h
        draw.line((left, y, width - right, y), fill="#dfe4ea", width=1)
        text_height = draw.textbbox((0, 0), label, font=tick_font)[3]
        draw.text(
            (4, y - text_height / 2),
            label,
            fill="#59616b",
            font=tick_font,
        )

    draw.line((left, top, left, top + plot_h), fill="#626b75", width=2)
    draw.line((left, top + plot_h, width - right, top + plot_h), fill="#626b75", width=2)
    if len(points) > 1:
        line_width = 4 if len(points) <= 60 else 3
        draw.line(
            [xy(game_number, rating) for game_number, rating, _ in points],
            fill="#3978c5",
            width=line_width,
        )

    # Use round tick intervals so long histories stay readable (for example,
    # every 20 games for a 150-game history), while preserving both endpoints.
    if len(points) <= 12:
        labeled_games = set(range(1, len(points) + 1))
    else:
        raw_step = (len(points) - 1) / 8
        magnitude = 10 ** math.floor(math.log10(raw_step))
        scaled_step = raw_step / magnitude
        nice_step = 1 if scaled_step <= 1 else 2 if scaled_step <= 2 else 5 if scaled_step <= 5 else 10
        tick_step = max(1, int(nice_step * magnitude))
        labeled_games = set(range(tick_step, len(points) + 1, tick_step))
        labeled_games.update({1, len(points)})

    for game_number in sorted(labeled_games):
        x, _ = xy(game_number, ratings[game_number - 1])
        draw.line((x, top + plot_h, x, top + plot_h + 5), fill="#68717d", width=1)
        label = str(game_number)
        text_width = draw.textbbox((0, 0), label, font=tick_font)[2]
        draw.text(
            (x - text_width / 2, top + plot_h + 14),
            label,
            fill="#59616b",
            font=tick_font,
        )
    x_label = "Games played"
    x_label_width = draw.textbbox((0, 0), x_label, font=label_font)[2]
    draw.text(
        (left + (plot_w - x_label_width) / 2, height - 25),
        x_label,
        fill="#353b43",
        font=label_font,
    )

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
