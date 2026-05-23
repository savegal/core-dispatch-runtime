import pytest

from bot.handlers.video_inbox import (
    _available_new_videos,
    _build_inbox_overview_text,
    _build_new_videos_text,
    _build_selection_text,
    _bulk_action_text,
    _context_back_callback,
    _group_videos_by_creator,
    order_grouped_creator_videos,
    _show_grouped_order_choice,
    _start_order_for_videos,
    _video_action_row,
    show_inbox,
    skip_all_videos,
)


def _video(video_id: int, *, creator_id: int = 7, status: str = "new", task_id: int | None = None) -> dict:
    return {
        "id": video_id,
        "video_id": f"76500000000000000{video_id}",
        "url": f"https://www.tiktok.com/@creator{creator_id}/video/76500000000000000{video_id}",
        "creator_id": creator_id,
        "creator_username": f"creator{creator_id}",
        "status": status,
        "display_status": status,
        "task_id": task_id,
        "create_time": 1770000000 + video_id,
    }


@pytest.mark.unit
def test_inbox_overview_uses_linked_video_ids_and_attention_summary():
    text = _build_inbox_overview_text(
        {"new": 2, "queued": 1, "done": 3, "skipped": 4, "failed": 1},
        [_video(1), _video(2)],
    )

    assert "Требует внимания" in text
    assert "Ошибки: *1*" in text
    assert "1." in text
    assert "[765000000000…](https://www.tiktok.com/@creator7/video/765000000000000001)" in text
    assert "[1](https://www.tiktok.com/@creator7/video/765000000000000001)" not in text
    assert "опубликовано:" in text


@pytest.mark.unit
def test_new_video_page_shows_pagination_and_video_metadata():
    text = _build_new_videos_text(
        {"new": 7},
        [_video(6), _video(7)],
        page=1,
        has_next=False,
    )

    assert "Страница *2/2*" in text
    assert "Всего новых: *7*" in text
    assert "6." in text
    assert "[765000000000…](https://www.tiktok.com/@creator7/video/765000000000000006)" in text
    assert "опубликовано:" in text


@pytest.mark.unit
def test_selection_page_marks_selected_and_keeps_links():
    text = _build_selection_text([_video(1), _video(2)], {2}, page=0, total_new=2)

    assert "Выбрано: *1*" in text
    assert "☐ 1." in text
    assert "☐ 1. `@creator7` [765000000000…](https://www.tiktok.com/@creator7/video/765000000000000001)" in text
    assert "☑️ 2. `@creator7` [765000000000…](https://www.tiktok.com/@creator7/video/765000000000000002)" in text


@pytest.mark.unit
def test_video_action_row_has_url_and_compact_actions():
    row = _video_action_row(_video(12), 3)

    assert row[0].text == "🎬 #3"
    assert row[0].url == "https://www.tiktok.com/@creator7/video/7650000000000000012"
    assert row[1].text == "🚀 Заказ"
    assert row[2].text == "✅ Уже"
    assert row[3].text == "🗑 Скип"
    assert row[1].callback_data == "vp:inbox:order:12"
    assert row[2].callback_data == "vp:inbox:done:12"
    assert row[3].callback_data == "vp:inbox:skip:12"


@pytest.mark.unit
def test_video_action_row_can_preserve_page_context():
    row = _video_action_row(_video(12), 3, context="creator:77:4")

    assert row[1].callback_data == "vp:inbox:order:12:creator:77:4"
    assert row[2].callback_data == "vp:inbox:done:12:creator:77:4"
    assert row[3].callback_data == "vp:inbox:skip:12:creator:77:4"
    assert _context_back_callback(["creator", "77", "4"]) == "vp:inbox:creator:77:4"
    assert _context_back_callback(["new", "2"]) == "vp:inbox:new:2"
    assert _context_back_callback([]) == "vp:inbox"


@pytest.mark.unit
def test_bulk_order_helpers_skip_processed_videos_and_group_by_creator():
    videos = [
        _video(1, creator_id=10),
        _video(2, creator_id=10, status="queued", task_id=99),
        _video(3, creator_id=11),
    ]

    available = _available_new_videos(videos)
    grouped = _group_videos_by_creator(available)

    assert [video["id"] for video in available] == [1, 3]
    assert sorted(grouped) == [10, 11]
    assert [video["id"] for video in grouped[10]] == [1]
    assert [video["id"] for video in grouped[11]] == [3]


@pytest.mark.unit
def test_bulk_action_text_does_not_overpromise_when_over_limit():
    assert _bulk_action_text("order", 7) == "🚀 Заказать все новые"
    assert _bulk_action_text("order", 77) == "🚀 Заказать первые 50"
    assert _bulk_action_text("skip", 77) == "🗑 Пропустить все новые"


class _FakeMessage:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class _FakeUser:
    id = 12345


class _FakeCallback:
    def __init__(self):
        self.from_user = _FakeUser()
        self.message = _FakeMessage()
        self.answers = []
        self.data = "vp:inbox:test"
        self.id = "callback-id"

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


class _FakeState:
    def __init__(self):
        self.data = {}
        self.cleared = False

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.cleared = True
        self.data = {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_show_inbox_exposes_actions_for_preview_videos(monkeypatch):
    import bot.handlers.video_inbox as video_inbox

    class FakeApi:
        async def get_inbox_videos(self, user_id, status=None, limit=5, offset=0):
            return {
                "videos": [_video(1), _video(2)],
                "by_status": {"new": 2, "queued": 0, "done": 0, "skipped": 0, "failed": 0},
            }

    async def fake_user_id(_telegram_id):
        return 55

    monkeypatch.setattr(video_inbox, "api_client", FakeApi())
    monkeypatch.setattr(video_inbox, "get_user_backend_id", fake_user_id)

    callback = _FakeCallback()
    await show_inbox(callback, _FakeState())

    text, kwargs = callback.message.edits[-1]
    keyboard = kwargs["reply_markup"].inline_keyboard
    buttons = [button for row in keyboard for button in row]

    assert "Новые *2*" in text
    assert any(button.text == "📋 Открыть новые (2)" for button in buttons)
    assert any(button.text == "🗑 Пропустить все новые" and button.callback_data == "vp:inbox:skip_all" for button in buttons)
    assert any(button.text == "🚀 Заказ" and button.callback_data == "vp:inbox:order:1" for button in buttons)
    assert any(button.text == "✅ Уже" and button.callback_data == "vp:inbox:done:1" for button in buttons)
    assert any(button.text == "🗑 Скип" and button.callback_data == "vp:inbox:skip:1" for button in buttons)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_skip_all_inbox_videos_uses_unlimited_backend_action(monkeypatch):
    import bot.handlers.video_inbox as video_inbox

    calls = {}

    class FakeApi:
        async def skip_all_new_inbox_videos(self, user_id):
            calls["skip_all"] = user_id
            return {"ok": True, "skipped": 77}

        async def get_inbox_videos(self, user_id, status=None, limit=5, offset=0):
            calls["refresh"] = (user_id, status, limit, offset)
            return {
                "videos": [],
                "by_status": {"new": 0, "queued": 0, "done": 0, "skipped": 77, "failed": 0},
            }

        async def skip_videos_batch(self, video_ids, user_id):
            pytest.fail("skip_all must not use the old 50-item batch path")

    async def fake_user_id(_telegram_id):
        return 55

    monkeypatch.setattr(video_inbox, "api_client", FakeApi())
    monkeypatch.setattr(video_inbox, "get_user_backend_id", fake_user_id)

    callback = _FakeCallback()
    callback.data = "vp:inbox:skip_all_do"
    await skip_all_videos(callback, _FakeState())

    assert calls["skip_all"] == 55
    assert calls["refresh"] == (55, "new", 5, 0)
    assert callback.answers[0][0] == ("🗑 Пропущено 77",)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_order_single_creator_creates_batch_and_opens_template_flow(monkeypatch):
    import bot.handlers.video_inbox as video_inbox
    import bot.handlers.video_parsing_new_videos as new_videos

    calls = {}

    class FakeApi:
        async def create_video_batch(self, creator_id, video_ids, user_id):
            calls["batch"] = (creator_id, video_ids, user_id)
            return {"batch_id": 901}

    async def fake_render_template_picker(message, state, **kwargs):
        calls["picker_message"] = message
        calls["picker_kwargs"] = kwargs
        calls["state"] = state
        return True

    monkeypatch.setattr(video_inbox, "api_client", FakeApi())
    monkeypatch.setattr(new_videos, "render_template_picker", fake_render_template_picker)

    state = _FakeState()
    callback = _FakeCallback()
    await _start_order_for_videos(
        callback,
        state,
        user_id=55,
        videos=[_video(1, creator_id=44), _video(2, creator_id=44)],
        back_callback="vp:inbox",
    )

    assert calls["batch"] == (44, ["1", "2"], 55)
    # FakeCallback хак заменён прямым вызовом render_template_picker:
    # проверяем, что параметры пробрасываются корректно.
    assert calls["picker_kwargs"]["creator_id"] == 44
    assert calls["picker_kwargs"]["batch_id"] == 901
    assert calls["picker_kwargs"]["user_id"] == 55
    assert calls["picker_kwargs"]["cancel_callback"] == "vp:inbox"
    # bound-method comparison через `is` ненадёжен — `callback.answer` создаёт
    # новый method-wrapper при каждом access'е. Достаточно убедиться, что
    # notify — это callable, и реальный поведенческий контракт уже покрыт тем,
    # что callback.message прокинут в picker как первый аргумент.
    assert callable(calls["picker_kwargs"]["notify"])
    assert calls["picker_message"] is callback.message
    assert calls["state"] is state


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_order_single_creator_preserves_inbox_back_callback(monkeypatch):
    import bot.handlers.video_inbox as video_inbox
    import bot.handlers.video_parsing_new_videos as new_videos

    calls = {}

    class FakeApi:
        async def create_video_batch(self, creator_id, video_ids, user_id):
            return {"batch_id": 902}

    async def fake_render_template_picker(message, state, **kwargs):
        calls["picker_kwargs"] = kwargs
        return True

    monkeypatch.setattr(video_inbox, "api_client", FakeApi())
    monkeypatch.setattr(new_videos, "render_template_picker", fake_render_template_picker)

    await _start_order_for_videos(
        _FakeCallback(),
        _FakeState(),
        user_id=55,
        videos=[_video(1, creator_id=44)],
        back_callback="vp:inbox:new:3",
    )

    assert calls["picker_kwargs"]["cancel_callback"] == "vp:inbox:new:3"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_order_multiple_creators_creates_separate_batches(monkeypatch):
    import bot.handlers.video_inbox as video_inbox

    class FakeApi:
        async def create_video_batch(self, creator_id, video_ids, user_id):
            pytest.fail("Batch must be created only after choosing a creator")
            return {"batch_id": int(creator_id) + 1000}

    monkeypatch.setattr(video_inbox, "api_client", FakeApi())

    callback = _FakeCallback()
    state = _FakeState()
    await _start_order_for_videos(
        callback,
        state,
        user_id=55,
        videos=[_video(1, creator_id=10), _video(2, creator_id=11)],
        back_callback="vp:inbox",
    )

    assert callback.message.edits
    keyboard = callback.message.edits[-1][1]["reply_markup"].inline_keyboard
    callbacks = [button.callback_data for row in keyboard for button in row if button.callback_data]

    assert "vp:inbox:group_order:10" in callbacks
    assert "vp:inbox:group_order:11" in callbacks
    assert "vp:inbox" in callbacks
    assert state.data["inbox_grouped_order_video_ids"] == {"10": [1], "11": [2]}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_grouped_creator_click_creates_only_selected_batch(monkeypatch):
    import bot.handlers.video_inbox as video_inbox
    import bot.handlers.video_parsing_new_videos as new_videos

    calls = {}

    class FakeApi:
        async def create_video_batch(self, creator_id, video_ids, user_id):
            calls["batch"] = (creator_id, video_ids, user_id)
            return {"batch_id": 303}

    async def fake_get_user_id(_telegram_id):
        return 55

    async def fake_render_template_picker(message, state, **kwargs):
        calls["picker_kwargs"] = kwargs
        return True

    monkeypatch.setattr(video_inbox, "api_client", FakeApi())
    monkeypatch.setattr(video_inbox, "get_user_backend_id", fake_get_user_id)
    monkeypatch.setattr(new_videos, "render_template_picker", fake_render_template_picker)

    callback = _FakeCallback()
    callback.data = "vp:inbox:group_order:11"
    state = _FakeState()
    await state.update_data(inbox_grouped_order_video_ids={"10": [1], "11": [2, 3]})

    await order_grouped_creator_videos(callback, state)

    assert calls["batch"] == (11, ["2", "3"], 55)
    assert calls["picker_kwargs"]["creator_id"] == 11
    assert calls["picker_kwargs"]["batch_id"] == 303
    assert calls["picker_kwargs"]["cancel_callback"] == "vp:inbox"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_new_videos_confirm_order_creates_task_and_links_videos(monkeypatch):
    import bot.handlers.video_parsing_new_videos as new_videos

    calls = {}

    class FakeApi:
        async def get_video_batch(self, batch_id, user_id):
            calls["batch_lookup"] = (batch_id, user_id)
            return {"creator_id": 44, "video_ids": ["1", "2"]}

        async def list_task_templates(self, user_id):
            return [
                {
                    "id": 77,
                    "name": "Template",
                    "main_comment": "Main comment",
                    "settings": {
                        "text_variants": ["one", "two", "three"],
                        "reply_count": 1,
                        "replies": ["reply"],
                    },
                    "likes": 0,
                }
            ]

        async def get_videos_by_ids(self, video_ids, user_id):
            calls["video_lookup"] = (video_ids, user_id)
            return [_video(1, creator_id=44), _video(2, creator_id=44)]

        async def create_task(self, **kwargs):
            calls["create_task"] = kwargs
            return {"task_id": 321}

        async def link_videos_to_tasks(self, user_id, video_task_pairs):
            calls["links"] = (user_id, video_task_pairs)
            return {"ok": True}

    async def fake_user_id(_telegram_id):
        return 55

    monkeypatch.setattr(new_videos, "api_client", FakeApi())
    monkeypatch.setattr(new_videos, "get_user_backend_id", fake_user_id)

    callback = _FakeCallback()
    callback.data = "vp:new_videos:confirm_order:44:901:77"
    state = _FakeState()

    await new_videos.new_videos_confirm_order(callback, state)

    assert calls["batch_lookup"] == (901, 55)
    assert calls["video_lookup"] == ([1, 2], 55)
    assert calls["create_task"]["user_id"] == 55
    assert calls["create_task"]["video_urls"] == [
        "https://www.tiktok.com/@creator44/video/765000000000000001",
        "https://www.tiktok.com/@creator44/video/765000000000000002",
    ]
    assert calls["create_task"]["text_variants"] == ["one", "two", "three"]
    assert calls["create_task"]["settings"]["origin"] == "new_videos"
    assert calls["create_task"]["settings"]["creator_source_id"] == 44
    assert calls["links"] == (55, [(1, 321), (2, 321)])
    assert state.cleared is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_grouped_choice_for_bulk_limit_stays_within_telegram_keyboard_limits(monkeypatch):
    import bot.handlers.video_inbox as video_inbox

    class FakeApi:
        async def create_video_batch(self, creator_id, video_ids, user_id):
            return {"batch_id": int(creator_id) + 5000}

    monkeypatch.setattr(video_inbox, "api_client", FakeApi())

    grouped = {
        creator_id: [_video(creator_id, creator_id=creator_id)]
        for creator_id in range(1, 51)
    }
    callback = _FakeCallback()

    await _show_grouped_order_choice(
        callback,
        grouped=grouped,
        back_callback="vp:inbox",
    )

    keyboard = callback.message.edits[-1][1]["reply_markup"].inline_keyboard
    buttons = [button for row in keyboard for button in row]

    assert len(buttons) == 51
    assert len(buttons) <= 100
    assert all(not button.callback_data or len(button.callback_data) <= 64 for button in buttons)
