from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw, ImageFont


def _font(size: int, bold: bool = False):
    candidates = [
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        ),
        "arialbd.ttf" if bold else "arial.ttf",
    ]

    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass

    return ImageFont.load_default()


def _center_text(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    *,
    font,
    fill: str = "black",
) -> None:
    left, top, right, bottom = box
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = left + ((right - left) - text_width) / 2
    y = top + ((bottom - top) - text_height) / 2 - 1
    draw.text((x, y), text, fill=fill, font=font)


def _draw_black_x(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
) -> None:
    """Draw a large black X for a player proposed on the mission team."""
    left, top, right, bottom = box
    pad_x = max(9, int((right - left) * 0.22))
    pad_y = max(7, int((bottom - top) * 0.20))
    width = max(4, int(min(right - left, bottom - top) * 0.12))
    draw.line(
        (left + pad_x, top + pad_y, right - pad_x, bottom - pad_y),
        fill="#111111",
        width=width,
    )
    draw.line(
        (right - pad_x, top + pad_y, left + pad_x, bottom - pad_y),
        fill="#111111",
        width=width,
    )


def build_vote_history_png(game_data: dict) -> bytes | None:
    """Render ProAvalon's voteHistory[player][mission][pick] as a PNG."""
    history = game_data.get("voteHistory")
    players = game_data.get("playerUsernamesOrdered") or []
    if not isinstance(history, dict) or not history or not players:
        return None

    # Match ProAvalon's own clockwise voting-history row order.
    ordered = [players[0], *reversed(players[1:])]
    first = next((player for player in ordered if player in history), None)
    if first is None:
        return None

    missions = history.get(first) or []
    columns: list[tuple[int, int]] = []
    for mission_index, picks in enumerate(missions):
        for pick_index, _ in enumerate(picks or []):
            columns.append((mission_index, pick_index))
    if not columns:
        return None

    name_width = 230
    cell_width = 52
    row_height = 42
    mission_header_height = 34
    pick_header_height = 30
    header_height = mission_header_height + pick_header_height
    width = name_width + cell_width * len(columns) + 2
    height = header_height + row_height * len(ordered) + 2

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    player_font = _font(15)
    header_font = _font(15, True)
    pick_font = _font(12, True)

    mission_history = game_data.get("missionHistory") or []

    draw.rectangle(
        (0, 0, name_width, header_height),
        fill="#f3f3f3",
        outline="#6f6f6f",
    )

    x = name_width
    for mission_index in sorted({mission for mission, _ in columns}):
        proposal_count = sum(
            1 for mission, _ in columns if mission == mission_index
        )
        result = (
            mission_history[mission_index]
            if mission_index < len(mission_history)
            else None
        )
        if result == "succeeded":
            mission_fill = "#b9dcff"
        elif result == "failed":
            mission_fill = "#ffb8b8"
        else:
            mission_fill = "#e8e8e8"

        mission_right = x + proposal_count * cell_width
        draw.rectangle(
            (x, 0, mission_right, mission_header_height),
            fill=mission_fill,
            outline="#6f6f6f",
        )
        _center_text(
            draw,
            (x, 0, mission_right, mission_header_height),
            f"M{mission_index + 1}",
            font=header_font,
        )

        for proposal_index in range(proposal_count):
            left = x + proposal_index * cell_width
            right = left + cell_width
            draw.rectangle(
                (left, mission_header_height, right, header_height),
                fill="#f5f5f5",
                outline="#8a8a8a",
            )
            _center_text(
                draw,
                (left, mission_header_height, right, header_height),
                f"P{proposal_index + 1}",
                font=pick_font,
                fill="#444444",
            )
        x = mission_right

    for row_index, player in enumerate(ordered):
        y = header_height + row_index * row_height
        draw.rectangle(
            (0, y, name_width, y + row_height),
            fill="#f5f5f5",
            outline="#8a8a8a",
        )
        shown_name = player if len(player) <= 25 else player[:24] + "…"
        draw.text((8, y + 11), shown_name, fill="black", font=player_font)

        player_history = history.get(player) or []
        x = name_width
        for mission_index, pick_index in columns:
            token = ""
            if (
                mission_index < len(player_history)
                and pick_index < len(player_history[mission_index] or [])
            ):
                token = str(
                    player_history[mission_index][pick_index] or ""
                )

            if "VHapprove" in token:
                cell_fill = "#baf4ba"
            elif "VHreject" in token:
                cell_fill = "#f5b4b4"
            else:
                cell_fill = "white"

            cell_box = (x, y, x + cell_width, y + row_height)
            draw.rectangle(
                cell_box, fill=cell_fill, outline="#8a8a8a", width=1
            )

            # Color communicates approve/reject; a large black X marks
            # players who were proposed for the mission team.
            if "VHpicked" in token:
                _draw_black_x(draw, cell_box)

            if "VHleader" in token:
                draw.rectangle(
                    (
                        x + 2,
                        y + 2,
                        x + cell_width - 2,
                        y + row_height - 2,
                    ),
                    outline="#222222",
                    width=3,
                )
            x += cell_width

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
