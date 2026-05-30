"""Формат JSON как у VOID Parser (void-parser-result)."""

from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from parser.marketplace import MarketplaceListing


def void_export_item(listing: "MarketplaceListing") -> dict[str, Any]:
    """Один объект в массиве items — те же ключи, что у VOID."""
    return {
        "item_title": listing.title or "",
        "item_photo": listing.photo or "",
        "ads_number": listing.ads_number,
        "parser_views": listing.parser_views,
        "ads_number_bought": listing.ads_number_bought,
        "ads_number_sold": listing.ads_number_sold,
        "gender": listing.gender or "",
        "email": listing.email or "",
        "person_reg_date": listing.person_reg_date or "",
        "item_price": listing.price or "",
        "views": listing.views,
        "rating": listing.rating if listing.rating is not None else 0,
        "created_date": listing.created_date or "",
        "created_real_date": listing.created_real_date or "",
        "phone": listing.phone or "",
        "item_desc": (listing.item_desc or "").strip() or "N/A",
        "location": listing.location or "",
        "item_link": listing.link or "",
        "person_link": listing.person_link or "",
        "item_person_name": listing.seller_name or "",
    }


def listings_to_void_json(items: list["MarketplaceListing"]) -> str:
    return json.dumps(
        {"items": [void_export_item(x) for x in items]},
        ensure_ascii=False,
        indent=2,
    )
