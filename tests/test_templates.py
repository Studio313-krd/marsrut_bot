from __future__ import annotations

from app.templates import content_list


def content_item(*, title: str, subtitle: str) -> dict[str, str]:
    return {
        "id": "item-1",
        "title": title,
        "subtitle": subtitle,
        "path": "/material/item-1",
    }


def test_catalog_button_uses_material_title_and_cleans_description_html() -> None:
    message = content_list(
        "businesses",
        [
            content_item(
                title="Стерео Пикник",
                subtitle="<p>Музыкальный и гастрономический фестиваль<br>в парке</p>",
            )
        ],
        "https://example.test",
        0,
        False,
    )

    assert message.buttons[0][0].text == "Открыть Стерео Пикник"
    assert "Музыкальный и гастрономический фестиваль\nв парке" in message.text
    assert "<p>" not in message.text
    assert "&lt;p&gt;" not in message.text


def test_reels_never_show_descriptions() -> None:
    message = content_list(
        "reels",
        [content_item(title="Рилс. Вячеслав Марковский", subtitle="Описание из сайта")],
        "https://example.test",
        0,
        False,
    )

    assert message.buttons[0][0].text == "Открыть Рилс. Вячеслав Марковский"
    assert "Описание из сайта" not in message.text


def test_catalog_button_title_fits_platform_limit() -> None:
    message = content_list(
        "articles",
        [content_item(title="Очень длинное название материала " * 5, subtitle="")],
        "https://example.test",
        0,
        False,
    )

    label = message.buttons[0][0].text
    assert label.startswith("Открыть Очень длинное название")
    assert label.endswith("…")
    assert len(label) <= 64
