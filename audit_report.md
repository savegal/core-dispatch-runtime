# Отчет аудита callback_data и handlers

Дата: 2026-03-21 15:43:24

## Статистика

- Всего callback_data в кнопках: 104
- Всего точных обработчиков: 95
- Всего префикс-обработчиков: 63

## ❌ МЕРТВЫЕ КНОПКИ (DEAD BUTTONS): 1

### `template:noop`
- `bot\keyboards\templates.py:103`
- `bot\keyboards\templates.py:103`

## ⚠️ НЕИСПОЛЬЗУЕМЫЕ ОБРАБОТЧИКИ (UNUSED HANDLERS): 14

### `admin:video_parsing`
- `bot\handlers\video_parsing.py:105`

### `composer:clear_links`
- `bot\handlers\composer.py:175`

### `composer:recent_links`
- `bot\handlers\composer.py:120`

### `menu:check_access`
- `bot\handlers\menu.py:440`

### `menu:help`
- `bot\handlers\menu.py:259`

### `menu:new_order:back`
- `bot\handlers\quick_order.py:57`

### `menu:settings`
- `bot\handlers\menu.py:590`

### `menu:subscription`
- `bot\handlers\menu.py:379`

### `orders:noop`
- `bot\handlers\orders.py:340`

### `quicktpl:noop`
- `bot\handlers\quick_order.py:834`

### `vp:auto_orders:list`
- `bot\handlers\video_parsing.py:915`

### `wizard:skip_save`
- `bot\handlers\order_wizard.py:1101`

### `wizard:start_new_order`
- `bot\handlers\order_wizard.py:167`

### `wizard:submit_order`
- `bot\handlers\order_wizard.py:1417`

## 📋 ПРЕФИКС-СООТВЕТСТВИЯ

### `admin:accounts:delete:dead`
Обрабатывается через префиксы: admin:accounts:delete:

### `admin:accounts:delete:error`
Обрабатывается через префиксы: admin:accounts:delete:

### `admin:accounts:delete:limited`
Обрабатывается через префиксы: admin:accounts:delete:

### `admin:accounts:export:active`
Обрабатывается через префиксы: admin:accounts:export:

### `admin:accounts:export:all`
Обрабатывается через префиксы: admin:accounts:export:

### `admin:accounts:export:dead`
Обрабатывается через префиксы: admin:accounts:export:

### `admin:accounts:export:error`
Обрабатывается через префиксы: admin:accounts:export:

### `admin:pricing:edit:failure`
Обрабатывается через префиксы: admin:pricing:edit:

### `admin:pricing:edit:success`
Обрабатывается через префиксы: admin:pricing:edit:

### `admin:proxy_settings:type:mobile`
Обрабатывается через префиксы: admin:proxy_settings:type:

### `admin:proxy_settings:type:none`
Обрабатывается через префиксы: admin:proxy_settings:type:

### `admin:proxy_settings:type:residential`
Обрабатывается через префиксы: admin:proxy_settings:type:

### `composer:comment:input`
Обрабатывается через префиксы: composer:comment:input

### `composer:input_links`
Обрабатывается через префиксы: composer:input_links

### `engagement:skip_reply:1`
Обрабатывается через префиксы: engagement:skip_reply:

### `engagement:skip_reply:2`
Обрабатывается через префиксы: engagement:skip_reply:

### `engagement:skip_reply:3`
Обрабатывается через префиксы: engagement:skip_reply:

### `vp:inbox:filter:done`
Обрабатывается через префиксы: vp:inbox:filter:

### `vp:inbox:filter:new`
Обрабатывается через префиксы: vp:inbox:filter:

### `vp:inbox:filter:queued`
Обрабатывается через префиксы: vp:inbox:filter:

### `vp:inbox:filter:skipped`
Обрабатывается через префиксы: vp:inbox:filter:

### `vp:review:list`
Обрабатывается через префиксы: vp:review:

### `wizard:reply_count:0`
Обрабатывается через префиксы: wizard:reply_count:

## 🔍 ПРЕФИКС-ОБРАБОТЧИКИ

### `admin:accounts:delete:*`
- `bot\handlers\admin.py:1003`

### `admin:accounts:delete_confirm:*`
- `bot\handlers\admin.py:1035`

### `admin:accounts:export:*`
- `bot\handlers\admin.py:916`

### `admin:accounts:page:*`
- `bot\handlers\admin.py:725`

### `admin:balance:add:*`
- `bot\handlers\admin.py:1628`

### `admin:balance:subtract:*`
- `bot\handlers\admin.py:1667`

### `admin:block:confirm:*`
- `bot\handlers\admin.py:1507`

### `admin:block:do:*`
- `bot\handlers\admin.py:1560`

### `admin:pricing:edit:*`
- `bot\handlers\admin.py:2232`

### `admin:proxies:page:*`
- `bot\handlers\admin_resources.py:174`

### `admin:proxy_settings:type:*`
- `bot\handlers\admin.py:2126`

### `admin:unblock:confirm:*`
- `bot\handlers\admin.py:1534`

### `admin:unblock:do:*`
- `bot\handlers\admin.py:1594`

### `admin:user:*`
- `bot\handlers\admin.py:1332`

### `admin:user:orders:*`
- `bot\handlers\admin.py:1861`

### `composer:comment:input*`
- `bot\handlers\composer.py:229`

### `composer:comment:last*`
- `bot\handlers\composer.py:339`

### `composer:comment:templates*`
- `bot\handlers\composer.py:241`

### `composer:input_links*`
- `bot\handlers\composer.py:80`

### `composer:preset:*`
- `bot\handlers\composer.py:191`

### `composer:select_recent:*`
- `bot\handlers\composer.py:150`

### `composer:select_template:*`
- `bot\handlers\composer.py:281`

### `composer:toggle:*`
- `bot\handlers\composer.py:372`

### `engagement:skip_reply:*`
- `bot\handlers\engagement_wizard.py:261`

### `menu:templates:page:*`
- `bot\handlers\menu.py:334`

### `order:copy:*`
- `bot\handlers\orders.py:632`

### `order:detail:*`
- `bot\handlers\orders.py:346`

### `order:retry_failed:*`
- `bot\handlers\orders.py:557`

### `order:texts:*`
- `bot\handlers\orders.py:698`

### `orders:page:*`
- `bot\handlers\orders.py:226`

### `quicktpl:confirm:*`
- `bot\handlers\quick_order.py:738`

### `quicktpl:edit:*`
- `bot\handlers\quick_order.py:176`

### `quicktpl:edit_comment:*`
- `bot\handlers\quick_order.py:624`

### `quicktpl:edit_replies:*`
- `bot\handlers\quick_order.py:669`

### `quicktpl:set_replies:*`
- `bot\handlers\quick_order.py:595`

### `template:apply:*`
- `bot\handlers\templates.py:93`

### `template:delete:*`
- `bot\handlers\templates.py:145`

### `template:delete_confirm:*`
- `bot\handlers\templates.py:190`

### `template:detail:*`
- `bot\handlers\templates.py:30`

### `template:edit:*`
- `bot\handlers\templates.py:223`

### `template:edit_comment:*`
- `bot\handlers\templates.py:365`

### `template:edit_likes:*`
- `bot\handlers\templates.py:736`

### `template:edit_name:*`
- `bot\handlers\templates.py:300`

### `template:edit_replies:*`
- `bot\handlers\templates.py:456`

### `template:edit_reply1:*`
- `bot\handlers\templates.py:516`

### `template:edit_reply2:*`
- `bot\handlers\templates.py:590`

### `template:edit_reply3:*`
- `bot\handlers\templates.py:663`

### `template:page:*`
- `bot\handlers\quick_order.py:145`

### `template:select:*`
- `bot\handlers\quick_order.py:119`

### `vp:creator:*`
- `bot\handlers\video_parsing.py:279`
- `bot\handlers\video_parsing.py:455`
- `bot\handlers\video_parsing.py:657`
- `bot\handlers\video_parsing.py:709`
- `bot\handlers\video_parsing.py:793`
- `bot\handlers\video_parsing.py:876`
- `bot\handlers\video_parsing.py:1110`
- `bot\handlers\video_parsing_new_videos.py:583`

### `vp:inbox:filter:*`
- `bot\handlers\video_inbox.py:244`

### `vp:inbox:video:*`
- `bot\handlers\video_inbox.py:361`

### `vp:list:page:*`
- `bot\handlers\video_parsing.py:248`

### `vp:new_videos:cancel:*`
- `bot\handlers\video_parsing_new_videos.py:540`

### `vp:new_videos:confirm_order:*`
- `bot\handlers\video_parsing_new_videos.py:266`

### `vp:new_videos:create_template:*`
- `bot\handlers\video_parsing_new_videos.py:470`

### `vp:new_videos:select_template:*`
- `bot\handlers\video_parsing_new_videos.py:22`

### `vp:new_videos:template_selected:*`
- `bot\handlers\video_parsing_new_videos.py:156`

### `vp:review:*`
- `bot\handlers\video_parsing.py:994`
- `bot\handlers\video_parsing.py:1055`
- `bot\handlers\video_parsing.py:1251`

### `vp:review:list:page:*`
- `bot\handlers\video_parsing.py:1315`

### `wizard:reply_count:*`
- `bot\handlers\order_wizard.py:398`

### `wizard:template:page:*`
- `bot\handlers\order_wizard.py:124`

### `wizard:template:select:*`
- `bot\handlers\order_wizard.py:32`

