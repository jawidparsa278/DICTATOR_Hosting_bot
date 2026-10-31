import json, logging, os, re, shutil, sqlite3, subprocess, tempfile, threading, zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager
import telebot
from telebot import types

BASE = Path(os.getenv('DATA_DIR', '/data'))
PROJECTS = BASE / 'projects'
DB = BASE / 'host.db'
PROJECTS.mkdir(parents=True, exist_ok=True)
TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
OWNER_ID = int(os.environ['OWNER_ID'])
BOT = telebot.TeleBot(TOKEN, parse_mode='HTML', threaded=True, num_threads=8)
logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'), format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('python-host-pro')
LOCK = threading.RLock()
MAX_UPLOAD = int(os.getenv('MAX_UPLOAD_MB','20')) * 1024 * 1024
MAX_PROJECTS = int(os.getenv('MAX_PROJECTS_PER_USER','3'))
CONTAINER_PREFIX = 'phpro-'


@contextmanager
def db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA foreign_keys=ON')
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT, joined_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY, added_by INTEGER, added_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS banned(user_id INTEGER PRIMARY KEY, reason TEXT, banned_by INTEGER, banned_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL, runtime TEXT NOT NULL, entrypoint TEXT NOT NULL, path TEXT NOT NULL, container_name TEXT NOT NULL, image_name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'stopped', created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(user_id));
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS channels(channel TEXT PRIMARY KEY);
        ''')
        c.execute("INSERT OR IGNORE INTO settings VALUES('bot_enabled','1')")
        c.execute("INSERT OR IGNORE INTO settings VALUES('free_mode','1')")

init_db()


def is_admin(uid):
    if uid == OWNER_ID: return True
    with db() as c: return c.execute('SELECT 1 FROM admins WHERE user_id=?',(uid,)).fetchone() is not None


def setting(key, default=''):
    with db() as c:
        r=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        return r['value'] if r else default


def blocked(uid):
    with db() as c: return c.execute('SELECT 1 FROM banned WHERE user_id=?',(uid,)).fetchone() is not None


def subscribed(uid):
    with db() as c: channels=[r['channel'] for r in c.execute('SELECT channel FROM channels')]
    for ch in channels:
        try:
            m=BOT.get_chat_member(ch,uid)
            if m.status not in ('creator','administrator','member'): return False
        except Exception: return False
    return True


def home_markup(uid):
    m=types.InlineKeyboardMarkup(row_width=2)
    m.add(types.InlineKeyboardButton('📤 رفع مشروع Python / ZIP',callback_data='upload'))
    m.add(types.InlineKeyboardButton('📂 پروژه‌های من',callback_data='mine'),types.InlineKeyboardButton('📊 وضعیت سرور',callback_data='status'))
    if is_admin(uid): m.add(types.InlineKeyboardButton('🛠 پنل مدیریت',callback_data='admin'))
    return m


def project_markup(pid, uid):
    m=types.InlineKeyboardMarkup(row_width=2)
    m.add(types.InlineKeyboardButton('▶️ اجرا',callback_data=f'run:{pid}'),types.InlineKeyboardButton('⏹ توقف',callback_data=f'stop:{pid}'))
    m.add(types.InlineKeyboardButton('📜 لاگ‌ها',callback_data=f'logs:{pid}'),types.InlineKeyboardButton('🗑 حذف',callback_data=f'del:{pid}'))
    m.add(types.InlineKeyboardButton('🔙 برگشت',callback_data='mine'))
    return m


def get_project(pid):
    with db() as c: return c.execute('SELECT * FROM projects WHERE id=?',(pid,)).fetchone()


def own_project(pid, uid):
    p=get_project(pid)
    return p if p and (p['user_id']==uid or is_admin(uid)) else None


def docker(args, timeout=900):
    return subprocess.run(['docker',*args],capture_output=True,text=True,timeout=timeout,check=False)


def safe_name(s):
    s=re.sub(r'[^a-zA-Z0-9_-]+','-',s).strip('-').lower()
    return (s or 'project')[:32]


def safe_extract(zf, dest):
    root=dest.resolve()
    total=0
    for info in zf.infolist():
        if info.is_dir(): continue
        name=Path(info.filename)
        if name.is_absolute() or '..' in name.parts or not name.parts: raise ValueError('مسیر غیرمجاز داخل ZIP پیدا شد.')
        total += info.file_size
        if total > 100*1024*1024: raise ValueError('حجم بازشده ZIP بیشتر از ۱۰۰ مگابایت است.')
        target=(dest/name).resolve()
        if root not in target.parents and target != root: raise ValueError('مسیر ZIP امن نیست.')
        target.parent.mkdir(parents=True,exist_ok=True)
        with zf.open(info) as src, open(target,'wb') as out: shutil.copyfileobj(src,out)


def inspect_project(path):
    if (path/'package.json').exists():
        try: pkg=json.loads((path/'package.json').read_text())
        except Exception: raise ValueError('package.json معتبر نیست.')
        entry=(pkg.get('scripts') or {}).get('start')
        if entry: return 'node', 'npm start'
        main=pkg.get('main','index.js')
        if not re.fullmatch(r'[\w./-]+\.js',main) or '..' in Path(main).parts: raise ValueError('فایل main در package.json معتبر نیست.')
        if not (path/main).is_file(): raise ValueError('فایل ورودی Node پیدا نشد.')
        return 'node', f'node {main}'
    if not any((path/f).is_file() for f in ('main.py','app.py','bot.py')):
        raise ValueError('برای پروژه Python یکی از فایل‌های main.py، app.py یا bot.py لازم است.')
    req=path/'requirements.txt'
    if req.exists() and req.stat().st_size>100_000: raise ValueError('requirements.txt بیش از حد بزرگ است.')
    return 'python', 'python main.py' if (path/'main.py').exists() else ('python app.py' if (path/'app.py').exists() else 'python bot.py')


def build_and_run(p):
    path=Path(p['path'])
    runtime=p['runtime']; entry=p['entrypoint']
    dockerfile=path/'Dockerfile.host'
    if runtime=='python':
        dockerfile.write_text('''FROM python:3.12-slim\nENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\nWORKDIR /app\nCOPY . /app\nRUN if [ -f requirements.txt ]; then pip install --no-cache-dir --disable-pip-version-check -r requirements.txt; fi\nCMD ["sh","-lc",'''+json.dumps(entry)+''']\n''')
    else:
        dockerfile.write_text('''FROM node:22-bookworm-slim\nENV NODE_ENV=production\nWORKDIR /app\nCOPY package*.json ./\nRUN if [ -f package.json ]; then npm install --omit=dev --no-audit --no-fund; fi\nCOPY . /app\nCMD ["sh","-lc",'''+json.dumps(entry)+''']\n''')
    b=docker(['build','--pull','-f',str(path/'Dockerfile.host'),'-t',p['image_name'],str(path)],timeout=1200)
    if b.returncode: raise RuntimeError((b.stderr or b.stdout)[-3000:])
    docker(['rm','-f',p['container_name']])
    run=docker(['run','-d','--name',p['container_name'],'--restart','unless-stopped','--memory','256m','--memory-swap','256m','--cpus','0.50','--pids-limit','64','--ulimit','nofile=1024:1024','--cap-drop','ALL','--security-opt','no-new-privileges:true','--read-only','--tmpfs','/tmp:rw,noexec,nosuid,size=32m','--network','bridge',p['image_name']])
    if run.returncode: raise RuntimeError((run.stderr or run.stdout)[-2000:])
    with db() as c: c.execute("UPDATE projects SET status='running' WHERE id=?",(p['id'],))


def stop_project(p):
    r=docker(['stop',p['container_name']],timeout=30)
    if r.returncode not in (0,1): raise RuntimeError(r.stderr[-1000:])
    with db() as c: c.execute("UPDATE projects SET status='stopped' WHERE id=?",(p['id'],))


def start_project(p):
    r=docker(['start',p['container_name']],timeout=30)
    if r.returncode: raise RuntimeError(r.stderr[-1000:])
    with db() as c: c.execute("UPDATE projects SET status='running' WHERE id=?",(p['id'],))


def send_home(chat_id, uid):
    BOT.send_message(chat_id,'<b>Python Host PRO</b>\nپروژه Python یا Node خود را آپلود کن تا در کانتینر جدا اجرا شود.',reply_markup=home_markup(uid))


@BOT.message_handler(commands=['start','help'])
def start(m):
    uid=m.from_user.id
    with db() as c: c.execute('INSERT OR IGNORE INTO users VALUES(?,?,?)',(uid,m.from_user.username or '',datetime.now(timezone.utc).isoformat()))
    if blocked(uid): return BOT.reply_to(m,'حساب شما مسدود است.')
    if setting('bot_enabled','1')!='1' and not is_admin(uid): return BOT.reply_to(m,'ربات موقتاً غیرفعال است.')
    if not subscribed(uid): return BOT.reply_to(m,'ابتدا در کانال‌های اجباری عضو شو و دوباره /start را بزن.')
    send_home(m.chat.id,uid)


@BOT.message_handler(commands=['myid'])
def myid(m): BOT.reply_to(m,f'ID: <code>{m.from_user.id}</code>')


@BOT.callback_query_handler(func=lambda c: True)
def callbacks(c):
    uid=c.from_user.id; data=c.data or ''; chat=c.message.chat.id
    if blocked(uid) and not is_admin(uid): return BOT.answer_callback_query(c.id,'حساب مسدود است.',show_alert=True)
    if data=='upload':
        BOT.answer_callback_query(c.id)
        BOT.send_message(chat,'فایل <b>.py</b> یا پروژهٔ <b>.zip</b> را بفرست. برای ZIP، فایل ورودی main.py/app.py/bot.py یا package.json لازم است.')
        return
    if data=='mine':
        with db() as con: rows=con.execute('SELECT id,name,runtime,status FROM projects WHERE user_id=? OR ?=1 ORDER BY created_at DESC LIMIT 20',(uid,1 if is_admin(uid) else 0)).fetchall()
        if not rows: return BOT.edit_message_text('هنوز پروژه‌ای ثبت نشده است.',chat,c.message.message_id,reply_markup=home_markup(uid))
        m=types.InlineKeyboardMarkup(row_width=1)
        for p in rows: m.add(types.InlineKeyboardButton(f"{p['name']} | {p['runtime']} | {p['status']}",callback_data='view:'+p['id']))
        m.add(types.InlineKeyboardButton('🔙 خانه',callback_data='home'))
        return BOT.edit_message_text('پروژه‌ها:',chat,c.message.message_id,reply_markup=m)
    if data=='home': return BOT.edit_message_text('منوی اصلی',chat,c.message.message_id,reply_markup=home_markup(uid))
    if data=='status':
        r=docker(['info','--format','{{.ServerVersion}}'])
        return BOT.answer_callback_query(c.id,'Docker: '+(r.stdout.strip() if r.returncode==0 else 'در دسترس نیست'),show_alert=True)
    if data=='admin':
        if not is_admin(uid): return BOT.answer_callback_query(c.id,'دسترسی ندارید.',show_alert=True)
        m=types.InlineKeyboardMarkup(row_width=2)
        m.add(types.InlineKeyboardButton('آمار',callback_data='stats'),types.InlineKeyboardButton('روشن/خاموش',callback_data='togglebot'))
        m.add(types.InlineKeyboardButton('راهنمای دستورات ادمین',callback_data='adminhelp'))
        return BOT.edit_message_text('پنل مدیریت',chat,c.message.message_id,reply_markup=m)
    if data=='adminhelp': return BOT.answer_callback_query(c.id,'/stats /ban ID /unban ID /addadmin ID /channel @name /unchannel @name',show_alert=True)
    if data=='stats':
        if not is_admin(uid): return BOT.answer_callback_query(c.id,'دسترسی ندارید.',show_alert=True)
        with db() as con:
            u=con.execute('SELECT COUNT(*) n FROM users').fetchone()['n']; p=con.execute('SELECT COUNT(*) n FROM projects').fetchone()['n']
        return BOT.answer_callback_query(c.id,f'کاربران: {u} | پروژه‌ها: {p}',show_alert=True)
    if data=='togglebot':
        if not is_admin(uid): return BOT.answer_callback_query(c.id,'دسترسی ندارید.',show_alert=True)
        with db() as con: con.execute("UPDATE settings SET value=? WHERE key='bot_enabled'",('0' if setting('bot_enabled','1')=='1' else '1',))
        return BOT.answer_callback_query(c.id,'وضعیت تغییر کرد.',show_alert=True)
    if data.startswith('view:'):
        p=own_project(data[5:],uid)
        if not p: return BOT.answer_callback_query(c.id,'پروژه پیدا نشد یا دسترسی ندارید.',show_alert=True)
        return BOT.edit_message_text(f"<b>{p['name']}</b>\nشناسه: <code>{p['id']}</code>\nRuntime: {p['runtime']}\nوضعیت: {p['status']}",chat,c.message.message_id,reply_markup=project_markup(p['id'],uid))
    if data.startswith(('run:','stop:','logs:','del:')):
        action,pid=data.split(':',1); p=own_project(pid,uid)
        if not p: return BOT.answer_callback_query(c.id,'دسترسی ندارید یا پروژه وجود ندارد.',show_alert=True)
        try:
            if action=='run':
                BOT.answer_callback_query(c.id,'در حال ساخت و اجرا...')
                build_and_run(p); return BOT.send_message(chat,'✅ پروژه اجرا شد.',reply_markup=project_markup(pid,uid))
            if action=='stop': stop_project(p); return BOT.answer_callback_query(c.id,'پروژه متوقف شد.')
            if action=='logs':
                r=docker(['logs','--tail','40',p['container_name']],timeout=20)
                out=(r.stdout+'\n'+r.stderr).strip() or 'لاگی وجود ندارد.'
                return BOT.send_message(chat,'<pre>'+esc(out[-3500:])+'</pre>')
            if action=='del':
                docker(['rm','-f',p['container_name']]); docker(['image','rm','-f',p['image_name']])
                shutil.rmtree(p['path'],ignore_errors=True)
                with db() as con: con.execute('DELETE FROM projects WHERE id=?',(pid,))
                return BOT.answer_callback_query(c.id,'پروژه حذف شد.')
        except Exception as e:
            log.exception('project action failed')
            return BOT.send_message(chat,'❌ خطا: <code>'+esc(str(e)[-2500:])+'</code>')
    BOT.answer_callback_query(c.id,'دستور ناشناخته است.')


def esc(s):
    return s.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')


@BOT.message_handler(content_types=['document'])
def upload(m):
    uid=m.from_user.id
    if blocked(uid) and not is_admin(uid): return
    if setting('bot_enabled','1')!='1' and not is_admin(uid): return BOT.reply_to(m,'ربات موقتاً غیرفعال است.')
    if not subscribed(uid): return BOT.reply_to(m,'ابتدا در کانال‌های اجباری عضو شو.')
    doc=m.document; filename=Path(doc.file_name or 'upload').name
    if doc.file_size and doc.file_size>MAX_UPLOAD: return BOT.reply_to(m,f'حداکثر حجم فایل {MAX_UPLOAD//1024//1024}MB است.')
    ext=Path(filename).suffix.lower()
    if ext not in ('.py','.zip'): return BOT.reply_to(m,'فقط فایل .py یا .zip پشتیبانی می‌شود.')
    with db() as con:
        count=con.execute('SELECT COUNT(*) n FROM projects WHERE user_id=?',(uid,)).fetchone()['n']
    if count>=MAX_PROJECTS and not is_admin(uid): return BOT.reply_to(m,f'سقف پروژه برای هر کاربر {MAX_PROJECTS} است.')
    try:
        f=BOT.get_file(doc.file_id); data=BOT.download_file(f.file_path)
        pid=os.urandom(4).hex(); folder=PROJECTS/str(uid)/pid; folder.mkdir(parents=True,exist_ok=False)
        if ext=='.py':
            (folder/'main.py').write_bytes(data); display=Path(filename).stem
        else:
            zpath=folder/'upload.zip'; zpath.write_bytes(data)
            with zipfile.ZipFile(zpath) as zf: safe_extract(zf,folder)
            zpath.unlink(missing_ok=True); display=Path(filename).stem
            # Flatten a single top-level project directory.
            children=list(folder.iterdir())
            dirs=[x for x in children if x.is_dir()]
            files=[x for x in children if x.is_file()]
            if len(dirs)==1 and not files:
                nested=dirs[0]
                for x in nested.iterdir(): shutil.move(str(x),folder/x.name)
                nested.rmdir()
        runtime,entry=inspect_project(folder)
        name=safe_name(display)
        image=f'python-host-proj-{uid}-{pid}'
        container=CONTAINER_PREFIX+pid
        with db() as con:
            con.execute('INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?,?)',(pid,uid,name,runtime,entry,str(folder),container,image,'stopped',datetime.now(timezone.utc).isoformat()))
        BOT.reply_to(m,f'✅ پروژه <b>{esc(name)}</b> ثبت شد.\nRuntime: {runtime}\nشناسه: <code>{pid}</code>\nبرای اجرا دکمهٔ زیر را بزن.',reply_markup=project_markup(pid,uid))
    except Exception as e:
        log.exception('upload failed')
        shutil.rmtree(locals().get('folder',Path('/nonexistent')),ignore_errors=True)
        BOT.reply_to(m,'❌ فایل آماده نشد: '+esc(str(e)[:1500]))


@BOT.message_handler(commands=['stats','ban','unban','addadmin','channel','unchannel'])
def admin_commands(m):
    if not is_admin(m.from_user.id): return BOT.reply_to(m,'دسترسی ندارید.')
    parts=(m.text or '').split(maxsplit=2); cmd=parts[0].split('@')[0]
    if cmd=='/stats':
        with db() as c:
            u=c.execute('SELECT COUNT(*) n FROM users').fetchone()['n']; p=c.execute('SELECT COUNT(*) n FROM projects').fetchone()['n']
        return BOT.reply_to(m,f'کاربران: {u}\nپروژه‌ها: {p}')
    if len(parts)<2: return BOT.reply_to(m,'شناسه یا نام کانال را وارد کن.')
    if cmd=='/ban':
        uid=int(parts[1]); reason=parts[2] if len(parts)>2 else 'بدون توضیح'
        with db() as c: c.execute('INSERT OR REPLACE INTO banned VALUES(?,?,?,?)',(uid,reason,m.from_user.id,datetime.now(timezone.utc).isoformat()))
        return BOT.reply_to(m,'کاربر مسدود شد.')
    if cmd=='/unban':
        with db() as c: c.execute('DELETE FROM banned WHERE user_id=?',(int(parts[1]),))
        return BOT.reply_to(m,'مسدودیت برداشته شد.')
    if cmd=='/addadmin':
        with db() as c: c.execute('INSERT OR IGNORE INTO admins VALUES(?,?,?)',(int(parts[1]),m.from_user.id,datetime.now(timezone.utc).isoformat()))
        return BOT.reply_to(m,'ادمین اضافه شد.')
    if cmd=='/channel':
        with db() as c: c.execute('INSERT OR IGNORE INTO channels VALUES(?)',(parts[1],))
        return BOT.reply_to(m,'کانال اشتراک اجباری اضافه شد.')
    if cmd=='/unchannel':
        with db() as c: c.execute('DELETE FROM channels WHERE channel=?',(parts[1],))
        return BOT.reply_to(m,'کانال حذف شد.')


if __name__=='__main__':
    log.info('Python Host PRO starting')
    BOT.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=25)
