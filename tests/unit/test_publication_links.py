"""Links retain the exact publication/parent and never invent public evidence."""
import base64
from urllib.parse import parse_qs, urlsplit

import pytest

from services.publication_links import comment_link

MEDIA, CID, PARENT = "7704037684280446216", "7704740811904828192", "7704740811904828100"


def cid_in_link(link):
    encoded = parse_qs(urlsplit(link).query)["cid"][0]
    return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()


@pytest.mark.parametrize("path", ["video", "photo", "share/video"])
def test_legacy_url_can_supply_the_media_id_without_losing_the_cid(path):
    link = comment_link(None, CID, f"https://www.tiktok.com/@fixture/{path}/{MEDIA}?old=1")
    assert urlsplit(link).path == f"/share/video/{MEDIA}"
    assert cid_in_link(link) == CID and "old=" not in link


@pytest.mark.parametrize("cid", [None, "", "123", "１２３４５６７８９０", '1" onclick="bad', "0" * 19])
def test_missing_or_invalid_cid_never_becomes_a_video_link_disguised_as_a_comment(cid):
    assert comment_link(MEDIA, cid, f"https://www.tiktok.com/@fixture/video/{MEDIA}") == ""


def test_untrusted_receipt_url_cannot_change_the_link_host_or_inject_markup():
    for raw in (f"https://tiktok.com.attacker.invalid/video/{MEDIA}", "javascript:bad()", "https://[broken"):
        assert comment_link(None, CID, raw) == ""
    assert comment_link(MEDIA, CID, "javascript:bad()").startswith("https://www.tiktok.com/")


def test_live_receipt_links_reply_and_parent_without_claiming_public_visibility():
    from bot.services.live_orders import render_live
    from tests.unit.test_live_order_presentation import activity, validate_card
    row = dict(kind="reply", comment_id=CID, parent_id=PARENT, aweme_id=MEDIA,
               text="reply", status="accepted", visibility="unchecked", link="javascript:bad()")
    text = render_live(activity(latest=[row]))
    assert f'href="{comment_link(MEDIA, CID)}"' in text
    assert f'href="{comment_link(MEDIA, PARENT)}"' in text
    assert "Открыть ответ" in text and "Родитель</a>" in text
    assert "javascript:" not in text and "Цель подтверждена" not in text
    validate_card(text)
    row["comment_id"] = None
    assert "<a " not in render_live(activity(latest=[row]))


def test_every_publication_in_the_map_has_its_own_link_and_fits_telegram():
    from bot.handlers.orders import _order_detail_page_count, _render_order_detail
    from tests.unit.test_live_order_presentation import validate_card
    url = f"https://www.tiktok.com/@fixture/video/{MEDIA}"
    replies = [dict(text=f"reply {index} " + "<&>" * 200, reply_comment_id=str(int(CID) + index),
                    parent_comment_id=PARENT, success=True, public_confirmed=False)
               for index in range(1, 21)]
    task = dict(id=12, kind="full", status="done", progress=1, video_urls=[url], settings={
        "publication_plan": dict(main_count=1, replies_per_main=20), "url_results": {url: {
            "aweme_id": MEDIA, "engagement": {"main": {"variants_published": [dict(text="main",
                comment_id=PARENT, public_confirmed=True)]}, "replies": {"items": replies}}}}})
    pages = [_render_order_detail(task, "map", page) for page in range(_order_detail_page_count(task, "map"))]
    for page in pages:
        validate_card(page)
        assert len(page) < 3600
    combined = "\n".join(pages)
    assert comment_link(MEDIA, PARENT) in combined
    assert all(comment_link(MEDIA, reply["reply_comment_id"]) in combined for reply in replies)


def test_export_and_journal_include_parent_link_without_changing_visibility(db_session, sample_task):
    from services.publication_reports import publication_rows, export_report
    from tests.unit.test_publication_observability import attempt
    sample_task.video_urls = [f"https://www.tiktok.com/@fixture/photo/{MEDIA}"]
    row = attempt(db_session, sample_task, kind="reply", comment_id=CID, parent_id=PARENT,
                  aweme_id=None, video_url=sample_task.video_urls[0])
    values = publication_rows(db_session, sample_task)[0]
    assert cid_in_link(values["link"]) == CID and cid_in_link(values["parent_link"]) == PARENT
    assert row.visibility == "unchecked"
    assert values["link"] in export_report(db_session, sample_task, "txt")
    assert values["parent_link"] in export_report(db_session, sample_task, "csv")
    assert values["link"] not in export_report(db_session, sample_task, "links")
    row.visibility = "visible"
    db_session.commit()
    assert values["link"] in export_report(db_session, sample_task, "links")
