import telebot
from telebot import types
import os
import subprocess
import time
import threading
import sqlite3
import logging
import traceback
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN in the environment.")
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

UPLOAD_FOLDER = "uploaded_files"
DB_FILE = "bot_data.db"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def update_db_structure():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    try:
        cursor.execute('''CREATE TABLE IF NOT EXISTS files_new
                        (id INTEGER PRIMARY KEY, filename TEXT, user_id INTEGER, upload_time TIMESTAMP)''')
        
        cursor.execute('''INSERT OR IGNORE INTO files_new (id, filename, user_id, upload_time)
                         SELECT id, filename, user_id, upload_time FROM files''')
        
        cursor.execute('DROP TABLE IF EXISTS files')
        cursor.execute('ALTER TABLE files_new RENAME TO files')
        
        conn.commit()
        logger.info("تم تحديث هيكل جدول الملفات بنجاح")
    except Exception as e:
        logger.error(f"خطأ في تحديث هيكل قاعدة البيانات: {e}")
        conn.rollback()
    finally:
        conn.close()

def init_db():
    # حذف ملف قاعدة البيانات القديم إذا كان موجوداً
    if os.path.exists(DB_FILE):
        try:
            os.remove(DB_FILE)
            logger.info("تم حذف ملف قاعدة البيانات القديم")
        except Exception as e:
            logger.error(f"خطأ في حذف ملف قاعدة البيانات: {e}")
    
    # إنشاء اتصال جديد بقاعدة البيانات
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    try:
        # إنشاء الجداول من جديد
        cursor.execute('''CREATE TABLE files
                         (id INTEGER PRIMARY KEY, 
                         filename TEXT, 
                         user_id INTEGER, 
                         upload_time TIMESTAMP)''')
        
        cursor.execute('''CREATE TABLE admins
                         (id INTEGER PRIMARY KEY, 
                         user_id INTEGER UNIQUE, 
                         added_by INTEGER, 
                         added_time TIMESTAMP)''')
        
        cursor.execute('''CREATE TABLE banned_users
                         (id INTEGER PRIMARY KEY, 
                         user_id INTEGER UNIQUE, 
                         banned_by INTEGER, 
                         ban_time TIMESTAMP, 
                         reason TEXT)''')
        
        cursor.execute('''CREATE TABLE force_subscribe
                         (id INTEGER PRIMARY KEY, 
                         channel_id TEXT UNIQUE, 
                         channel_username TEXT, 
                         added_by INTEGER, 
                         added_time TIMESTAMP)''')
        
        cursor.execute('''CREATE TABLE bot_settings
                         (id INTEGER PRIMARY KEY, 
                         setting_key TEXT UNIQUE, 
                         setting_value TEXT)''')
        
        cursor.execute('''CREATE TABLE paid_users
                         (id INTEGER PRIMARY KEY, 
                         user_id INTEGER UNIQUE, 
                         expiry_time TIMESTAMP, 
                         added_by INTEGER, 
                         added_time TIMESTAMP)''')
        
        # إضافة الإعدادات الافتراضية
        default_settings = [
            ('free_mode', 'enabled'),
            ('paid_mode', 'disabled'),
            ('bot_status', 'enabled')
        ]
        
        cursor.executemany("INSERT INTO bot_settings (setting_key, setting_value) VALUES (?, ?)", default_settings)
        
        conn.commit()
        logger.info("تم إنشاء قاعدة البيانات الجديدة بنجاح")
        
    except Exception as e:
        logger.error(f"خطأ في إنشاء قاعدة البيانات: {e}")
        conn.rollback()
    finally:
        conn.close()

init_db()

running_processes = {}
developer = "@PV_Parwani_4k"
DEVELOPER_ID = 8204959327 

def db_execute(query, params=()):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(query, params)
    conn.commit()
    conn.close()

def db_fetchone(query, params=()):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(query, params)
    result = cursor.fetchone()
    conn.close()
    return result

def db_fetchall(query, params=()):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(query, params)
    result = cursor.fetchall()
    conn.close()
    return result

def is_admin(user_id):
    result = db_fetchone("SELECT user_id FROM admins WHERE user_id = ?", (user_id,))
    return result is not None or user_id == DEVELOPER_ID

def bot_enabled():
    result = db_fetchone("SELECT setting_value FROM bot_settings WHERE setting_key = 'bot_status'")
    return result and result[0] == 'enabled'

def is_paid_mode():
    result = db_fetchone("SELECT setting_value FROM bot_settings WHERE setting_key = 'paid_mode'")
    return result and result[0] == 'enabled'

def is_user_paid(user_id):
    if user_id == DEVELOPER_ID:
        return True
    result = db_fetchone("SELECT expiry_time FROM paid_users WHERE user_id = ?", (user_id,))
    if result:
        expiry_time = datetime.strptime(result[0], '%Y-%m-%d %H:%M:%S')
        if datetime.now() < expiry_time:
            return True
        else:
            db_execute("DELETE FROM paid_users WHERE user_id = ?", (user_id,))
    return False

def check_subscription(user_id):
    channels = db_fetchall("SELECT channel_id FROM force_subscribe")
    if not channels:
        return True
    
    for channel in channels:
        try:
            member = bot.get_chat_member(channel[0], user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                return False
        except Exception as e:
            logger.error(f"Error checking subscription: {e}")
            return False
    
    return True

def main_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)

    btn_upload = types.InlineKeyboardButton("❀ رفع ملف ❀", callback_data="upload", style="success")
    btn_delete = types.InlineKeyboardButton("❀ حذف ملف ❀", callback_data="delete_file", style="success")
    btn_install = types.InlineKeyboardButton("❀ تحميل مكتبة ❀", callback_data="install_lib", style="success")
    btn_create = types.InlineKeyboardButton("❀ إنشاء بوت ❀", callback_data="make_bot", style="primary")
    btn_stop = types.InlineKeyboardButton("❀ إيقاف بوت ❀", callback_data="stop_one", style="success")
    btn_start = types.InlineKeyboardButton("❀ تشغيل بوت ❀", callback_data="start_one", style="success")
    btn_myfiles = types.InlineKeyboardButton("❀ ملفاتي ❀", callback_data="list_files", style="danger")
    btn_admin = types.InlineKeyboardButton("♛ لوحة الأدمن ♛", callback_data="admin_panel", style="danger")
    btn_dev = types.InlineKeyboardButton("« الـمـطـور » ", url=f"https://t.me/VZZDZ", style="danger")
    
    markup.add(btn_upload, btn_delete)
    markup.add(btn_install, btn_create)
    markup.add(btn_stop, btn_start)
    markup.add(btn_myfiles, btn_admin)
    markup.add(btn_dev)
    
    return markup

def admin_panel():
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    btn_add_admin = types.InlineKeyboardButton("➕ إضافة أدمن", callback_data="add_admin")
    btn_remove_admin = types.InlineKeyboardButton("➖ حذف أدمن", callback_data="remove_admin")
    btn_get_files = types.InlineKeyboardButton("📁 جلب الملفات", callback_data="get_files")
    btn_ban_user = types.InlineKeyboardButton("⛔ حظر عضو", callback_data="ban_user")
    btn_unban_user = types.InlineKeyboardButton("✅ فك حظر عضو", callback_data="unban_user")
    btn_stats = types.InlineKeyboardButton("📊 الإحصائيات", callback_data="stats")
    btn_add_channel = types.InlineKeyboardButton("➕ إضافة قناة", callback_data="add_channel")
    btn_remove_channel = types.InlineKeyboardButton("➖ حذف قناة", callback_data="remove_channel")
    btn_list_channels = types.InlineKeyboardButton("📋 قنوات الاشتراك", callback_data="list_channels")
    btn_stop_bot = types.InlineKeyboardButton("⛔ إيقاف البوت", callback_data="stop_bot")
    btn_start_bot = types.InlineKeyboardButton("🟢 تشغيل البوت", callback_data="start_bot")
    btn_free_mode = types.InlineKeyboardButton("🆓 الوضع المجاني", callback_data="free_mode")
    btn_paid_mode = types.InlineKeyboardButton("💳 الوضع المدفوع", callback_data="paid_mode")
    btn_add_paid = types.InlineKeyboardButton("➕ دفع إشترك ", callback_data="add_paid_user")
    btn_back = types.InlineKeyboardButton("🔙 رجوع", callback_data="back_main")
    
    markup.add(btn_add_admin, btn_remove_admin)
    markup.add(btn_get_files, btn_ban_user)
    markup.add(btn_unban_user, btn_stats)
    markup.add(btn_add_channel, btn_remove_channel)
    markup.add(btn_list_channels, btn_stop_bot)
    markup.add(btn_start_bot, btn_free_mode)
    markup.add(btn_paid_mode, btn_add_paid)
    markup.add(btn_back)
    
    return markup

def file_control_panel(filename):
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    btn_edit = types.InlineKeyboardButton("🗞 تحرير •", callback_data=f"edit_{filename}", style="danger")
    btn_toggle = types.InlineKeyboardButton("⚙️ تشغيل/إيقاف •", callback_data=f"toggle_{filename}", style="danger")
    btn_delete = types.InlineKeyboardButton("🗑 حذف •", callback_data=f"delete_{filename}", style="danger")
    btn_download = types.InlineKeyboardButton("📥 تنزيل •", callback_data=f"download_{filename}", style="success")
    btn_back = types.InlineKeyboardButton("🔙 رجوع •", callback_data="back_files", style="success")
    
    markup.add(btn_edit, btn_toggle, btn_delete, btn_download, btn_back)
    
    return markup

@bot.message_handler(commands=["start"])
def start(message):
    user_id = message.from_user.id
    
    if not bot_enabled():
        bot.send_message(message.chat.id, "⛔ البوت معطل حاليًا. يرجى المحاولة لاحقًا.")
        return
    
    if is_paid_mode() and not is_user_paid(user_id) and not is_admin(user_id):
        paid_msg = (
            "💳 أنت غير مصرح لك باستخدام هذا الأمر.\n\n"
            "🏴‍☠ يرجى التواصل مع المطور للشراء: @VZZDZ\n\n"
            "💰 السعر\n"
            "🏴‍☠ إشتراك يومي: 250﷼يمني\n"
            "🏴‍☠ إشتراك أسبوعيًا: 500﷼يمني1$دولار\n"
            "🏴‍☠ إشتراك شهريًا: 1500﷼يمني3$دولار\n"
            "🏴‍☠ إشتراك سكربت بدون تشفير 100%: 2500﷼يمني5$"
        )
        markup = types.InlineKeyboardMarkup()
        btn_dev = types.InlineKeyboardButton("• الـمـطـور •", url="https://t.me/VZZDZ")
        btn_channel = types.InlineKeyboardButton("• قـنـاة الـمـطـور •", url="https://t.me/teenn15")
        markup.add(btn_dev, btn_channel)
        bot.send_message(message.chat.id, paid_msg, reply_markup=markup)
        return
    
    if not check_subscription(user_id):
        channels = db_fetchall("SELECT channel_id, channel_username FROM force_subscribe")
        if channels:
            markup = types.InlineKeyboardMarkup()
            for channel in channels:
                channel_id, channel_username = channel
                btn = types.InlineKeyboardButton(f"انضم هنا {channel_username}", url=f"https://t.me/{channel_username[1:]}")
                markup.add(btn)
            
            btn_check = types.InlineKeyboardButton("✅ تحقق من الاشتراك", callback_data="check_subscription")
            markup.add(btn_check)
            
            bot.send_message(message.chat.id, 
                            "📢 يرجى الاشتراك في القنوات التالية لاستخدام البوت:",
                            reply_markup=markup)
            return
    
    welcome_msg = (
        "🧸أهـلاً بـك فـي بـوت أسـتـضـافـة『Python Host』\n"
        "╔═══════════════════▢\n\n"
        "║ الـمـلـك 『 @PV_Parwani_4k 』🗞\n"
        "║ قـنـاة الـمـلـك 『 @AFG_Hacking_1387 』🛒\n"
        "║ بوتPHP 『 @Dictator_Hosting_bot 』🛒\n"
        "║\n"
        "╚═══════════════════▢\n"
        "📨▢════[استخدم الأزرارللـتـحـگـم]════▢📨"
    )
    bot.send_message(
        message.chat.id,
        welcome_msg,
        reply_markup=main_menu(),
    )

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    data = call.data
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    
    if not bot_enabled() and not data.startswith("start_bot"):
        bot.answer_callback_query(call.id, "⛔ البوت معطل حاليًا.")
        return
    
    if not check_subscription(user_id) and not data == "check_subscription":
        bot.answer_callback_query(call.id, "📢 يرجى الاشتراك في القنوات المطلوبة أولاً.")
        return
    
    elif data == "add_paid_user":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ ليس لديك صلاحية هذه العملية.")
            return
        msg = bot.edit_message_text("👤 أرسل آيدي المستخدم لجعل حسابه مدفوع:", chat_id, call.message.id)
        bot.register_next_step_handler(msg, add_paid_user_id_step)

    elif data == "upload":
        if is_paid_mode() and not is_user_paid(user_id) and not is_admin(user_id):
            bot.answer_callback_query(call.id, "💳 هذا البوت يعمل بالوضع المدفوع. يرجى التواصل مع المطور.")
            return           
        bot.edit_message_text("🗒 أرسل ملف Python (.py) وسأقوم برفع واجعله يعمل بشكل صحيح✅:", chat_id, call.message.id)

    elif data == "delete_file":
        msg = bot.edit_message_text("🗑 أرسل اسم الملف الذي تريد حذفه:", chat_id, call.message.id)
        bot.register_next_step_handler(msg, delete_file_step)

    elif data == "install_lib":
        msg = bot.edit_message_text("📦 أرسل اسم المكتبة التي تريد تحميلها (مثال: requests):", chat_id, call.message.id)
        bot.register_next_step_handler(msg, install_lib_step)

    elif data == "make_bot":
        msg = bot.edit_message_text("✏️ أرسل كود البوت بصيغة <code>.py</code>:", chat_id, call.message.id)
        bot.register_next_step_handler(msg, make_bot_step)

    elif data == "stop_one":
        msg = bot.edit_message_text("⛔ أرسل اسم البوت الذي تريد إيقافه:", chat_id, call.message.id)
        bot.register_next_step_handler(msg, stop_one_step)

    elif data == "start_one":
        msg = bot.edit_message_text("🟢 أرسل اسم البوت الذي تريد تشغيله:", chat_id, call.message.id)
        bot.register_next_step_handler(msg, start_one_step)

    elif data == "list_files":
        files = [f for f in os.listdir(UPLOAD_FOLDER) if f.endswith(".py") and (is_admin(user_id) or db_fetchone("SELECT filename FROM files WHERE filename = ? AND user_id = ?", (f, user_id)))]
        if not files:
            bot.edit_message_text("📂 لا توجد ملفات مرفوعة حالياً.", chat_id, call.message.id, reply_markup=main_menu())
            return
        
        msg = "📋 ملفاتك:\n\n"
        for f in files:
            status = "🟢 شغال" if f in running_processes else "🔴 متوقف"
            size = os.path.getsize(os.path.join(UPLOAD_FOLDER, f)) // 1024
            msg += f"• {f} ({size} KB) — {status}\n"
        
        markup = types.InlineKeyboardMarkup()
        for f in files:
            btn = types.InlineKeyboardButton(f"📁 {f}", callback_data=f"control_{f}")
            markup.add(btn)
        btn_back = types.InlineKeyboardButton("🔙 رجوع", callback_data="back_main")
        markup.add(btn_back)
        
        bot.edit_message_text(msg, chat_id, call.message.id, reply_markup=markup)

    elif data == "admin_panel":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ ليس لديك صلاحية الوصول إلى لوحة الأدمن.")
            return
        
        admin_msg = (
            "🧸أهـلاً بـك فـي لـوحـة ادامـن『𝗕𝐘』\n"
            "╔═══════════════════▢\n\n"
            "║ الـمـلـك.     『 @PV_Parwani_4k 』🗞\n"
            "║ قـنـاة الـمـلـك 『 @AFG_Hacking_1387 』🛒\n"
            "║ قـنـاة الـمـلـك 『 @AFG_Hacking_1387 』🛒\n"
            "║\n"
            "╚═══════════════════▢\n"
            "🗞••▢══[استخدم لوحةالادامن بلأسفل]══▢••🗞"
        )
        bot.edit_message_text(admin_msg, chat_id, call.message.id, reply_markup=admin_panel())

    elif data.startswith("control_"):
        filename = data.replace("control_", "")
        bot.edit_message_text(f"⚙️ تحكم في الملف: {filename}", chat_id, call.message.id, reply_markup=file_control_panel(filename))

    elif data.startswith("edit_"):
        filename = data.replace("edit_", "")
        msg = bot.edit_message_text(f"📝 أرسل الكود الجديد للملف: {filename}", chat_id, call.message.id)
        bot.register_next_step_handler(msg, edit_file_step, filename)

    elif data.startswith("toggle_"):
        filename = data.replace("toggle_", "")
        if filename in running_processes:
            running_processes[filename].terminate()
            del running_processes[filename]
            bot.answer_callback_query(call.id, f"⛔ تم إيقاف البوت: {filename}")
        else:
            path = os.path.join(UPLOAD_FOLDER, filename)
            if os.path.exists(path):
                p = subprocess.Popen(["python3", path])
                running_processes[filename] = p
                bot.answer_callback_query(call.id, f"🟢 تم تشغيل البوت: {filename}")
            else:
                bot.answer_callback_query(call.id, f"❌ الملف غير موجود: {filename}")
        
        bot.edit_message_reply_markup(chat_id, call.message.id, reply_markup=file_control_panel(filename))

    elif data.startswith("delete_"):
        filename = data.replace("delete_", "")
        path = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(path):
            if filename in running_processes:
                running_processes[filename].terminate()
                del running_processes[filename]
            os.remove(path)
            db_execute("DELETE FROM files WHERE filename = ?", (filename,))
            bot.answer_callback_query(call.id, f"🗑 تم حذف الملف: {filename}")
            bot.edit_message_text(f"🗑 تم حذف الملف: {filename}", chat_id, call.message.id, reply_markup=main_menu())
        else:
            bot.answer_callback_query(call.id, f"❌ الملف غير موجود: {filename}")

    elif data.startswith("download_"):
        filename = data.replace("download_", "")
        path = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(path):
            with open(path, 'rb') as f:
                bot.send_document(chat_id, f, caption=f"📥 الملف: {filename}")
            bot.answer_callback_query(call.id, f"📥 تم إرسال الملف: {filename}")
        else:
            bot.answer_callback_query(call.id, f"❌ الملف غير موجود: {filename}")

    elif data == "back_main":
        bot.edit_message_text("👋 أهلاً بك في <b>مدير البوتات المتطور</b>!\n\n🔽 استخدم الأزرار للتحكم:", chat_id, call.message.id, reply_markup=main_menu())

    elif data == "back_files":
        files = [f for f in os.listdir(UPLOAD_FOLDER) if f.endswith(".py") and (is_admin(user_id) or db_fetchone("SELECT filename FROM files WHERE filename = ? AND user_id = ?", (f, user_id)))]
        if not files:
            bot.edit_message_text("📂 لا توجد ملفات مرفوعة حالياً.", chat_id, call.message.id, reply_markup=main_menu())
            return
        
        msg = "📋 ملفاتك:\n\n"
        for f in files:
            status = "🟢 شغال" if f in running_processes else "🔴 متوقف"
            size = os.path.getsize(os.path.join(UPLOAD_FOLDER, f)) // 1024
            msg += f"• {f} ({size} KB) — {status}\n"
        
        markup = types.InlineKeyboardMarkup()
        for f in files:
            btn = types.InlineKeyboardButton(f"📁 {f}", callback_data=f"control_{f}")
            markup.add(btn)
        btn_back = types.InlineKeyboardButton("🔙 رجوع", callback_data="back_main")
        markup.add(btn_back)
        
        bot.edit_message_text(msg, chat_id, call.message.id, reply_markup=markup)

    elif data == "check_subscription":
        if check_subscription(user_id):
            bot.edit_message_text("✅ تم الاشتراك في جميع القنوات بنجاح!\n\nأهلاً بك في البوت:", chat_id, call.message.id, reply_markup=main_menu())
        else:
            bot.answer_callback_query(call.id, "❌ لم يتم الاشتراك في جميع القنوات بعد.")

    elif data == "add_admin":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ ليس لديك صلاحية هذه العملية.")
            return
        
        msg = bot.edit_message_text("👨‍💻 أرسل آيدي المستخدم لإضافته كمشرف:", chat_id, call.message.id)
        bot.register_next_step_handler(msg, add_admin_step)

    elif data == "remove_admin":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ ليس لديك صلاحية هذه العملية.")
            return
        
        admins = db_fetchall("SELECT user_id FROM admins")
        if not admins:
            bot.edit_message_text("❌ لا يوجد مشرفين حالياً.", chat_id, call.message.id, reply_markup=admin_panel())
            return
        
        markup = types.InlineKeyboardMarkup()
        for admin in admins:
            admin_id = admin[0]
            btn = types.InlineKeyboardButton(f"👨‍💻 {admin_id}", callback_data=f"remove_admin_{admin_id}")
            markup.add(btn)
        btn_back = types.InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")
        markup.add(btn_back)
        
        bot.edit_message_text("👨‍💻 اختر المشرف الذي تريد إزالته:", chat_id, call.message.id, reply_markup=markup)

    elif data.startswith("remove_admin_"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ ليس لديك صلاحية هذه العملية.")
            return
        
        admin_id = int(data.replace("remove_admin_", ""))
        db_execute("DELETE FROM admins WHERE user_id = ?", (admin_id,))
        bot.answer_callback_query(call.id, f"✅ تم إزالة المشرف: {admin_id}")
        bot.edit_message_text("✅ تم إزالة المشرف بنجاح", chat_id, call.message.id, reply_markup=admin_panel())

    elif data == "get_files":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ ليس لديك صلاحية هذه العملية.")
            return
        
        files = os.listdir(UPLOAD_FOLDER)
        if not files:
            bot.edit_message_text("❌ لا توجد ملفات مرفوعة حالياً.", chat_id, call.message.id, reply_markup=admin_panel())
            return
        
        msg = "📁 جميع الملفات المرفوعة:\n\n"
        for f in files:
            file_info = db_fetchone("SELECT user_id, upload_time FROM files WHERE filename = ?", (f,))
            user_info = f"المستخدم: {file_info[0]}" if file_info else "مستخدم غير معروف"
            upload_time = file_info[1] if file_info else "وقت غير معروف"
            size = os.path.getsize(os.path.join(UPLOAD_FOLDER, f)) // 1024
            status = "🟢 شغال" if f in running_processes else "🔴 متوقف"
            msg += f"• {f} ({size} KB) — {status} — {user_info} — {upload_time}\n"
        
        bot.edit_message_text(msg, chat_id, call.message.id, reply_markup=admin_panel())

    elif data == "ban_user":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ ليس لديك صلاحية هذه العملية.")
            return
        
        msg = bot.edit_message_text("⛔ أرسل آيدي المستخدم لحظره:", chat_id, call.message.id)
        bot.register_next_step_handler(msg, ban_user_step)

    elif data == "unban_user":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ ليس لديك صلاحية هذه العملية.")
            return
        
        banned_users = db_fetchall("SELECT user_id FROM banned_users")
        if not banned_users:
            bot.edit_message_text("✅ لا يوجد مستخدمين محظورين حالياً.", chat_id, call.message.id, reply_markup=admin_panel())
            return
        
        markup = types.InlineKeyboardMarkup()
        for user in banned_users:
            user_id = user[0]
            btn = types.InlineKeyboardButton(f"👤 {user_id}", callback_data=f"unban_user_{user_id}")
            markup.add(btn)
        btn_back = types.InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")
        markup.add(btn_back)
        
        bot.edit_message_text("👤 اختر المستخدم الذي تريد فك حظره:", chat_id, call.message.id, reply_markup=markup)

    elif data.startswith("unban_user_"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ ليس لديك صلاحية هذه العملية.")
            return
        
        user_id_unban = int(data.replace("unban_user_", ""))
        db_execute("DELETE FROM banned_users WHERE user_id = ?", (user_id_unban,))
        bot.answer_callback_query(call.id, f"✅ تم فك حظر المستخدم: {user_id_unban}")
        bot.edit_message_text("✅ تم فك الحظر بنجاح", chat_id, call.message.id, reply_markup=admin_panel())

    elif data == "stats":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ ليس لديك صلاحية هذه العملية.")
            return
        
        total_files = len([f for f in os.listdir(UPLOAD_FOLDER) if f.endswith('.py')])
        total_admins = len(db_fetchall("SELECT user_id FROM admins"))
        total_banned = len(db_fetchall("SELECT user_id FROM banned_users"))
        total_channels = len(db_fetchall("SELECT channel_id FROM force_subscribe"))
        running_bots = len(running_processes)
        
        stats_msg = (
            f"📊 إحصائيات البوت:\n\n"
            f"• 📁 الملفات: {total_files}\n"
            f"• 👨‍💻 المشرفين: {total_admins}\n"
            f"• ⛔ المحظورين: {total_banned}\n"
            f"• 📢 قنوات الاشتراك: {total_channels}\n"
            f"• 🤖 البوتات النشطة: {running_bots}\n"
            f"• 💳 وضع الدفع: {'مفعل' if is_paid_mode() else 'معطل'}\n"
            f"• 🆓 الوضع المجاني: {'مفعل' if not is_paid_mode() else 'معطل'}\n"
            f"• 🔧 حالة البوت: {'نشط' if bot_enabled() else 'معطل'}\n"
        )
        
        bot.edit_message_text(stats_msg, chat_id, call.message.id, reply_markup=admin_panel())

    elif data == "add_channel":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ ليس لديك صلاحية هذه العملية.")
            return
        
        msg = bot.edit_message_text("📢 أرسل معرف القناة لإضافتها للاشتراك الإجباري (يجب أن يبدأ بـ @):", chat_id, call.message.id)
        bot.register_next_step_handler(msg, add_channel_step)

    elif data == "remove_channel":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ ليس لديك صلاحية هذه العملية.")
            return
        
        channels = db_fetchall("SELECT channel_id, channel_username FROM force_subscribe")
        if not channels:
            bot.edit_message_text("❌ لا توجد قنوات للاشتراك الإجباري.", chat_id, call.message.id, reply_markup=admin_panel())
            return
        
        markup = types.InlineKeyboardMarkup()
        for channel in channels:
            channel_id, channel_username = channel
            btn = types.InlineKeyboardButton(f"{channel_username}", callback_data=f"remove_channel_{channel_id}")
            markup.add(btn)
        btn_back = types.InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")
        markup.add(btn_back)
        
        bot.edit_message_text("📢 اختر القناة التي تريد إزالتها:", chat_id, call.message.id, reply_markup=markup)

    elif data.startswith("remove_channel_"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ ليس لديك صلاحية هذه العملية.")
            return
        
        channel_id = data.replace("remove_channel_", "")
        db_execute("DELETE FROM force_subscribe WHERE channel_id = ?", (channel_id,))
        bot.answer_callback_query(call.id, "✅ تم إزالة القناة بنجاح")
        bot.edit_message_text("✅ تم إزالة القناة بنجاح", chat_id, call.message.id, reply_markup=admin_panel())

    elif data == "list_channels":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ ليس لديك صلاحية هذه العملية.")
            return
        
        channels = db_fetchall("SELECT channel_id, channel_username, added_by FROM force_subscribe")
        if not channels:
            bot.edit_message_text("❌ لا توجد قنوات للاشتراك الإجباري.", chat_id, call.message.id, reply_markup=admin_panel())
            return
        
        msg = "📢 قنوات الاشتراك الإجباري:\n\n"
        for channel in channels:
            channel_id, channel_username, added_by = channel
            msg += f"• {channel_username} (تمت الإضافة بواسطة: {added_by})\n"
        
        bot.edit_message_text(msg, chat_id, call.message.id, reply_markup=admin_panel())

    elif data == "stop_bot":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ ليس لديك صلاحية هذه العملية.")
            return
        
        db_execute("UPDATE bot_settings SET setting_value = 'disabled' WHERE setting_key = 'bot_status'")
        bot.answer_callback_query(call.id, "⛔ تم إيقاف البوت بنجاح")
        bot.edit_message_text("⛔ تم إيقاف البوت بنجاح", chat_id, call.message.id, reply_markup=admin_panel())

    elif data == "start_bot":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ ليس لديك صلاحية هذه العملية.")
            return
        
        db_execute("UPDATE bot_settings SET setting_value = 'enabled' WHERE setting_key = 'bot_status'")
        bot.answer_callback_query(call.id, "🟢 تم تشغيل البوت بنجاح")
        bot.edit_message_text("🟢 تم تشغيل البوت بنجاح", chat_id, call.message.id, reply_markup=admin_panel())

    elif data == "free_mode":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ ليس لديك صلاحية هذه العملية.")
            return
        
        db_execute("UPDATE bot_settings SET setting_value = 'enabled' WHERE setting_key = 'free_mode'")
        db_execute("UPDATE bot_settings SET setting_value = 'disabled' WHERE setting_key = 'paid_mode'")
        bot.answer_callback_query(call.id, "🆓 تم تفعيل الوضع المجاني")
        bot.edit_message_text("🆓 تم تفعيل الوضع المجاني بنجاح", chat_id, call.message.id, reply_markup=admin_panel())

    elif data == "paid_mode":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ ليس لديك صلاحية هذه العملية.")
            return
        
        db_execute("UPDATE bot_settings SET setting_value = 'enabled' WHERE setting_key = 'paid_mode'")
        db_execute("UPDATE bot_settings SET setting_value = 'disabled' WHERE setting_key = 'free_mode'")
        bot.answer_callback_query(call.id, "💳 تم تفعيل الوضع المدفوع")
        bot.edit_message_text("💳 تم تفعيل الوضع المدفوع بنجاح", chat_id, call.message.id, reply_markup=admin_panel())

def add_admin_step(message):
    try:
        new_admin_id = int(message.text)
        db_execute("INSERT OR IGNORE INTO admins (user_id, added_by, added_time) VALUES (?, ?, ?)", 
                  (new_admin_id, message.from_user.id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        bot.send_message(message.chat.id, f"✅ تم إضافة المشرف الجديد: {new_admin_id}", reply_markup=admin_panel())
    except ValueError:
        bot.send_message(message.chat.id, "❌ يجب إدخال آيدي صحيح (أرقام فقط).", reply_markup=admin_panel())

def ban_user_step(message):
    try:
        user_id_ban = int(message.text)
        db_execute("INSERT OR IGNORE INTO banned_users (user_id, banned_by, ban_time, reason) VALUES (?, ?, ?, ?)", 
                  (user_id_ban, message.from_user.id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "تم الحظر من قبل المشرف"))
        bot.send_message(message.chat.id, f"⛔ تم حظر المستخدم: {user_id_ban}", reply_markup=admin_panel())
    except ValueError:
        bot.send_message(message.chat.id, "❌ يجب إدخال آيدي صحيح (أرقام فقط).", reply_markup=admin_panel())

def add_channel_step(message):
    channel_username = message.text.strip()
    if not channel_username.startswith('@'):
        bot.send_message(message.chat.id, "❌ يجب أن يبدأ معرف القناة بـ @", reply_markup=admin_panel())
        return
    
    try:
        chat = bot.get_chat(channel_username)
        db_execute("INSERT OR IGNORE INTO force_subscribe (channel_id, channel_username, added_by, added_time) VALUES (?, ?, ?, ?)", 
                  (chat.id, channel_username, message.from_user.id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        bot.send_message(message.chat.id, f"✅ تم إضافة القناة: {channel_username}", reply_markup=admin_panel())
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ خطأ في إضافة القناة: {e}", reply_markup=admin_panel())

def edit_file_step(message, filename):
    new_code = message.text
    path = os.path.join(UPLOAD_FOLDER, filename)
    
    if os.path.exists(path):
        if filename in running_processes:
            running_processes[filename].terminate()
            del running_processes[filename]
        
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_code)
        
        p = subprocess.Popen(["python3", path])
        running_processes[filename] = p
        
        bot.send_message(message.chat.id, f"✅ تم تحديث وتشغيل الملف: {filename}", reply_markup=file_control_panel(filename))
    else:
        bot.send_message(message.chat.id, f"❌ الملف غير موجود: {filename}")

def delete_file_step(message):
    filename = message.text.strip()
    path = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(path):
        bot.reply_to(message, "❌ الملف غير موجود.")
        return
    
    file_owner = db_fetchone("SELECT user_id FROM files WHERE filename = ?", (filename,))
    if not is_admin(message.from_user.id) and (not file_owner or file_owner[0] != message.from_user.id):
        bot.reply_to(message, "❌ ليس لديك صلاحية حذف هذا الملف.")
        return
    
    size = os.path.getsize(path) // 1024
    status = "🟢 شغال" if filename in running_processes else "🔴 متوقف"
    
    confirm = types.InlineKeyboardMarkup()
    confirm.add(types.InlineKeyboardButton("✅ تأكيد الحذف", callback_data=f"confirm_delete_{filename}"))
    confirm.add(types.InlineKeyboardButton("❌ إلغاء", callback_data="back_main"))
    
    bot.reply_to(message, f"📂 <b>{filename}</b>\nالحجم: {size} KB\nالحالة: {status}\n\nهل تريد الحذف؟", reply_markup=confirm)

def install_lib_step(message):
    lib_name = message.text.strip()
    try:
        subprocess.check_call(["pip", "install", lib_name])
        bot.reply_to(message, f"📦 تم تحميل المكتبة: {lib_name}")
    except Exception as e:
        bot.reply_to(message, f"⚠️ فشل تحميل المكتبة:\n{e}")

def make_bot_step(message):
    code = message.text
    filename = f"userbot_{message.from_user.id}_{int(time.time())}.py"
    path = os.path.join(UPLOAD_FOLDER, filename)
    
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)
    
    db_execute("INSERT INTO files (filename, user_id, upload_time) VALUES (?, ?, ?)",
              (filename, message.from_user.id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    
    msg = bot.reply_to(message, "⏳ جاري تشغيل البوت...")
    for i in range(1, 6):
        try:
            bot.edit_message_text(f"⏳ جاري التشغيل {i*20}%", message.chat.id, msg.id)
        except:
            pass
        time.sleep(0.5)
    
    p = subprocess.Popen(["python3", path])
    running_processes[filename] = p
    
    bot.edit_message_text(f"✅ تم إنشاء وتشغيل البوت: {filename}", message.chat.id, msg.id, reply_markup=main_menu())

def stop_one_step(message):
    filename = message.text.strip()
    if filename in running_processes:
        running_processes[filename].terminate()
        del running_processes[filename]
        bot.reply_to(message, f"⛔ تم إيقاف البوت: {filename}")
    else:
        bot.reply_to(message, "❌ البوت غير مشغل أو غير موجود.")

def start_one_step(message):
    filename = message.text.strip()
    path = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(path):
        bot.reply_to(message, "❌ الملف غير موجود.")
        return
    
    file_owner = db_fetchone("SELECT user_id FROM files WHERE filename = ?", (filename,))
    if not is_admin(message.from_user.id) and (not file_owner or file_owner[0] != message.from_user.id):
        bot.reply_to(message, "❌ ليس لديك صلاحية تشغيل هذا الملف.")
        return
    
    if filename in running_processes:
        bot.reply_to(message, "⚠️ البوت شغال بالفعل.")
        return
    
    p = subprocess.Popen(["python3", path])
    running_processes[filename] = p
    bot.reply_to(message, f"🟢 تم تشغيل البوت: {filename}")

def add_paid_user_id_step(message):
    try:
        target_user_id = int(message.text.strip())
        msg = bot.reply_to(message, "⏳ أرسل عدد الساعات للاشتراك (مثال: 24):")
        bot.register_next_step_handler(msg, add_paid_user_duration_step, target_user_id)
    except ValueError:
        bot.reply_to(message, "❌ يرجى إرسال آيدي صحيح (أرقام فقط).")

def add_paid_user_duration_step(message, target_user_id):
    try:
        hours = int(message.text.strip())
        expiry_time = datetime.now() + timedelta(hours=hours)
        expiry_str = expiry_time.strftime('%Y-%m-%d %H:%M:%S')
        
        db_execute("INSERT OR REPLACE INTO paid_users (user_id, expiry_time, added_by, added_time) VALUES (?, ?, ?, ?)",
                   (target_user_id, expiry_str, message.from_user.id, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        
        price = "غير محدد"
        if hours == 24: price = "250﷼يمني إشتراك يومي"
        elif hours == 168: price = "500﷼يمني1$دولار إشتراك اسبوعي "
        elif hours == 720: price = "1500﷼يمني3$دولار إشتراك شهري "
        elif hours > 8760: price = "2500﷼يمني5$دولار إشتراك مد الحياة "
        
        bot.reply_to(message, f"✅ تم تفعيل الاشتراك للمستخدم {target_user_id} لمدة {hours} ساعة.\nينتهي في: {expiry_str}")
        
        user_msg = (
            "✅تم دفع إشتراك إلا حسابك بنجاح\n"
            f"⏱مدة الإشتراك: {hours} ساعة\n"
            f"🏴‍☠السعر: {price}"
        )
        try:
            bot.send_message(target_user_id, user_msg)
        except:
            bot.reply_to(message, "⚠️ تم تفعيل الاشتراك ولكن تعذر إرسال رسالة للمستخدم (ربما قام بحظر البوت).")
            
    except ValueError:
        bot.reply_to(message, "❌ يرجى إرسال عدد ساعات صحيح (أرقام فقط).")

@bot.message_handler(content_types=['document', 'text'])
def handle_document(message):
    try:
        logger.info(f"تم استلام رسالة جديدة من {message.from_user.id}")
        
        if not hasattr(message, 'document') or not message.document:
            logger.warning("الرسالة المستلمة لا تحتوي على مستند")
            return
            
        user_id = message.from_user.id
        logger.info(f"معالجة مستند من المستخدم {user_id}")
        
        if not bot_enabled():
            logger.warning("تم رفض الطلب لأن البوت معطل")
            bot.send_message(message.chat.id, "⛔ البوت معطل حاليًا.")
            return
        
        if is_paid_mode() and not is_user_paid(user_id) and not is_admin(user_id):
            logger.warning(f"تم رفض الطلب لأن البوت في الوضع المدفوع للمستخدم {user_id}")
            paid_msg = (
                "💳 أنت غير مصرح لك باستخدام هذا الأمر.\n\n"
                "🏴‍☠ يرجى التواصل مع المطور للشراء: @VZZDZ\n\n"
                "💰 السعر\n"
                "🏴‍☠ يومي: 250﷼يمني\n"
                "🏴‍☠ أسبوعيًا: 500﷼يمني1$دولار\n"
                "🏴‍☠ شهريًا: 1500﷼يمني3$دولار\n"
                "🏴‍☠ سكربت بدون تشفير 100%: 2500﷼يمني5$"
            )
            markup = types.InlineKeyboardMarkup()
            btn_dev = types.InlineKeyboardButton("• الـمـطـور •", url="https://t.me/VZZDZ")
            btn_channel = types.InlineKeyboardButton("• قـنـاة الـمـطـور •", url="https://t.me/w_2_2_8")
            markup.add(btn_dev, btn_channel)
            bot.send_message(message.chat.id, paid_msg, reply_markup=markup)
            return
        
        if not check_subscription(user_id):
            logger.warning(f"المستخدم {user_id} غير مشترك في القنوات المطلوبة")
            channels = db_fetchall("SELECT channel_id, channel_username FROM force_subscribe")
            if channels:
                markup = types.InlineKeyboardMarkup()
                for channel in channels:
                    channel_id, channel_username = channel
                    btn = types.InlineKeyboardButton(f"انضم هنا {channel_username}", url=f"https://t.me/{channel_username[1:]}")
                    markup.add(btn)
                
                btn_check = types.InlineKeyboardButton("✅ تحقق من الاشتراك", callback_data="check_subscription")
                markup.add(btn_check)
                
                bot.send_message(message.chat.id, 
                              "📢 يرجى الاشتراك في القنوات التالية لاستخدام البوت:",
                              reply_markup=markup)
            return
        
        document = message.document
        logger.info(f"جاري معالجة الملف: {document.file_name}")
        
        if not document.file_name.endswith('.py'):
            logger.warning(f"تم رفض ملف بامتداد غير مدعوم: {document.file_name}")
            bot.reply_to(message, "❌ يرجى رفع ملف Python فقط (امتداد .py)")
            return
        
        try:
            file_info = bot.get_file(document.file_id)
            logger.info(f"تم الحصول على معلومات الملف: {file_info.file_path}")
            
            file_path = os.path.join(UPLOAD_FOLDER, document.file_name)
            counter = 1
            original_name = document.file_name
            
            while os.path.exists(file_path):
                name, ext = os.path.splitext(original_name)
                document.file_name = f"{name}_{counter}{ext}"
                file_path = os.path.join(UPLOAD_FOLDER, document.file_name)
                counter += 1
            
            logger.info(f"جاري تحميل الملف إلى: {file_path}")
            downloaded = bot.download_file(file_info.file_path)
            
            with open(file_path, "wb") as f:
                f.write(downloaded)
            
            logger.info("تم حفظ الملف بنجاح")
            
            db_execute("INSERT INTO files (filename, user_id, upload_time) VALUES (?, ?, ?)",
                     (document.file_name, user_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            
            bot.reply_to(message, f"📁 تم رفع الملف: {document.file_name}")
            
            if document.file_name.endswith(".py"):
                if document.file_name in running_processes:
                    logger.info(f"إيقاف العملية السابقة للملف: {document.file_name}")
                    running_processes[document.file_name].terminate()
                    del running_processes[document.file_name]
                
                logger.info(f"تشغيل الملف: {file_path}")
                p = subprocess.Popen(["python", file_path], 
                                  stdout=subprocess.PIPE, 
                                  stderr=subprocess.PIPE)
                running_processes[document.file_name] = p
                logger.info(f"تم بدء العملية بنجاح (PID: {p.pid})")
                
                bot.reply_to(message, f"✅ تم تشغيل البوت: {document.file_name}", 
                           reply_markup=file_control_panel(document.file_name))
            
            if user_id != DEVELOPER_ID:
                buttons = types.InlineKeyboardMarkup()
                buttons.add(
                    types.InlineKeyboardButton("🗑 حذف الملف", callback_data=f"dev_delete_{document.file_name}_{user_id}"),
                    types.InlineKeyboardButton("⛔ حظر المستخدم", callback_data=f"dev_ban_{user_id}"),
                    types.InlineKeyboardButton("⛔ إيقاف البوت", callback_data=f"dev_stop_{document.file_name}")
                )
                
                file_size = os.path.getsize(file_path) // 1024
                status = "🟢 شغال" if document.file_name in running_processes else "🔴 متوقف"
                user_info = f"@{message.from_user.username}" if message.from_user.username else f"{message.from_user.first_name} ({user_id})"
                
                bot.send_message(
                    DEVELOPER_ID,
                    f"📤 تم رفع ملف جديد!\n\n"
                    f"• اسم الملف: {document.file_name}\n"
                    f"• الحجم: {file_size} KB\n"
                    f"• الحالة: {status}\n"
                    f"• من: {user_info}",
                    reply_markup=buttons
                )
                logger.info("تم إرسال إشعار للمطور")
                
        except Exception as e:
            logger.error(f"حدث خطأ أثناء معالجة الملف: {str(e)}", exc_info=True)
            bot.reply_to(message, f"❌ حدث خطأ أثناء معالجة الملف: {str(e)}")
            
    except Exception as e:
        logger.critical(f"خطأ غير متوقع: {str(e)}", exc_info=True)
        bot.reply_to(message, "❌ حدث خطأ غير متوقع. يرجى المحاولة لاحقًا.")
        
        if 'DEVELOPER_ID' in globals():
            bot.send_message(DEVELOPER_ID, f"⚠️ خطأ في معالجة الملف:\n{str(e)}\n\n{str(traceback.format_exc())}")

if __name__ == "__main__":
    logger.info("🚀 البوت يعمل الآن...")
    bot.infinity_polling(timeout=10, long_polling_timeout=5)
