"""
Единый модуль для всех callback_data в боте.
Все callback_data должны быть определены здесь для избежания ошибок.
"""

# ========== ГЛАВНОЕ МЕНЮ ==========
MENU_MAIN = "menu:main"
MENU_NEW_ORDER = "menu:new_order"
MENU_ORDERS = "menu:orders"
MENU_TEMPLATES = "menu:templates"
MENU_SETTINGS = "menu:settings"
MENU_HELP = "menu:help"
MENU_SUBSCRIPTION = "menu:subscription"
MENU_ADMIN = "menu:admin"
MENU_CHECK_ACCESS = "menu:check_access"

# ========== ЗАКАЗЫ ==========
ORDER_CREATE = "order:create"
ORDER_LIST = "order:list"
ORDER_DETAIL = "order:detail"
ORDER_COPY = "order:copy"
ORDER_CANCEL = "order:cancel"
ORDER_REFRESH = "order:refresh"

def order_detail(order_id: int) -> str:
    return f"{ORDER_DETAIL}:{order_id}"

def order_copy(order_id: int) -> str:
    return f"{ORDER_COPY}:{order_id}"

def order_cancel(order_id: int) -> str:
    return f"{ORDER_CANCEL}:{order_id}"

# ========== МАСТЕР СОЗДАНИЯ ЗАКАЗА ==========
WIZARD_LOAD_FILE = "wizard:load_file"
WIZARD_TEMPLATE = "wizard:template"
WIZARD_REPLY_MODE = "wizard:reply_mode"
WIZARD_CONFIRM = "wizard:confirm"
WIZARD_BACK = "wizard:back"
WIZARD_CANCEL = "wizard:cancel"
WIZARD_SKIP_REPLY3 = "wizard:skip_reply3"

def wizard_template(template_id: int) -> str:
    return f"{WIZARD_TEMPLATE}:{template_id}"

def wizard_reply_mode(mode: str) -> str:
    return f"{WIZARD_REPLY_MODE}:{mode}"

# ========== ШАБЛОНЫ ==========
TEMPLATE_CREATE = "template:create"
TEMPLATE_SELECT = "template:select"
TEMPLATE_DETAIL = "template:detail"
TEMPLATE_APPLY = "template:apply"
TEMPLATE_EDIT = "template:edit"
TEMPLATE_DELETE = "template:delete"
TEMPLATE_SET_DEFAULT = "template:set_default"
TEMPLATE_BACK = "template:back"
TEMPLATE_CANCEL = "template:cancel"

def template_select(template_id: int) -> str:
    return f"{TEMPLATE_SELECT}:{template_id}"

def template_detail(template_id: int) -> str:
    return f"{TEMPLATE_DETAIL}:{template_id}"

def template_apply(template_id: int) -> str:
    return f"{TEMPLATE_APPLY}:{template_id}"

def template_edit(template_id: int) -> str:
    return f"{TEMPLATE_EDIT}:{template_id}"

def template_delete(template_id: int) -> str:
    return f"{TEMPLATE_DELETE}:{template_id}"

def template_set_default(template_id: int) -> str:
    return f"{TEMPLATE_SET_DEFAULT}:{template_id}"

# ========== НАСТРОЙКИ ==========
SETTINGS_TOGGLE_DELAYS = "settings:toggle_delays"
SETTINGS_TOGGLE_SHUFFLE = "settings:toggle_shuffle"
SETTINGS_TOGGLE_ROTATE = "settings:toggle_rotate"
SETTINGS_REPLY_MODE = "settings:reply_mode"
SETTINGS_TOGGLE_LIKES = "settings:toggle_likes"

def settings_reply_mode(mode: str) -> str:
    return f"{SETTINGS_REPLY_MODE}:{mode}"

# ========== АДМИН-ПАНЕЛЬ ==========
ADMIN_MAIN = "admin:main"
ADMIN_USERS = "admin:users"
ADMIN_ACCOUNTS = "admin:accounts"
ADMIN_PROXIES = "admin:proxies"
ADMIN_RESOURCES = "admin:resources"
ADMIN_QUEUE = "admin:queue"
ADMIN_ACCOUNTS_IMPORT = "admin:accounts:import"
ADMIN_ACCOUNTS_LIST = "admin:accounts:list"
ADMIN_ACCOUNTS_ASSIGN_PROXIES = "admin:accounts:assign_proxies"
ADMIN_PROXIES_IMPORT = "admin:proxies:import"
ADMIN_PROXIES_LIST = "admin:proxies:list"
ADMIN_RESOURCES_CLEAN_ACCOUNTS = "admin:resources:clean:accounts"
ADMIN_RESOURCES_CLEAN_PROXIES = "admin:resources:clean:proxies"
ADMIN_RESOURCES_DEAD_ACCOUNTS = "admin:resources:dead:accounts"
ADMIN_RESOURCES_BAD_PROXIES = "admin:resources:bad:proxies"
ADMIN_USER = "admin:user"
ADMIN_GRANT = "admin:grant"
ADMIN_REVOKE = "admin:revoke"

def admin_user(user_id: int) -> str:
    return f"{ADMIN_USER}:{user_id}"

def admin_grant(user_id: int) -> str:
    return f"{ADMIN_GRANT}:{user_id}"

def admin_revoke(user_id: int) -> str:
    return f"{ADMIN_REVOKE}:{user_id}"

# ========== COMPOSER (старый, для совместимости) ==========
COMPOSER_INPUT_LINKS = "composer:input_links"
COMPOSER_LOAD_FILE = "composer:load_file"
COMPOSER_PRESET = "composer:preset"
COMPOSER_COMMENT_INPUT = "composer:comment:input"
COMPOSER_REPLY_MODE = "composer:reply_mode"
COMPOSER_LIKES = "composer:likes"
COMPOSER_TOGGLE = "composer:toggle"
COMPOSER_SUBMIT = "composer:submit"
COMPOSER_SAVE_TEMPLATE = "composer:save_template"
COMPOSER_CANCEL = "composer:cancel"

# ========== УТИЛИТЫ ==========
def parse_callback(callback_data: str) -> tuple[str, list[str]]:
    """Парсит callback_data и возвращает (префикс, аргументы)."""
    parts = callback_data.split(":")
    if len(parts) == 1:
        return parts[0], []
    return parts[0], parts[1:]
