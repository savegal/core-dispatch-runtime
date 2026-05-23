===================================================
BAS СКРИПТ ДЛЯ TIKTOK КОММЕНТИРОВАНИЯ
===================================================

КАК СОЗДАТЬ СКРИПТ В BAS:
---------------------------

BAS скрипты создаются визуально в редакторе BAS.
Ниже - пошаговая инструкция что именно нажимать.

ШАГ 1: Открыть BAS, нажать "New Script"

ШАГ 2: В левой панели найти "Threads" и создать поток с именем "Main"

ШАГ 3: Добавить блок "HTTP Server" (это встроенная функция BAS):
  - Drag & Drop блок "CreateServer" в поток
  - Параметры:
    * Port: 8765
    * Variable: server1

ШАГ 4: Добавить блок "Wait For Request":
  - Drag & Drop блок "WaitForRequest"
  - Server: server1
  - Path: /post_comment
  - Method: POST
  - Variable для тела запроса: request_body

ШАГ 5: Добавить блок "Javascript Execute":
  - Вставить код (смотри файл TikTokCommentBot.js)

ШАГ 6: Добавить блок "Send Response":
  - Variable: response_json

ШАГ 7: Зациклить (перейти на Шаг 4)

---
ВМЕСТО ЭТОГО ВСЕГО:

Проще всего использовать готовый JavaScript-подход.
Смотри файл TikTokCommentBot.js - его нужно вставить
в блок "Javascript Execute" в BAS.

Или используй Python HTTP-сервер как прослойку (bas_http_server.py)
который запускается отдельно и управляет BAS через bas-remote-python.
